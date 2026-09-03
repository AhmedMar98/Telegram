"""The compatibility columns must never disagree with what decides them.

Two branches of work arrived at the same facts twice. The measurement
contract (§46) put them on ``channels``; the collection runtime (§50) put
them in ``source_progress`` and ``collection_runs``. The reconciliation
kept both — the columns because ``app.coverage`` reads them, the tables
because they are the authority — and made each column a copy with exactly
one writer.

That arrangement is only worth anything if it is checked. A copy with a
single writer is a design; a copy proved to track its source is a fact.
Each test below moves the authority and asserts the copy followed, and
each names the sabotage that breaks it.

The failure this prevents is specific and quiet: ``app.coverage`` computes
the number an operator uses to decide whether collection is healthy. If a
second writer set ``caught_up`` or ``last_outcome`` behind the runtime's
back, that number would be wrong in a way nothing else would show.
"""

from __future__ import annotations

import pytest

from app import assignments, progress
from app.database import SessionLocal
from app.models import Channel, SourceProgress, TelegramAccount, Workspace


@pytest.fixture
def source():
    with SessionLocal() as db:
        workspace = Workspace(name="authority")
        db.add(workspace)
        db.flush()
        account = TelegramAccount(workspace_id=workspace.id, label="a", session_string="x")
        db.add(account)
        db.flush()
        channel = Channel(workspace_id=workspace.id, tg_channel_id="-100777", username="src")
        db.add(channel)
        db.flush()
        assignments.assign(db, channel, account.id, reason="fixture")
        db.commit()
        return workspace.id, account.id, channel.id


def _channel(channel_id: int) -> Channel:
    with SessionLocal() as db:
        return db.get(Channel, channel_id)


# --- last_attempt_at -------------------------------------------------------


def test_recording_an_attempt_moves_the_legacy_column_with_the_row(source):
    """Sabotage: drop the mirror line in ``progress.record_attempt`` and
    coverage reads "never attempted" for a source being attempted now."""
    _, _, channel_id = source
    with SessionLocal() as db:
        row = progress.record_attempt(db, db.get(Channel, channel_id))
        db.commit()
        authoritative = row.last_attempt_at

    assert authoritative is not None
    assert _channel(channel_id).last_attempt_at == authoritative


def test_advancing_progress_also_moves_the_legacy_attempt_column(source):
    _, account_id, channel_id = source
    with SessionLocal() as db:
        progress.advance(db, db.get(Channel, channel_id), 40, account_id=account_id)
        db.commit()
        authoritative = progress.get(db, channel_id).last_attempt_at

    assert _channel(channel_id).last_attempt_at == authoritative


# --- caught_up vs coverage_status -----------------------------------------


@pytest.mark.parametrize(
    ("coverage_status", "expected"),
    [
        (SourceProgress.NO_DETECTED_GAP, True),
        (SourceProgress.DETECTED_GAP, False),
        # The whole reason the projection is lossy in this direction: a
        # boolean cannot say "cannot tell", and NULL is the only honest
        # rendering of it. Sabotage: map UNKNOWN_COVERAGE to False and
        # coverage.py counts every unmeasured source as "behind".
        (SourceProgress.UNKNOWN_COVERAGE, None),
    ],
)
def test_caught_up_is_a_faithful_projection_of_coverage_status(source, coverage_status, expected):
    _, account_id, channel_id = source
    with SessionLocal() as db:
        progress.advance(db, db.get(Channel, channel_id), 10, account_id=account_id, coverage=coverage_status)
        db.commit()

    assert _channel(channel_id).caught_up is expected


def test_the_enum_keeps_information_the_boolean_cannot(source):
    """ "never read" and "read but cannot tell" are both NULL on the copy.

    Stated as a test so nobody reads ``caught_up IS NULL`` as a gap claim:
    the authority separates the two, the projection does not.
    """
    _, account_id, channel_id = source
    assert _channel(channel_id).caught_up is None, "never read"

    with SessionLocal() as db:
        progress.advance(
            db,
            db.get(Channel, channel_id),
            10,
            account_id=account_id,
            coverage=SourceProgress.UNKNOWN_COVERAGE,
        )
        db.commit()
        assert progress.get(db, channel_id).coverage_status == SourceProgress.UNKNOWN_COVERAGE

    assert _channel(channel_id).caught_up is None, "read, but coverage unknown"


# --- watermark_regressions -------------------------------------------------


def test_the_regression_counter_is_fed_by_the_refusal_that_produced_it(source):
    """One verdict, one count.

    Sabotage: increment the counter from a second comparison anywhere else
    and the two can disagree — which is exactly the shape of the problem
    the branches created by measuring the same thing twice.
    """
    _, account_id, channel_id = source
    with SessionLocal() as db:
        progress.advance(db, db.get(Channel, channel_id), 90, account_id=account_id)
        db.commit()
    assert _channel(channel_id).watermark_regressions == 0

    with SessionLocal() as db:
        result = progress.advance(db, db.get(Channel, channel_id), 30, account_id=account_id)
        db.commit()

    assert result.refused == progress.WOULD_REGRESS
    assert _channel(channel_id).watermark_regressions == 1
    with SessionLocal() as db:
        assert progress.get(db, channel_id).current_watermark == 90, "and the authority held"


def test_a_refused_regression_on_the_historical_track_does_not_touch_the_live_counter(source):
    """The counter is about the live watermark; the tracks are separate."""
    _, account_id, channel_id = source
    with SessionLocal() as db:
        channel = db.get(Channel, channel_id)
        progress.advance(db, channel, 50, account_id=account_id, track=SourceProgress.HISTORICAL)
        progress.advance(db, channel, 20, account_id=account_id, track=SourceProgress.HISTORICAL)
        db.commit()
    assert _channel(channel_id).watermark_regressions == 0


# --- account_id ------------------------------------------------------------


def test_the_assignment_mirror_still_tracks_the_authority(source):
    """Phase 1's arrangement, re-asserted after the branches were joined."""
    workspace_id, _, channel_id = source
    with SessionLocal() as db:
        other = TelegramAccount(workspace_id=workspace_id, label="b", session_string="x")
        db.add(other)
        db.flush()
        assignments.assign(db, db.get(Channel, channel_id), other.id, reason="rebalance")
        db.commit()
        authoritative = assignments.current_account_id(db, channel_id)

    assert _channel(channel_id).account_id == authoritative
    with SessionLocal() as db:
        assert assignments.mirror_disagreements(db, workspace_id) == []


def test_no_source_has_a_mirror_that_drifted(source):
    """The sweep coverage.py would need if it ever stopped trusting these."""
    workspace_id, account_id, channel_id = source
    with SessionLocal() as db:
        progress.advance(db, db.get(Channel, channel_id), 15, account_id=account_id)
        db.commit()

    with SessionLocal() as db:
        assert progress.mirror_disagreements(db, workspace_id) == []
