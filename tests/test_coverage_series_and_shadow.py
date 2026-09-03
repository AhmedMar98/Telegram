"""The operational series (§47.1) and shadow classification (§47.3).

Two properties carry the weight here:

``test_a_run_that_did_nothing_still_writes_a_row`` — a gap in the series
and a run that found nothing are indistinguishable afterwards, and they
need opposite responses.

``test_shadow_mode_never_writes`` — shadow that mutates is not shadow, it
is a deployment. Everything else in this file is arithmetic; that one is
the safety property.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app import coverage, shadow
from app.database import SessionLocal
from app.ingest import IngestSummary
from app.models import Channel, CoverageSnapshot, Link, Message, Workspace
from app.timeutil import utcnow
from tests.conftest import register_workspace


@pytest.fixture()
def workspace_id() -> int:
    db = SessionLocal()
    try:
        workspace = Workspace(name="Series")
        db.add(workspace)
        db.commit()
        return workspace.id
    finally:
        db.close()


def _snapshot(workspace_id: int, *, summary=None, run_id: str = "run-1") -> CoverageSnapshot:
    db = SessionLocal()
    try:
        return coverage.record_snapshot(db, workspace_id, run_id=run_id, started_at=utcnow(), summary=summary)
    finally:
        db.close()


# --- the series -------------------------------------------------------------


def test_a_run_that_did_nothing_still_writes_a_row(workspace_id):
    """Otherwise a stopped collector and an idle one look identical."""
    _snapshot(workspace_id)

    db = SessionLocal()
    try:
        rows = coverage.history(db, workspace_id)
        assert len(rows) == 1
        assert rows[0].links_stored == 0
        assert rows[0].sources_expected == 0
    finally:
        db.close()


def test_the_run_counters_come_from_the_run_not_from_the_table(workspace_id):
    """``messages_seen`` counts what the run was offered; ``processed`` is
    the subset it worked on. The difference is the overlap between the two
    readers, which is a designed behaviour and worth being able to see."""
    summary = IngestSummary(stored=7, duplicates=3, scanned=40, already_processed=12)

    row = _snapshot(workspace_id, summary=summary)

    assert row.messages_seen == 40
    assert row.messages_processed == 28, "40 offered minus 12 already done"
    assert row.links_found == 10, "stored plus duplicates"
    assert row.links_stored == 7
    assert row.duplicate_occurrences == 3


def test_lag_is_reported_as_a_distribution_not_an_average(workspace_id):
    """Nine fresh sources and one a day behind average out to "fine"."""
    db = SessionLocal()
    try:
        now = utcnow()
        for index in range(10):
            channel = Channel(workspace_id=workspace_id, tg_channel_id=f"-100{index}", username=f"s{index}")
            db.add(channel)
            db.flush()
            age = timedelta(days=1) if index == 9 else timedelta(minutes=5)
            db.add(
                Message(
                    workspace_id=workspace_id,
                    channel_id=channel.id,
                    tg_message_id=index + 1,
                    posted_at=now - age,
                )
            )
        db.commit()
    finally:
        db.close()

    row = _snapshot(workspace_id)

    assert row.collection_lag_p50 < 3_600, "a typical source is fresh"
    assert row.collection_lag_p95 > 80_000, "and the tail is a day behind — visible, not averaged away"


def test_the_trend_needs_more_than_one_reading(workspace_id):
    db = SessionLocal()
    try:
        assert coverage.trend(coverage.history(db, workspace_id)) == "unknown"
    finally:
        db.close()


def test_a_falling_coverage_rate_is_reported_as_degrading(workspace_id):
    """99.2 → 98.7 → 94.1 is the case this exists for: every reading looks
    acceptable and the direction is the signal."""
    db = SessionLocal()
    try:
        base = utcnow() - timedelta(hours=12)
        for index, succeeded in enumerate([100, 99, 98, 70, 60, 55]):
            db.add(
                CoverageSnapshot(
                    workspace_id=workspace_id,
                    run_id=f"r{index}",
                    started_at=base + timedelta(hours=index),
                    finished_at=base + timedelta(hours=index, minutes=1),
                    sources_due=100,
                    sources_succeeded=succeeded,
                )
            )
        db.commit()
        assert coverage.trend(coverage.history(db, workspace_id)) == "degrading"
    finally:
        db.close()


def test_old_snapshots_are_pruned_on_write(workspace_id):
    db = SessionLocal()
    try:
        db.add(
            CoverageSnapshot(
                workspace_id=workspace_id,
                run_id="ancient",
                started_at=utcnow() - timedelta(days=400),
                finished_at=utcnow() - timedelta(days=400),
            )
        )
        db.commit()
    finally:
        db.close()

    _snapshot(workspace_id, run_id="fresh")

    db = SessionLocal()
    try:
        assert [row.run_id for row in coverage.history(db, workspace_id)] == ["fresh"]
    finally:
        db.close()


def test_the_history_endpoint_carries_the_trend(client):
    register_workspace(client, email="series@example.com", workspace_name="SeriesWS")

    body = client.get("/status/coverage/history").json()

    assert body["snapshots"] == []
    assert body["trend"] == "unknown"


# --- shadow mode ------------------------------------------------------------


def _link(db, workspace_id: int, channel_id: int, url: str, category: str, by: str = "rules-v2") -> Link:
    row = Link(
        workspace_id=workspace_id,
        channel_id=channel_id,
        message_id=1,
        url=url,
        url_hash=url,
        domain="example.com",
        category=category,
        classified_by=by,
    )
    db.add(row)
    db.flush()
    return row


@pytest.fixture()
def corpus(workspace_id: int) -> tuple[int, int]:
    db = SessionLocal()
    try:
        channel = Channel(workspace_id=workspace_id, tg_channel_id="-100c", username="c", title="قناة")
        db.add(channel)
        db.flush()
        # One row whose stored category is what today's rules produce, and
        # one carrying a category from a version that no longer exists.
        _link(db, workspace_id, channel.id, "https://example.com/x.apk", "software_apps")
        _link(db, workspace_id, channel.id, "https://example.com/y.pdf", "movies_series", by="rules-v1")
        db.commit()
        return workspace_id, channel.id
    finally:
        db.close()


def test_shadow_mode_finds_the_rows_todays_rules_would_change(corpus):
    workspace_id, _ = corpus
    db = SessionLocal()
    try:
        report = shadow.compare(db, workspace_id)
    finally:
        db.close()

    assert report.compared == 2
    assert report.agreed == 1
    assert report.disagreed == 1
    assert report.disagreement_rate == 0.5
    assert report.transitions[("movies_series", "books_courses")] == 1
    assert report.samples[0].stored_by == "rules-v1"


def test_shadow_mode_never_writes(corpus):
    """The safety property. Sabotage: make ``compare`` assign the proposed
    category and this fails — a shadow run that mutates is a deployment
    nobody approved.
    """
    workspace_id, _ = corpus
    db = SessionLocal()
    try:
        before = {row.id: (row.category, row.classified_by) for row in db.query(Link).all()}
        shadow.compare(db, workspace_id)
        db.expire_all()
        after = {row.id: (row.category, row.classified_by) for row in db.query(Link).all()}
    finally:
        db.close()

    assert after == before


def test_a_human_verdict_is_excluded_from_the_comparison(workspace_id):
    """A candidate gets no opinion about a verdict it may not overwrite —
    and counting those as disagreements would make every rule change look
    worse the more a person had curated."""
    db = SessionLocal()
    try:
        channel = Channel(workspace_id=workspace_id, tg_channel_id="-100h", username="h")
        db.add(channel)
        db.flush()
        _link(db, workspace_id, channel.id, "https://example.com/z.apk", "music", by="manual")
        db.commit()

        report = shadow.compare(db, workspace_id)
    finally:
        db.close()

    assert report.compared == 0
    assert report.human_verdicts_skipped == 1
    assert report.disagreement_rate is None, "an empty comparison has no rate, not a perfect one"


def test_a_candidate_ruleset_can_be_compared_without_touching_telegram(corpus):
    """The reason this precedes a labelled benchmark: comparing two rule
    sets over the whole corpus costs zero Telegram requests, because the
    URL and its context are already stored."""
    workspace_id, _ = corpus

    def always_games(url: str, context: str | None, title: str | None) -> str:
        return "games"

    db = SessionLocal()
    try:
        report = shadow.compare(db, workspace_id, candidate=always_games)
    finally:
        db.close()

    assert report.compared == 2
    assert report.disagreed == 2
    assert dict(report.transitions) == {
        ("software_apps", "games"): 1,
        ("movies_series", "games"): 1,
    }


def test_the_drift_endpoint_returns_a_shortlist_not_a_corpus(client):
    register_workspace(client, email="drift@example.com", workspace_name="DriftWS")
    client.post("/links", json={"text": "https://example.com/a.apk"})

    body = client.get("/status/classification-drift").json()

    assert body["compared"] == 1
    assert body["disagreed"] == 0
    assert body["disagreement_rate"] == 0.0
    assert len(body["samples"]) <= shadow.SAMPLE_LIMIT


def test_a_run_with_nothing_to_collect_still_records_itself(workspace_id, monkeypatch):
    """The early-return paths in ``collect()`` write a snapshot too.

    Found by sabotage: removing that call broke nothing, because no test
    drove ``collect()`` itself — only ``_collect_channel``. A run that
    exits early is exactly the run worth recording, since "the collector
    stopped" and "the collector had nothing to do" are the two readings
    this series exists to tell apart.
    """
    import asyncio

    from scripts import collect as collector

    db = SessionLocal()
    try:
        db.add(
            Channel(
                workspace_id=workspace_id,
                tg_channel_id="-100nothing",
                username="nothing",
                is_active=False,  # nothing collectable
            )
        )
        db.commit()
    finally:
        db.close()

    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_SESSION_STRING", "session")
    monkeypatch.setenv("COLLECTOR_WORKSPACE_ID", str(workspace_id))
    # No account is provisioned, so collect() takes its "no active
    # collecting accounts" exit — before any Telegram connection.
    monkeypatch.setattr(collector, "_ensure_primary_account", lambda *args, **kwargs: None)

    asyncio.run(collector.collect())

    db = SessionLocal()
    try:
        rows = coverage.history(db, workspace_id)
        assert len(rows) == 1, "the run recorded itself despite doing nothing"
        assert rows[0].sources_expected == 0
        assert rows[0].links_stored == 0
    finally:
        db.close()
