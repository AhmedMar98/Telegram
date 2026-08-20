"""A dead link and an unreachable one are not the same thing.

The checker used to answer `status_code < 400` and store the result. That
meant a single 429 — the checker itself being throttled — recorded every
link on that host as dead in one run, and a 503 during a five-minute
deploy did the same. These tests pin the three-state model that replaced
it, and the backoff and priority rules built on top of it.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.models import Channel, Link, Workspace
from app.timeutil import utcnow
from app.vitality import UNREACHABLE_DEATH_THRESHOLD, status_category
from scripts.check_link_vitality import (
    BACKOFF_AFTER_FAILURES,
    BACKOFF_DAYS,
    ProbeResult,
    _select_batch,
    apply_probe,
    check_one,
)
from tests.conftest import register_workspace


def _probe(handler) -> ProbeResult:
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await check_one(client, "https://example.com/x")

    return asyncio.run(run())


# --- the three outcomes ----------------------------------------------------


@pytest.mark.parametrize("status", [429, 408, 425])
def test_a_throttled_response_is_unreachable_not_dead(status: int):
    """This is the regression that motivated the whole change: one popular
    host rate-limiting a batch used to kill every link on it."""
    result = _probe(lambda request: httpx.Response(status))
    assert result.outcome == "unreachable"
    assert result.http_status == status


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_server_error_is_unreachable_not_dead(status: int):
    """5xx says the server has a problem, not that the resource is gone."""
    assert _probe(lambda request: httpx.Response(status)).outcome == "unreachable"


@pytest.mark.parametrize("status", [400, 403, 404, 410, 418])
def test_a_client_error_is_dead(status: int):
    assert _probe(lambda request: httpx.Response(status)).outcome == "dead"


@pytest.mark.parametrize("status", [200, 204, 301, 302, 399])
def test_a_success_or_redirect_is_alive(status: int):
    assert _probe(lambda request: httpx.Response(status)).outcome == "alive"


# --- folding a probe into stored state -------------------------------------


def _link(**kwargs) -> Link:
    defaults = {
        "url": "https://example.com/x",
        "is_alive": None,
        "http_status": None,
        "last_alive_at": None,
        "consecutive_failures": 0,
    }
    return Link(**{**defaults, **kwargs})


def test_a_successful_check_records_when_it_was_last_alive():
    link = _link()
    now = utcnow()
    apply_probe(link, ProbeResult("alive", 200), now)

    assert link.is_alive is True
    assert link.last_alive_at == now
    assert link.last_checked_at == now
    assert link.consecutive_failures == 0


def test_a_successful_check_clears_an_earlier_failure_streak():
    link = _link(is_alive=False, consecutive_failures=7)
    apply_probe(link, ProbeResult("alive", 200), utcnow())
    assert link.consecutive_failures == 0
    assert link.is_alive is True


def test_a_definite_client_error_kills_the_link_immediately():
    """A 404 is not ambiguous — no streak is needed."""
    link = _link(is_alive=True)
    apply_probe(link, ProbeResult("dead", 404), utcnow())
    assert link.is_alive is False
    assert link.consecutive_failures == 1


def test_one_unreachable_check_does_not_kill_a_working_link():
    link = _link(is_alive=True, last_alive_at=utcnow() - timedelta(days=1))
    before = link.last_alive_at

    apply_probe(link, ProbeResult("unreachable", 503), utcnow())

    assert link.is_alive is True, "a single 503 must not overturn a known-good link"
    assert link.consecutive_failures == 1
    assert link.last_alive_at == before, "an unsuccessful check is not a sighting"


def test_a_sustained_unreachable_streak_eventually_kills_the_link():
    """Not forgiving forever: repeated unreachability *is* evidence."""
    link = _link(is_alive=True)
    for _ in range(UNREACHABLE_DEATH_THRESHOLD):
        apply_probe(link, ProbeResult("unreachable", 503), utcnow())

    assert link.consecutive_failures == UNREACHABLE_DEATH_THRESHOLD
    assert link.is_alive is False


def test_the_failing_status_is_still_recorded_while_the_link_stays_alive():
    """The user must be able to see *why* a link is being retried."""
    link = _link(is_alive=True)
    apply_probe(link, ProbeResult("unreachable", 429), utcnow())
    assert link.http_status == 429
    assert link.is_alive is True


# --- human-readable categories ---------------------------------------------


@pytest.mark.parametrize(
    ("http_status", "is_alive", "expected"),
    [
        (None, None, "unchecked"),
        (None, False, "unreachable"),
        (200, True, "ok"),
        (301, True, "redirect"),
        (403, False, "blocked"),
        (401, False, "blocked"),
        (404, False, "missing"),
        (410, False, "missing"),
        (429, True, "throttled"),
        (503, True, "server_error"),
        (418, False, "client_error"),
    ],
)
def test_status_category(http_status, is_alive, expected):
    assert status_category(http_status, is_alive) == expected


# --- batch selection: priority and backoff ---------------------------------


@pytest.fixture
def workspace_and_channel():
    db = SessionLocal()
    try:
        workspace = Workspace(name="Vitality WS")
        db.add(workspace)
        db.flush()
        channel = Channel(workspace_id=workspace.id, tg_channel_id="7007")
        db.add(channel)
        db.commit()
        return workspace.id, channel.id
    finally:
        db.close()


def _seed(workspace_id: int, channel_id: int, rows: list[dict]) -> dict[str, int]:
    db = SessionLocal()
    ids = {}
    try:
        for index, row in enumerate(rows):
            url = row.pop("url")
            link = Link(
                workspace_id=workspace_id,
                channel_id=channel_id,
                message_id=index,
                url=url,
                url_hash=f"hash-{index}",
                domain="example.com",
                category="other",
                confidence=0.5,
                classified_by="rules",
                source_type="text",
                **row,
            )
            db.add(link)
            db.flush()
            ids[url] = link.id
        db.commit()
        return ids
    finally:
        db.close()


def _batch_urls(limit: int = 10) -> list[str]:
    db = SessionLocal()
    try:
        return [link.url for link in _select_batch(db, limit)]
    finally:
        db.close()


def test_favorites_are_checked_before_anything_else(workspace_and_channel):
    workspace_id, channel_id = workspace_and_channel
    _seed(
        workspace_id,
        channel_id,
        [
            {"url": "https://example.com/plain-never-checked"},
            {"url": "https://example.com/starred", "is_favorite": True, "last_checked_at": utcnow()},
        ],
    )

    assert _batch_urls()[0] == "https://example.com/starred"


def test_never_checked_links_come_before_stale_ones(workspace_and_channel):
    workspace_id, channel_id = workspace_and_channel
    _seed(
        workspace_id,
        channel_id,
        [
            {"url": "https://example.com/stale", "last_checked_at": utcnow() - timedelta(days=1)},
            {"url": "https://example.com/never"},
        ],
    )

    assert _batch_urls() == ["https://example.com/never", "https://example.com/stale"]


def test_a_repeatedly_failing_link_is_skipped_until_the_backoff_expires(workspace_and_channel):
    """The run budget is fixed; re-confirming week-old corpses every six
    hours means the fresh links wait."""
    workspace_id, channel_id = workspace_and_channel
    _seed(
        workspace_id,
        channel_id,
        [
            {
                "url": "https://example.com/long-dead",
                "is_alive": False,
                "consecutive_failures": BACKOFF_AFTER_FAILURES,
                "last_checked_at": utcnow() - timedelta(hours=6),
            },
            {"url": "https://example.com/fresh"},
        ],
    )

    assert _batch_urls() == ["https://example.com/fresh"]


def test_the_backoff_expires_and_the_link_is_retried(workspace_and_channel):
    workspace_id, channel_id = workspace_and_channel
    _seed(
        workspace_id,
        channel_id,
        [
            {
                "url": "https://example.com/long-dead",
                "is_alive": False,
                "consecutive_failures": BACKOFF_AFTER_FAILURES,
                "last_checked_at": utcnow() - timedelta(days=BACKOFF_DAYS + 1),
            }
        ],
    )

    assert _batch_urls() == ["https://example.com/long-dead"]


def test_a_backed_off_link_is_skipped_even_when_starred(workspace_and_channel):
    """Starring a link does not make a server that has been down for a week
    answer any faster."""
    workspace_id, channel_id = workspace_and_channel
    _seed(
        workspace_id,
        channel_id,
        [
            {
                "url": "https://example.com/starred-dead",
                "is_favorite": True,
                "is_alive": False,
                "consecutive_failures": BACKOFF_AFTER_FAILURES + 2,
                "last_checked_at": utcnow(),
            }
        ],
    )

    assert _batch_urls() == []


def test_a_link_is_never_selected_twice_in_one_batch(workspace_and_channel):
    """A starred never-checked link matches two passes; it must appear once,
    or the batch silently checks fewer distinct links than its limit says."""
    workspace_id, channel_id = workspace_and_channel
    _seed(workspace_id, channel_id, [{"url": "https://example.com/starred-new", "is_favorite": True}])

    assert _batch_urls() == ["https://example.com/starred-new"]


# --- the API surface -------------------------------------------------------


def _add(client: TestClient, text: str) -> int:
    assert client.post("/links", json={"text": text}).status_code == 201
    return client.get("/links").json()["items"][0]["id"]


def test_archiving_removes_a_link_from_default_results(client: TestClient):
    register_workspace(client, email="arch@example.com", workspace_name="Arch")
    link_id = _add(client, "https://example.com/dead.pdf")

    assert client.post(f"/links/{link_id}/archive").status_code == 200

    assert client.get("/links").json()["total"] == 0
    assert client.get("/links", params={"include_archived": True}).json()["total"] == 1


def test_archiving_is_reversible(client: TestClient):
    register_workspace(client, email="unarch@example.com", workspace_name="Unarch")
    link_id = _add(client, "https://example.com/dead.pdf")
    client.post(f"/links/{link_id}/archive")

    client.post(f"/links/{link_id}/archive", params={"is_archived": False})

    assert client.get("/links").json()["total"] == 1


def test_an_archived_link_is_still_in_the_export(client: TestClient):
    """Archiving is a view preference. Silently dropping the link from the
    user's own data export would make the export a lie."""
    register_workspace(client, email="archexp@example.com", workspace_name="ArchExp")
    link_id = _add(client, "https://example.com/dead.pdf")
    client.post(f"/links/{link_id}/archive")

    rows = client.get("/links/export.json").json()
    assert [r["url"] for r in rows] == ["https://example.com/dead.pdf"]
    assert rows[0]["is_archived"] is True


def test_cannot_archive_another_workspaces_link(client: TestClient):
    register_workspace(client, email="archo@example.com", workspace_name="ArchO")
    victim = _add(client, "https://example.com/theirs.pdf")
    client.post("/auth/logout")

    register_workspace(client, email="archa@example.com", workspace_name="ArchA")
    assert client.post(f"/links/{victim}/archive").status_code == 404


def test_a_fresh_link_reports_itself_unchecked(client: TestClient):
    register_workspace(client, email="cat@example.com", workspace_name="Cat")
    _add(client, "https://example.com/new.pdf")

    item = client.get("/links").json()["items"][0]
    assert item["status_category"] == "unchecked"
    assert item["consecutive_failures"] == 0
    assert item["last_alive_at"] is None
    assert item["is_archived"] is False


def test_stats_report_the_vitality_split(client: TestClient):
    register_workspace(client, email="vstats@example.com", workspace_name="VStats")
    _add(client, "https://example.com/a.pdf")

    stats = client.get("/links/stats").json()
    assert stats["vitality"] == {
        "alive": 0,
        "dead": 0,
        "unchecked": 1,
        "archived": 0,
        "deadest_domains": [],
    }
    assert stats["added_this_week"] == 1
    assert stats["added_this_month"] == 1


def test_stats_name_the_domains_that_rot(client: TestClient):
    register_workspace(client, email="rot@example.com", workspace_name="Rot")
    _add(client, "https://rotten.example/a.pdf https://rotten.example/b.pdf https://solid.example/c.pdf")

    db = SessionLocal()
    try:
        for link in db.query(Link).all():
            link.is_alive = link.domain == "solid.example"
        db.commit()
    finally:
        db.close()

    stats = client.get("/links/stats").json()
    assert stats["vitality"]["deadest_domains"] == [["rotten.example", 2]]
    assert stats["vitality"]["alive"] == 1
    assert stats["vitality"]["dead"] == 2


def test_sorting_by_last_checked_is_accepted(client: TestClient):
    register_workspace(client, email="sortchk@example.com", workspace_name="SortChk")
    _add(client, "https://example.com/a.pdf")
    assert client.get("/links", params={"sort": "checked"}).status_code == 200
