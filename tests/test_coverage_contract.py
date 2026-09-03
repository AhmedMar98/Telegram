"""The measurement contract (§46): the denominators, not just the counters.

Most of these tests exist because a metric can be *computed* correctly and
still be *defined* wrongly, and the second failure is invisible. The two
that matter most:

``test_coverage_is_measured_against_what_was_due`` — a source correctly
skipped because it is not due yet must not lower the score, or the number
falls as the scheduler gets smarter.

``test_a_perfect_score_is_never_reported_for_an_empty_denominator`` — "of
the zero sources that were due, all zero succeeded" is 100% by arithmetic
and meaningless by construction. It is reported as absent.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app import coverage
from app.database import SessionLocal
from app.models import Channel, Link, Message, Workspace
from app.timeutil import utcnow
from tests.conftest import register_workspace


@pytest.fixture()
def workspace_id() -> int:
    db = SessionLocal()
    try:
        workspace = Workspace(name="Coverage")
        db.add(workspace)
        db.commit()
        return workspace.id
    finally:
        db.close()


def _source(db, workspace_id: int, tg_id: str, **kwargs) -> Channel:
    row = Channel(workspace_id=workspace_id, tg_channel_id=tg_id, username=tg_id.strip("-"), **kwargs)
    db.add(row)
    db.flush()
    return row


def _measure(workspace_id: int) -> coverage.Coverage:
    db = SessionLocal()
    try:
        return coverage.measure(db, workspace_id)
    finally:
        db.close()


# --- the denominators -------------------------------------------------------


def test_coverage_is_measured_against_what_was_due(workspace_id):
    """succeeded / due, never succeeded / expected.

    Ten sources, three of them due. All three succeed. Coverage is 100% —
    the seven correctly left alone are not failures, and counting them
    would make the number fall every time the scheduler got better at
    leaving quiet sources alone.
    """
    db = SessionLocal()
    try:
        fresh = utcnow()
        stale = utcnow() - timedelta(hours=5)
        for n in range(3):
            _source(db, workspace_id, f"-100due{n}", last_collected_at=stale, last_outcome=coverage.SUCCEEDED)
        for n in range(7):
            _source(db, workspace_id, f"-100new{n}", last_collected_at=fresh, last_outcome=coverage.SUCCEEDED)
        db.commit()
    finally:
        db.close()

    report = _measure(workspace_id)
    assert report.sources_expected == 10
    assert report.sources_due == 10, "nothing was excluded by policy, so all ten were due"
    assert report.sources_overdue == 3, "and three of them are late — reported separately"
    assert report.coverage_rate == 1.0, "all ten were read successfully"


def test_a_policy_skipped_source_leaves_both_halves_of_the_ratio(workspace_id):
    """The rule this contract exists for: a source deliberately left out
    of the cycle must not lower the score.

    It leaves the numerator *and* the denominator — subtracting it from
    only one is what produced a coverage rate of 3.33 in the first draft,
    caught by this file rather than by a reader.
    """
    db = SessionLocal()
    try:
        _source(db, workspace_id, "-100paused", last_outcome=coverage.SKIPPED)
        _source(db, workspace_id, "-100worked", last_outcome=coverage.SUCCEEDED)
        db.commit()
    finally:
        db.close()

    report = _measure(workspace_id)
    assert report.sources_expected == 2
    assert report.sources_skipped == 1
    assert report.sources_due == 1
    assert report.coverage_rate == 1.0


def test_a_perfect_score_is_never_reported_for_an_empty_denominator(workspace_id):
    """Nothing due is an absent score, not a perfect one."""
    db = SessionLocal()
    try:
        _source(db, workspace_id, "-100quiet", last_outcome=coverage.SKIPPED)
        db.commit()
    finally:
        db.close()

    report = _measure(workspace_id)
    assert report.sources_due == 0
    assert report.coverage_rate is None, "0/0 must not render as 100%"


def test_a_never_attempted_source_counts_as_due_but_not_as_failed(workspace_id):
    db = SessionLocal()
    try:
        _source(db, workspace_id, "-100virgin")
        db.commit()
    finally:
        db.close()

    report = _measure(workspace_id)
    assert (report.sources_expected, report.sources_due, report.sources_overdue) == (1, 1, 1)
    assert report.sources_attempted == 0
    assert report.sources_failed == 0
    assert report.coverage_rate == 0.0, "due and never read is zero coverage, not absent"


def test_synthetic_and_inactive_sources_are_outside_the_denominator(workspace_id):
    """The manual bucket is not a Telegram source and can never be due."""
    db = SessionLocal()
    try:
        _source(db, workspace_id, "manual")
        _source(db, workspace_id, "import:bookmarks.html")
        _source(db, workspace_id, "-100off", is_active=False)
        _source(db, workspace_id, "-100real")
        db.commit()
    finally:
        db.close()

    assert _measure(workspace_id).sources_expected == 1


# --- failure classification -------------------------------------------------


def test_failures_are_reported_by_kind_not_as_a_single_number(workspace_id):
    db = SessionLocal()
    try:
        kinds = (coverage.RATE_LIMITED, coverage.RATE_LIMITED, coverage.ACCESS_DENIED)
        for index, kind in enumerate(kinds):
            _source(
                db,
                workspace_id,
                f"-100fail{index}",
                last_outcome=coverage.FAILED,
                last_failure_kind=kind,
            )
        _source(db, workspace_id, "-100ok", last_outcome=coverage.SUCCEEDED)
        db.commit()
    finally:
        db.close()

    report = _measure(workspace_id)
    assert report.sources_failed == 3
    assert report.failures_by_kind == {coverage.RATE_LIMITED: 2, coverage.ACCESS_DENIED: 1}
    assert report.failure_rate == 0.75


def test_every_failure_kind_is_named_and_none_is_a_catch_all_for_the_rest(workspace_id):
    """``unknown`` exists so an unclassified failure is visible as
    unclassified, rather than folded into a neighbour it does not belong to."""
    assert coverage.UNKNOWN in coverage.FAILURE_KINDS
    assert len(set(coverage.FAILURE_KINDS)) == 8

    db = SessionLocal()
    try:
        _source(db, workspace_id, "-100weird", last_outcome=coverage.FAILED, last_failure_kind=None)
        db.commit()
    finally:
        db.close()
    assert _measure(workspace_id).failures_by_kind == {coverage.UNKNOWN: 1}


# --- lag is not duration ----------------------------------------------------


def test_lag_measures_the_age_of_the_data_not_the_length_of_the_job(workspace_id):
    """Freshness comes from Telegram's own ``posted_at``, so a job that
    ran for one second over month-old messages reports month-old lag."""
    db = SessionLocal()
    try:
        channel = _source(db, workspace_id, "-100lag", last_collected_at=utcnow())
        db.add(
            Message(
                workspace_id=workspace_id,
                channel_id=channel.id,
                tg_message_id=1,
                posted_at=utcnow() - timedelta(hours=30),
            )
        )
        db.commit()
    finally:
        db.close()

    report = _measure(workspace_id)
    assert report.collection_lag_seconds is not None
    assert report.collection_lag_seconds > 29 * 3600
    assert report.is_fresh is False
    # ...and how far behind the reader was when it read, which is a
    # different number about a different thing.
    assert report.watermark_lag_seconds is not None


def test_nothing_collected_yet_is_unknown_lag_not_infinite_lag(workspace_id):
    report = _measure(workspace_id)
    assert report.collection_lag_seconds is None
    assert report.is_fresh is None, "unknown must not render as stale"


# --- watermark integrity, reported apart from coverage ----------------------


def test_watermark_integrity_is_not_folded_into_the_success_rate(workspace_id):
    """A run can report every source succeeded and still have left a hole.

    So the regression count is reported beside coverage, never inside it —
    otherwise a single number would average away the one condition that
    means data is being lost.
    """
    db = SessionLocal()
    try:
        _source(
            db,
            workspace_id,
            "-100holed",
            last_collected_at=utcnow() - timedelta(hours=5),
            last_outcome=coverage.SUCCEEDED,
            watermark_regressions=2,
        )
        db.commit()
    finally:
        db.close()

    report = _measure(workspace_id)
    assert report.coverage_rate == 1.0, "coverage still reads perfect..."
    assert report.watermark.regressions == 1, "...while integrity says otherwise"
    assert report.watermark.sound is False
    assert report.gap_rate == 1.0


def test_an_ownership_conflict_counts_as_a_gap_not_as_a_plain_failure(workspace_id):
    db = SessionLocal()
    try:
        _source(
            db,
            workspace_id,
            "-100stolen",
            last_outcome=coverage.FAILED,
            last_failure_kind=coverage.ASSIGNMENT_ERROR,
        )
        db.commit()
    finally:
        db.close()

    report = _measure(workspace_id)
    assert report.watermark.ownership_conflicts == 1
    assert report.gap_rate == 1.0
    assert report.watermark.sound is False


def test_a_source_that_hit_the_cap_is_behind_but_not_unsound(workspace_id):
    """An unfinished window is not a hole: the next run continues it."""
    db = SessionLocal()
    try:
        _source(db, workspace_id, "-100backlog", last_outcome=coverage.SUCCEEDED, caught_up=False)
        db.commit()
    finally:
        db.close()

    report = _measure(workspace_id)
    assert report.watermark.behind == 1
    assert report.watermark.sound is True
    assert report.gap_rate == 0.0


# --- duplicates: three meanings, three numbers ------------------------------


def test_the_three_duplicate_rates_are_reported_separately(workspace_id):
    """One number would move for reasons the reader cannot infer: a channel
    reposting, two channels sharing a resource, and a re-read are three
    different events needing three different responses."""
    db = SessionLocal()
    try:
        first = _source(db, workspace_id, "-100a")
        second = _source(db, workspace_id, "-100b")
        for channel in (first, second):
            db.add(
                Link(
                    workspace_id=workspace_id,
                    channel_id=channel.id,
                    message_id=1,
                    url="https://example.com/shared",
                    url_hash="samehash",
                    domain="example.com",
                )
            )
        db.commit()
    finally:
        db.close()

    rates = _measure(workspace_id).duplicates
    assert rates.duplicate_link_occurrence == 0.5, "one of the two links is a repeat of the other"
    assert rates.duplicate_resource == 0.5, "and it is the cross-source signal, not waste"


# --- what the contract refuses to do ---------------------------------------


def test_the_contract_reports_no_classification_accuracy(workspace_id):
    """Collection correctness and classification accuracy are two levels.

    The second needs a labelled corpus that does not exist (§44.11), and
    mixing an unmeasurable number into a measurable one makes the whole
    figure unciteable. So no field here may mention accuracy.
    """
    fields = set(vars(_measure(workspace_id)))
    assert not [name for name in fields if "accuracy" in name or "precision" in name]


def test_the_endpoint_returns_the_contract(client):
    register_workspace(client, email="cov@example.com", workspace_name="CovWS")
    client.post("/channels", json={"tg_channel_id": "-1005000", "username": "measured"})

    body = client.get("/status/coverage").json()

    assert body["sources_expected"] == 1
    assert body["sources_due"] == 1
    assert body["coverage_rate"] == 0.0
    assert body["watermark_sound"] is True
    assert "failures_by_kind" in body
