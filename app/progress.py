"""The only place a watermark moves, and the rules it moves under.

A watermark is the answer to "where is it safe to resume from". Get it
wrong in the cheap direction and work is repeated; get it wrong in the
expensive direction and messages are skipped by every reader, forever,
with no counter disagreeing. That asymmetry is why every rule here fails
towards repeating work.

Four invariants, and none of them is left to a caller to remember:

**Monotonicity.** Progress never goes backwards. Enforced by a database
trigger (migration 0031), not by this module — a Python guard protects the
one path that goes through Python.

**Ownership.** An account that has lost the assignment does not write
progress. Checked against ``source_assignments`` — the authority — and
re-read at write time rather than trusted from when the run began, because
the whole point is to notice a change another process committed while this
one was working.

**Atomicity.** The watermark and the run's record of it move in one
transaction. A watermark that survived a crash its own evidence did not is
a number nobody can explain.

**Persist-then-advance.** The caller must have committed the data before
calling; this module records how far *stored* data reaches. Advancing on
"received" or "task finished" is how a crash turns into a silent gap.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import assignments
from app.models import Channel, SourceProgress
from app.timeutil import utcnow

logger = logging.getLogger(__name__)


class ProgressRefused(RuntimeError):
    """A write that would have broken an invariant. Carries which one."""


@dataclass(frozen=True)
class ProgressResult:
    """What a write actually did, in terms a caller can act on."""

    advanced: bool
    watermark: int
    #: Set when the write was refused rather than applied.
    refused: str | None = None


#: Refusal reasons, as strings because they are logged and shown.
STALE_ASSIGNMENT = "assignment changed during the run"
WOULD_REGRESS = "watermark would move backwards"


def get(db: Session, source_id: int, track: str = SourceProgress.LIVE) -> SourceProgress | None:
    return db.execute(
        select(SourceProgress).where(SourceProgress.source_id == source_id, SourceProgress.track == track)
    ).scalar_one_or_none()


def ensure(db: Session, channel: Channel, track: str = SourceProgress.LIVE) -> SourceProgress:
    """The progress row for this source and track, created if absent.

    A new row starts at the legacy watermark for the live track rather
    than at zero: ``Channel.last_message_id`` is where the old collector
    actually got to, and starting from zero would re-read the archive and
    call it new.
    """
    if track not in SourceProgress.TRACKS:
        raise ValueError(f"unknown progress track: {track!r}")
    row = get(db, channel.id, track)
    if row is not None:
        return row
    row = SourceProgress(
        workspace_id=channel.workspace_id,
        source_id=channel.id,
        track=track,
        current_watermark=(channel.last_message_id or 0) if track == SourceProgress.LIVE else 0,
        coverage_status=SourceProgress.UNKNOWN_COVERAGE,
    )
    db.add(row)
    db.flush()
    return row


def record_attempt(db: Session, channel: Channel, track: str = SourceProgress.LIVE) -> SourceProgress:
    """Note that something tried. Says nothing about whether it worked.

    Separate from progress on purpose: "tried five minutes ago and has not
    moved in a week" is the shape of a silent failure, and one timestamp
    cannot express it.
    """
    row = ensure(db, channel, track)
    moment = utcnow()
    row.last_attempt_at = moment
    row.updated_at = moment
    if track == SourceProgress.LIVE:
        # The legacy column derives from this row, so it moves with it. An
        # attempt that updated only the authority would leave the mirror
        # reading "never attempted" for a source being attempted right now
        # — and app.coverage computes "due" from that mirror.
        channel.last_attempt_at = row.last_attempt_at
    db.flush()
    return row


def advance(
    db: Session,
    channel: Channel,
    watermark: int,
    *,
    account_id: int | None,
    track: str = SourceProgress.LIVE,
    run_id: int | None = None,
    at: datetime | None = None,
    coverage: str | None = None,
) -> ProgressResult:
    """Move progress to ``watermark``, if every invariant allows it.

    ``account_id`` is the account claiming the progress; ``None`` means a
    path that needs no account (the public reader). Ownership is checked
    against the assignment table at this moment, not at the moment the run
    started.

    Returns rather than raises on a refusal: a stale worker finding out it
    has been replaced is an ordinary event in a fleet, not an error, and
    the work it already persisted stays persisted.
    """
    row = ensure(db, channel, track)
    moment = at or utcnow()
    row.last_attempt_at = moment

    if account_id is not None:
        holder = assignments.current_account_id(db, channel.id)
        if holder != account_id:
            # The new owner resumes from the watermark this worker was
            # about to move. Leaving it alone is what keeps the messages
            # between here and there readable by somebody.
            logger.warning(
                "source %s: refusing progress from account %s; assignment now holds %s",
                channel.id,
                account_id,
                holder,
            )
            row.updated_at = moment
            db.flush()
            return ProgressResult(advanced=False, watermark=row.current_watermark, refused=STALE_ASSIGNMENT)

    if watermark < row.current_watermark:
        logger.warning(
            "source %s: refusing watermark %s below current %s on track %s",
            channel.id,
            watermark,
            row.current_watermark,
            track,
        )
        # The measurement contract counts regressions per source (§46). The
        # count is fed from this refusal rather than from a second
        # comparison somewhere else, so the number can never disagree with
        # the guard that produced it. The event is authoritative; the
        # counter is derived from it.
        if track == SourceProgress.LIVE:
            channel.watermark_regressions = (channel.watermark_regressions or 0) + 1
        row.updated_at = moment
        db.flush()
        return ProgressResult(advanced=False, watermark=row.current_watermark, refused=WOULD_REGRESS)

    moved = watermark > row.current_watermark
    row.current_watermark = watermark
    row.updated_at = moment
    if moved:
        row.last_progress_at = moment
        row.last_run_id = run_id
    if coverage is not None:
        if coverage not in SourceProgress.COVERAGE_STATES:
            raise ValueError(f"unknown coverage status: {coverage!r}")
        row.coverage_status = coverage

    if track == SourceProgress.LIVE:
        _mirror_live(channel, row, moment)

    db.flush()
    return ProgressResult(advanced=moved, watermark=row.current_watermark)


#: How a three-valued coverage status projects onto the two-valued legacy
#: column. ``None`` for UNKNOWN_COVERAGE is the whole reason the projection
#: is lossy in this direction and not the other: the enum can say "cannot
#: tell", the boolean cannot, and NULL is the only honest rendering of it.
_CAUGHT_UP_PROJECTION: dict[str, bool | None] = {
    SourceProgress.NO_DETECTED_GAP: True,
    SourceProgress.DETECTED_GAP: False,
    SourceProgress.UNKNOWN_COVERAGE: None,
}


def _mirror_live(channel: Channel, row: SourceProgress, moment: datetime) -> None:
    """Keep every legacy column on ``channels`` in step with the live track.

    Four columns, one writer. ``last_message_id`` and ``last_collected_at``
    are read by the scheduled collector, the dashboard and the API;
    ``last_attempt_at`` and ``caught_up`` are read by the measurement
    contract in ``app.coverage``. All four are *derived* — the live
    ``source_progress`` row is the authority — and they are written here
    rather than by their readers so that no second path can make one of
    them disagree with the row it is supposed to reflect.

    The arrangement is the one ``channels.account_id`` already has: a
    mirror with a single writer, not a second authority.
    """
    if row.current_watermark > (channel.last_message_id or 0):
        channel.last_message_id = row.current_watermark
    # Stamped even when nothing moved: this answers "when did anything last
    # look at this dialog", which is what the collector's rotation order
    # needs. Stamping only on a non-empty run would park every quiet dialog
    # at the front of the queue forever and starve the rest.
    channel.last_collected_at = moment
    channel.last_attempt_at = row.last_attempt_at
    channel.caught_up = _CAUGHT_UP_PROJECTION[row.coverage_status]


def mirror_disagreements(db: Session, workspace_id: int) -> list[tuple[int, int, int]]:
    """Sources where the legacy column and the live track disagree.

    Should be empty. Exists so the property is asserted rather than
    assumed, and so a deployment that predates this module can be checked.
    Returns ``(source_id, mirror, authoritative)``.
    """
    out: list[tuple[int, int, int]] = []
    rows = db.query(Channel).filter(Channel.workspace_id == workspace_id).all()
    for channel in rows:
        progress = get(db, channel.id, SourceProgress.LIVE)
        if progress is None:
            continue
        if (channel.last_message_id or 0) != progress.current_watermark:
            out.append((channel.id, channel.last_message_id or 0, progress.current_watermark))
    return out
