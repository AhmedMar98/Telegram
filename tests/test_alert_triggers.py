"""Phase 9b: the things that actually raise each alert.

Phase 9a built the vocabulary, the gate and the record. Four of the eight
types it declared had nothing that could ever raise them, and a declared
alert with no trigger is a switch for a message that never arrives — worse
than no switch, because it reads as a promise.

The structural test at the bottom is the phase's exit criterion in code:
every type in the catalogue must have somewhere that raises it.
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest
import yaml
from fastapi.testclient import TestClient

from app.alerts import (
    ADULT_CONTENT,
    ALERT_TYPES,
    BACKUP_RESULT,
    GROQ_QUOTA,
    UNSTABLE_CATEGORY,
    WEEKLY_DIGEST,
)
from app.classifier import llm
from app.database import SessionLocal
from app.models import Channel, ClassificationFeedback, Link, Notification
from tests.conftest import register_workspace

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PASSWORD = "j8Kd0-slwQ2x"

# Classified adult by the rules tier on the URL alone, so these tests do
# not depend on the optional LLM tier being reachable. Deliberately with
# no file extension: the extension rule outranks the keyword rule, so
# ".../xxx-clip.mp4" classifies as movies_series, not adult.
ADULT_URL = "https://example.com/adult/clip"


def _workspace(client: TestClient) -> int:
    return client.get("/auth/me").json()["workspace_id"]


def _enable(client: TestClient, key: str) -> None:
    assert client.patch(f"/notifications/preferences/{key}", json={"enabled": True}).status_code == 200


def _alerts(workspace_id: int, alert_type: str) -> list[Notification]:
    with SessionLocal() as db:
        return (
            db.query(Notification)
            .filter(Notification.workspace_id == workspace_id, Notification.alert_type == alert_type)
            .order_by(Notification.id)
            .all()
        )


# --- idea 152: a new adult-classified link ---------------------------------


def test_an_adult_link_raises_the_alert_once_switched_on(client: TestClient):
    register_workspace(client, email="a1@example.com", workspace_name="A1")
    workspace_id = _workspace(client)
    _enable(client, ADULT_CONTENT.key)

    client.post("/links", json={"text": ADULT_URL})

    raised = _alerts(workspace_id, ADULT_CONTENT.key)
    assert len(raised) == 1
    assert "1 رابط جديد" in raised[0].body


def test_the_adult_alert_is_off_until_asked_for(client: TestClient):
    """The default policy, exercised through a real trigger rather than
    asserted against the catalogue: content alerts are proactive sending."""
    register_workspace(client, email="a2@example.com", workspace_name="A2")
    workspace_id = _workspace(client)

    client.post("/links", json={"text": ADULT_URL})

    assert _alerts(workspace_id, ADULT_CONTENT.key) == []


def test_a_duplicate_adult_link_does_not_alert_again(client: TestClient):
    """The URL is only announced when a row was actually stored.

    This is the whole reason the alert is raised from the caller after its
    commit rather than from store_link: the second attempt rolls back
    inside a SAVEPOINT, and an alert sent there would announce a link that
    does not exist.
    """
    register_workspace(client, email="a3@example.com", workspace_name="A3")
    workspace_id = _workspace(client)
    _enable(client, ADULT_CONTENT.key)

    client.post("/links", json={"text": ADULT_URL})
    client.post("/links", json={"text": ADULT_URL})

    assert len(_alerts(workspace_id, ADULT_CONTENT.key)) == 1


def test_a_non_adult_link_raises_nothing(client: TestClient):
    register_workspace(client, email="a4@example.com", workspace_name="A4")
    workspace_id = _workspace(client)
    _enable(client, ADULT_CONTENT.key)

    client.post("/links", json={"text": "https://example.com/manual.pdf"})

    assert _alerts(workspace_id, ADULT_CONTENT.key) == []


def test_many_adult_links_produce_one_message_that_summarises_the_tail(client: TestClient):
    """A channel that posts nothing else must not paste hundreds of URLs
    into somebody's chat."""
    register_workspace(client, email="a5@example.com", workspace_name="A5")
    workspace_id = _workspace(client)
    _enable(client, ADULT_CONTENT.key)

    urls = " ".join(f"https://example.com/adult-{index}" for index in range(9))
    client.post("/links", json={"text": urls})

    raised = _alerts(workspace_id, ADULT_CONTENT.key)
    assert len(raised) == 1
    assert raised[0].body.count("  • ") == 5
    assert "و4 غيرها" in raised[0].body


def test_the_alerted_url_carries_no_telegram_markup(client: TestClient):
    """URLs come from message text somebody else wrote."""
    register_workspace(client, email="a6@example.com", workspace_name="A6")
    workspace_id = _workspace(client)
    _enable(client, ADULT_CONTENT.key)

    client.post("/links", json={"text": "https://example.com/adult_*hot*_show"})

    body = _alerts(workspace_id, ADULT_CONTENT.key)[0].body
    assert "*" not in body and "[" not in body and "_" not in body


def test_the_collector_accumulates_adult_links_across_channels(client: TestClient):
    """The run-level plumbing, which nothing else covers.

    _collect_channel builds a per-channel summary and commits per channel;
    the alert is sent once for the whole run. If the accumulation were
    dropped, every test above would still pass — they all go through the
    single-call manual path — and a workspace following twenty channels
    would get either twenty messages or none.
    """
    from datetime import UTC, datetime

    from app.ingest import IngestSummary
    from app.notify import report_adult_links
    from scripts import collect as collector

    class FakeMessage:
        def __init__(self, message_id: int, text: str):
            self.id = message_id
            self.raw_text = text
            self.date = datetime.now(UTC)
            self.reply_markup = None
            self.forward = None

    class FakeClient:
        def __init__(self, messages):
            self._messages = messages

        async def get_entity(self, ref):
            return f"entity:{ref}"

        def iter_messages(self, entity, **kwargs):
            selected = [m for m in self._messages if m.id > kwargs.get("min_id", 0)]

            async def _gen():
                for message in selected:
                    yield message

            return _gen()

    register_workspace(client, email="c1@example.com", workspace_name="C1")
    workspace_id = _workspace(client)
    _enable(client, ADULT_CONTENT.key)
    first = client.post("/channels", json={"tg_channel_id": "1", "username": "one"}).json()["id"]
    second = client.post("/channels", json={"tg_channel_id": "2", "username": "two"}).json()["id"]

    run = IngestSummary()
    with SessionLocal() as db:
        for index, channel_id in enumerate((first, second)):
            fake = FakeClient([FakeMessage(1, f"https://example.com/adult/from-{index}")])
            asyncio.run(collector._collect_channel(fake, db, db.get(Channel, channel_id), run))

    assert len(run.adult_urls) == 2

    with SessionLocal() as db:
        asyncio.run(report_adult_links(db, workspace_id, run.adult_urls))

    raised = _alerts(workspace_id, ADULT_CONTENT.key)
    assert len(raised) == 1, "one run, one message — not one per channel"
    assert "2 رابط جديد" in raised[0].body


# --- idea 160: what is left of the optional tier's quota -------------------


@pytest.fixture(autouse=True)
def _forget_quota():
    llm.reset_quota()
    yield
    llm.reset_quota()


def test_quota_is_read_from_whatever_dimensions_the_response_names():
    """No hardcoded list of dimensions: the pairing rule is the contract."""
    llm.record_quota_headers(
        {
            "x-ratelimit-remaining-requests": "50",
            "x-ratelimit-limit-requests": "1000",
            "x-ratelimit-remaining-tokens": "9000",
            "x-ratelimit-limit-tokens": "10000",
        }
    )

    assert [r.dimension for r in llm.quota_readings()] == ["requests", "tokens"]
    assert llm.lowest_quota().fraction_left == pytest.approx(0.05)


def test_a_remaining_header_with_no_limit_is_ignored():
    """A number with no denominator says nothing: "8000 left" of what?"""
    llm.record_quota_headers({"x-ratelimit-remaining-requests": "8000"})

    assert llm.quota_readings() == []


def test_reset_headers_are_not_mistaken_for_quota():
    """They sit in the same x-ratelimit-* family and carry durations."""
    llm.record_quota_headers(
        {
            "x-ratelimit-reset-requests": "7.66s",
            "x-ratelimit-limit-requests": "1000",
        }
    )

    assert llm.quota_readings() == []


def test_no_rate_limit_headers_at_all_means_no_reading():
    """The idea is conditional on these headers existing. If Groq sends
    none, or sends them under other names, this is the outcome: silence,
    never a fabricated warning."""
    llm.record_quota_headers({"content-type": "application/json"})

    assert llm.lowest_quota() is None


def test_header_names_are_matched_case_insensitively():
    llm.record_quota_headers({"X-RateLimit-Remaining-Requests": "1", "X-RateLimit-Limit-Requests": "100"})

    assert llm.lowest_quota().dimension == "requests"


def test_the_collector_warns_only_below_the_configured_fraction(client: TestClient):
    from scripts.collect import _warn_on_groq_quota

    register_workspace(client, email="q1@example.com", workspace_name="Q1")
    workspace_id = _workspace(client)

    llm.record_quota_headers({"x-ratelimit-remaining-requests": "500", "x-ratelimit-limit-requests": "1000"})
    with SessionLocal() as db:
        asyncio.run(_warn_on_groq_quota(db, workspace_id))
    assert _alerts(workspace_id, GROQ_QUOTA.key) == []

    llm.record_quota_headers({"x-ratelimit-remaining-requests": "50", "x-ratelimit-limit-requests": "1000"})
    with SessionLocal() as db:
        asyncio.run(_warn_on_groq_quota(db, workspace_id))
    raised = _alerts(workspace_id, GROQ_QUOTA.key)
    assert len(raised) == 1
    assert "5٪" in raised[0].body


def test_an_unconfigured_or_silent_groq_never_warns(client: TestClient):
    from scripts.collect import _warn_on_groq_quota

    register_workspace(client, email="q2@example.com", workspace_name="Q2")
    workspace_id = _workspace(client)

    with SessionLocal() as db:
        asyncio.run(_warn_on_groq_quota(db, workspace_id))

    assert _alerts(workspace_id, GROQ_QUOTA.key) == []


# --- idea 158: the backup says so, either way ------------------------------


def _report_run(client: TestClient, name: str, conclusion: str):
    key = client.post("/auth/api-keys", json={"name": f"{name}-reporter"}).json()["key"]
    fresh = TestClient(client.app)
    return fresh.post(
        "/status/workflow-runs",
        json={"name": name, "conclusion": conclusion},
        headers={"Authorization": f"Bearer {key}"},
    )


def test_a_successful_backup_is_confirmed_not_silent(client: TestClient):
    register_workspace(client, email="b1@example.com", workspace_name="B1")
    workspace_id = _workspace(client)

    assert _report_run(client, "backup", "success").status_code == 201

    raised = _alerts(workspace_id, BACKUP_RESULT.key)
    assert len(raised) == 1
    assert "ناجحة" in raised[0].title


def test_a_failed_backup_also_sends(client: TestClient):
    """Sending on both outcomes is what makes the message's own absence
    mean something: no message at all means the run never happened."""
    register_workspace(client, email="b2@example.com", workspace_name="B2")
    workspace_id = _workspace(client)

    _report_run(client, "backup", "failure")

    raised = _alerts(workspace_id, BACKUP_RESULT.key)
    assert len(raised) == 1
    assert "فشل" in raised[0].title


def test_other_workflows_reach_the_board_and_stop_there(client: TestClient):
    """One message per workflow per run would be several an hour."""
    register_workspace(client, email="b3@example.com", workspace_name="B3")
    workspace_id = _workspace(client)

    _report_run(client, "collector", "failure")
    _report_run(client, "vitality", "success")

    assert _alerts(workspace_id, BACKUP_RESULT.key) == []
    assert len(client.get("/status").json()["latest_runs"]) == 2


def test_the_backup_workflow_reports_the_name_the_alert_matches():
    """Guards the guard. The alert compares against a literal name; a
    rename in the workflow would silently disable it while every test
    below still passed, because they all report the name themselves."""
    from app.routers.status import BACKUP_WORKFLOW_NAME

    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/backup.yml").read_text(encoding="utf-8"))

    assert workflow["jobs"]["report"]["with"]["name"] == BACKUP_WORKFLOW_NAME


# --- idea 163: a domain corrected over and over ----------------------------


def _add_and_correct(client: TestClient, url: str, category: str) -> None:
    client.post("/links", json={"text": url})
    link_id = client.get("/links", params={"q": url}).json()["items"][0]["id"]
    assert client.patch(f"/links/{link_id}", json={"category": category}).status_code == 200


def test_three_corrections_on_one_domain_raise_the_alert(client: TestClient):
    register_workspace(client, email="u1@example.com", workspace_name="U1")
    workspace_id = _workspace(client)
    _enable(client, UNSTABLE_CATEGORY.key)

    for index in range(3):
        _add_and_correct(client, f"https://shaky.example/{index}.bin", "games")

    raised = _alerts(workspace_id, UNSTABLE_CATEGORY.key)
    assert len(raised) == 1
    assert "shaky.example" in raised[0].body
    assert "قاعدة مفقودة" in raised[0].body


def test_two_corrections_are_a_coincidence_not_a_pattern(client: TestClient):
    register_workspace(client, email="u2@example.com", workspace_name="U2")
    workspace_id = _workspace(client)
    _enable(client, UNSTABLE_CATEGORY.key)

    for index in range(2):
        _add_and_correct(client, f"https://stable.example/{index}.bin", "games")

    assert _alerts(workspace_id, UNSTABLE_CATEGORY.key) == []


def test_the_alert_fires_at_the_crossing_and_not_on_every_correction_after(client: TestClient):
    """An alert that repeats is an alert that gets muted — and muting it
    costs the next, different alert its audience too."""
    register_workspace(client, email="u3@example.com", workspace_name="U3")
    workspace_id = _workspace(client)
    _enable(client, UNSTABLE_CATEGORY.key)

    for index in range(7):
        _add_and_correct(client, f"https://noisy.example/{index}.bin", "games")

    assert len(_alerts(workspace_id, UNSTABLE_CATEGORY.key)) == 1


def test_mixed_targets_are_reported_as_mixed_content(client: TestClient):
    """One target category means a missing rule; several mean the site
    genuinely hosts different things. They need different fixes, so the
    alert distinguishes them instead of just counting."""
    register_workspace(client, email="u4@example.com", workspace_name="U4")
    workspace_id = _workspace(client)
    _enable(client, UNSTABLE_CATEGORY.key)

    for index, category in enumerate(("games", "music", "books_courses")):
        _add_and_correct(client, f"https://mixed.example/{index}.bin", category)

    body = _alerts(workspace_id, UNSTABLE_CATEGORY.key)[0].body
    assert "مختلط المحتوى" in body
    assert body.count("  • ") == 3


def test_one_workspace_corrections_do_not_count_toward_anothers(client: TestClient):
    register_workspace(client, email="u5@example.com", workspace_name="U5")
    victim = _workspace(client)
    _enable(client, UNSTABLE_CATEGORY.key)
    for index in range(2):
        _add_and_correct(client, f"https://shared.example/{index}.bin", "games")
    client.post("/auth/logout")

    register_workspace(client, email="u6@example.com", workspace_name="U6")
    other = _workspace(client)
    _enable(client, UNSTABLE_CATEGORY.key)
    for index in range(3):
        _add_and_correct(client, f"https://shared.example/{index}.bin", "games")

    assert len(_alerts(other, UNSTABLE_CATEGORY.key)) == 1
    assert _alerts(victim, UNSTABLE_CATEGORY.key) == []


def test_re_selecting_the_same_category_is_not_a_correction(client: TestClient):
    """No-ops must not accumulate toward the threshold, or the alert
    measures clicking rather than instability."""
    register_workspace(client, email="u7@example.com", workspace_name="U7")
    workspace_id = _workspace(client)
    _enable(client, UNSTABLE_CATEGORY.key)

    client.post("/links", json={"text": "https://noop.example/a.bin"})
    link_id = client.get("/links").json()["items"][0]["id"]
    for _ in range(5):
        client.patch(f"/links/{link_id}", json={"category": "games"})

    with SessionLocal() as db:
        assert db.query(ClassificationFeedback).count() == 1
    assert _alerts(workspace_id, UNSTABLE_CATEGORY.key) == []


def test_every_correction_records_the_domain_it_grouped_by(client: TestClient):
    register_workspace(client, email="u8@example.com", workspace_name="U8")
    _add_and_correct(client, "https://www.grouped.example/a.bin", "games")

    with SessionLocal() as db:
        assert db.query(ClassificationFeedback).one().domain == "grouped.example"


# --- idea 151: the week in three numbers -----------------------------------


def _seed_link(workspace_id: int, channel_id: int, *, url: str, age_days: int = 0, dead_days: int | None = None):
    from datetime import timedelta

    from app.timeutil import utcnow

    with SessionLocal() as db:
        link = Link(
            workspace_id=workspace_id,
            channel_id=channel_id,
            message_id=0,
            url=url,
            url_hash=url,
            domain="seed.example",
            category="other",
            created_at=utcnow() - timedelta(days=age_days),
        )
        if dead_days is not None:
            link.is_alive = False
            link.last_checked_at = utcnow() - timedelta(days=dead_days)
        db.add(link)
        db.commit()


def test_the_digest_counts_only_the_last_week(client: TestClient):
    from scripts.weekly_digest import build

    register_workspace(client, email="d1@example.com", workspace_name="D1")
    workspace_id = _workspace(client)
    channel_id = client.post("/channels", json={"tg_channel_id": "1", "username": "src"}).json()["id"]

    _seed_link(workspace_id, channel_id, url="https://seed.example/new.bin", age_days=1)
    _seed_link(workspace_id, channel_id, url="https://seed.example/old.bin", age_days=30)

    with SessionLocal() as db:
        digest = build(db, workspace_id)

    assert digest.new_links == 1


def test_dead_links_are_counted_by_when_they_were_confirmed(client: TestClient):
    """Not by when they died, which the data does not record: a check
    confirms, it does not witness."""
    from scripts.weekly_digest import build

    register_workspace(client, email="d2@example.com", workspace_name="D2")
    workspace_id = _workspace(client)
    channel_id = client.post("/channels", json={"tg_channel_id": "1", "username": "src"}).json()["id"]

    _seed_link(workspace_id, channel_id, url="https://seed.example/x.bin", age_days=90, dead_days=2)
    _seed_link(workspace_id, channel_id, url="https://seed.example/y.bin", age_days=90, dead_days=40)

    with SessionLocal() as db:
        digest = build(db, workspace_id)

    assert digest.confirmed_dead == 1


def test_a_channel_that_stopped_posting_is_named(client: TestClient):
    from datetime import timedelta

    from app.timeutil import utcnow
    from scripts.weekly_digest import build

    register_workspace(client, email="d3@example.com", workspace_name="D3")
    workspace_id = _workspace(client)
    quiet = client.post("/channels", json={"tg_channel_id": "1", "username": "quiet"}).json()["id"]
    busy = client.post("/channels", json={"tg_channel_id": "2", "username": "busy"}).json()["id"]

    with SessionLocal() as db:
        for channel_id in (quiet, busy):
            channel = db.get(Channel, channel_id)
            channel.created_at = utcnow() - timedelta(days=60)
        db.commit()

    _seed_link(workspace_id, quiet, url="https://seed.example/old.bin", age_days=40)
    _seed_link(workspace_id, busy, url="https://seed.example/fresh.bin", age_days=1)

    with SessionLocal() as db:
        digest = build(db, workspace_id)

    assert digest.silent_channels == ["quiet"]
    assert digest.active_channels == 2


def test_a_channel_added_yesterday_is_not_silent_yet(client: TestClient):
    """Silence is judged from when the channel got its chance to speak."""
    from scripts.weekly_digest import build

    register_workspace(client, email="d4@example.com", workspace_name="D4")
    workspace_id = _workspace(client)
    client.post("/channels", json={"tg_channel_id": "1", "username": "brand-new"})

    with SessionLocal() as db:
        digest = build(db, workspace_id)

    assert digest.silent_channels == []


def test_the_manual_bucket_is_never_reported_as_silent(client: TestClient):
    """It has no upstream to go quiet: "you have not pasted anything in a
    fortnight" is not news about the collection."""
    from datetime import timedelta

    from app.timeutil import utcnow
    from scripts.weekly_digest import build

    register_workspace(client, email="d5@example.com", workspace_name="D5")
    workspace_id = _workspace(client)
    client.post("/links", json={"text": "https://example.com/manual.pdf"})

    with SessionLocal() as db:
        for channel in db.query(Channel).filter(Channel.workspace_id == workspace_id):
            channel.created_at = utcnow() - timedelta(days=60)
        db.commit()
        digest = build(db, workspace_id)

    assert digest.silent_channels == []
    assert digest.active_channels == 0


def test_a_quiet_week_still_says_so(client: TestClient):
    """A digest that only arrives when something happened cannot be told
    apart from one that stopped arriving."""
    from scripts.weekly_digest import Digest, render

    body = render(Digest())

    assert "أسبوع بلا تغيير" in body


def test_the_digest_is_off_until_asked_for(client: TestClient):
    from scripts.weekly_digest import run

    register_workspace(client, email="d6@example.com", workspace_name="D6")
    workspace_id = _workspace(client)

    asyncio.run(run(workspace_id))
    assert _alerts(workspace_id, WEEKLY_DIGEST.key) == []

    _enable(client, WEEKLY_DIGEST.key)
    asyncio.run(run(workspace_id))
    assert len(_alerts(workspace_id, WEEKLY_DIGEST.key)) == 1


# --- the exit criterion ----------------------------------------------------


def test_every_declared_alert_type_has_something_that_raises_it():
    """Phase 9b in one assertion.

    A type in the catalogue with no trigger is a switch for a message that
    can never arrive, which reads as a promise the system does not keep.
    Searched by constant name across the shipped source rather than by a
    hand-maintained list, so a type added later without a trigger fails
    here rather than being noticed by its silence.
    """
    sources = [
        path
        for directory in ("app", "scripts")
        for path in (REPO_ROOT / directory).rglob("*.py")
        if path.name != "alerts.py"
    ]
    corpus = "\n".join(path.read_text(encoding="utf-8") for path in sources)

    names = {alert.key: None for alert in ALERT_TYPES}
    import app.alerts as alerts_module

    for attribute in dir(alerts_module):
        value = getattr(alerts_module, attribute)
        if isinstance(value, type(next(iter(ALERT_TYPES)))) and value.key in names:
            names[value.key] = attribute

    missing = [key for key, constant in names.items() if constant is None or f"{constant}.key" not in corpus]

    assert not missing, f"declared but never raised: {missing}"
