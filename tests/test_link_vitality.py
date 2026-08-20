"""Tests for the scheduled link vitality checker.

Unit-level coverage for the HTTP probe logic uses httpx.MockTransport so
no test ever touches the real network; batch-selection and the API filter
are covered against a real (SQLite) database, matching the rest of this
suite's discipline.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app.classifier import hash_url
from app.database import SessionLocal
from app.models import Channel, Link, Workspace
from scripts.check_link_vitality import _select_batch, check_one, check_vitality
from tests.conftest import register_workspace


def _seed_link(workspace_id: int, channel_id: int, message_id: int, url: str, **overrides):
    db = SessionLocal()
    try:
        link = Link(
            workspace_id=workspace_id,
            channel_id=channel_id,
            message_id=message_id,
            url=url,
            url_hash=hash_url(url),
            domain="example.com",
            category="other",
            confidence=0.5,
            classified_by="rules",
            **overrides,
        )
        db.add(link)
        db.commit()
        db.refresh(link)
        return link.id
    finally:
        db.close()


@pytest.fixture
def workspace_and_channel():
    db = SessionLocal()
    try:
        workspace = Workspace(name="Vitality WS")
        db.add(workspace)
        db.flush()
        channel = Channel(workspace_id=workspace.id, tg_channel_id="1", username="c")
        db.add(channel)
        db.commit()
        return workspace.id, channel.id
    finally:
        db.close()


# --- check_one: the per-URL probe -------------------------------------------------


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_check_one_reports_alive_on_200():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    async def run():
        async with httpx.AsyncClient(transport=_transport(handler)) as client:
            return await check_one(client, "https://example.com/a")

    result = asyncio.run(run())
    assert result.outcome == "alive"
    assert result.http_status == 200


def test_check_one_reports_dead_on_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async def run():
        async with httpx.AsyncClient(transport=_transport(handler)) as client:
            return await check_one(client, "https://example.com/gone")

    result = asyncio.run(run())
    assert result.outcome == "dead"
    assert result.http_status == 404


def test_check_one_falls_back_to_get_when_head_is_rejected():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "HEAD":
            return httpx.Response(405)
        return httpx.Response(200)

    async def run():
        async with httpx.AsyncClient(transport=_transport(handler)) as client:
            return await check_one(client, "https://example.com/head-not-allowed")

    result = asyncio.run(run())
    assert calls == ["HEAD", "GET"]
    assert result.outcome == "alive"
    assert result.http_status == 200


def test_check_one_treats_network_failure_as_unreachable_not_dead():
    """A retired domain and a momentary DNS failure look identical from
    here, so one connection error is not a death sentence — only a streak
    of them is."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async def run():
        async with httpx.AsyncClient(transport=_transport(handler)) as client:
            return await check_one(client, "https://unreachable.example/x")

    result = asyncio.run(run())
    assert result.outcome == "unreachable"
    assert result.http_status is None


def test_check_one_treats_timeout_as_unreachable_not_dead():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    async def run():
        async with httpx.AsyncClient(transport=_transport(handler)) as client:
            return await check_one(client, "https://slow.example/x")

    result = asyncio.run(run())
    assert result.outcome == "unreachable"
    assert result.http_status is None


# --- _select_batch: never-checked first, then oldest-checked ----------------------


def test_select_batch_prefers_never_checked_over_stale(workspace_and_channel):
    workspace_id, channel_id = workspace_and_channel
    from datetime import timedelta

    from app.timeutil import utcnow

    checked_id = _seed_link(
        workspace_id, channel_id, 1, "https://example.com/checked", last_checked_at=utcnow() - timedelta(days=5)
    )
    never_checked_id = _seed_link(workspace_id, channel_id, 2, "https://example.com/never")

    db = SessionLocal()
    try:
        batch = _select_batch(db, limit=10)
    finally:
        db.close()

    ids = [link.id for link in batch]
    assert ids == [never_checked_id, checked_id]


def test_select_batch_respects_the_limit(workspace_and_channel):
    workspace_id, channel_id = workspace_and_channel
    for i in range(5):
        _seed_link(workspace_id, channel_id, i, f"https://example.com/{i}")

    db = SessionLocal()
    try:
        batch = _select_batch(db, limit=3)
    finally:
        db.close()

    assert len(batch) == 3


# --- check_vitality: end-to-end against a fake network ----------------------------


def test_check_vitality_persists_results(workspace_and_channel):
    workspace_id, channel_id = workspace_and_channel
    alive_id = _seed_link(workspace_id, channel_id, 1, "https://example.com/alive")
    dead_id = _seed_link(workspace_id, channel_id, 2, "https://example.com/dead")

    def handler(request: httpx.Request) -> httpx.Response:
        if "alive" in str(request.url):
            return httpx.Response(200)
        return httpx.Response(404)

    checked = asyncio.run(check_vitality(transport=httpx.MockTransport(handler)))
    assert checked == 2

    db = SessionLocal()
    try:
        alive = db.get(Link, alive_id)
        dead = db.get(Link, dead_id)
        assert alive.is_alive is True
        assert alive.http_status == 200
        assert alive.last_checked_at is not None
        assert dead.is_alive is False
        assert dead.http_status == 404
    finally:
        db.close()


def test_check_vitality_is_a_noop_with_no_links_due():
    checked = asyncio.run(check_vitality(transport=httpx.MockTransport(lambda r: httpx.Response(200))))
    assert checked == 0


# --- API filter: GET /links?alive=... ----------------------------------------------


def test_search_can_filter_by_alive_state(client: TestClient):
    register_workspace(client, email="vitality@example.com", workspace_name="Vitality Co")
    workspace_id = client.get("/auth/me").json()["workspace_id"]
    channel = client.post("/channels", json={"tg_channel_id": "1", "username": "c1"}).json()

    _seed_link(workspace_id, channel["id"], 1, "https://example.com/alive", is_alive=True, http_status=200)
    _seed_link(workspace_id, channel["id"], 2, "https://example.com/dead", is_alive=False, http_status=404)
    _seed_link(workspace_id, channel["id"], 3, "https://example.com/unknown")

    alive_resp = client.get("/links", params={"alive": "true"})
    assert alive_resp.status_code == 200
    assert [i["url"] for i in alive_resp.json()["items"]] == ["https://example.com/alive"]

    dead_resp = client.get("/links", params={"alive": "false"})
    assert [i["url"] for i in dead_resp.json()["items"]] == ["https://example.com/dead"]

    # No filter includes the never-checked link too.
    all_resp = client.get("/links")
    assert all_resp.json()["total"] == 3
