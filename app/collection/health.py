"""Is collection actually working? Answered without a single ratio.

A runtime that reports "97% success" is reporting nothing: the 3% is
where all four different problems live, and each of them needs a
different response. So this module produces *findings* — named
conditions, each with the rows that prove it — and no aggregate.

Three questions, three answers, none of them interchangeable:

**Is anything trying?**  ``stalled`` — a source that is assigned to an
active account and has not been attempted within the expected interval.
Nobody is working it, whatever the run history says.

**Is trying producing anything?**  ``silent_failures`` — a source that is
being attempted and is not moving. This is the failure this phase exists
to catch: every counter looks alive, runs open and close, and the
watermark has not moved in a week. ``last_attempt_at`` and
``last_progress_at`` are separate columns for exactly this reason; one
timestamp cannot express it.

**Do we have any right to claim coverage?**  ``coverage_gaps`` — a source
whose progress claims a position no stored evidence supports.

What this module refuses to claim
---------------------------------
It never reports "no gaps". Telegram message ids are not contiguous —
deleted messages, service messages and channel migrations all leave holes
that are not gaps — so "id 41 is missing" is not evidence of anything.
``NO_DETECTED_GAP`` therefore means *no gap was detected*, and this file
is careful never to let that be read as *no gap exists*. A finding here
is a positive statement backed by a row; the absence of findings is not
a statement at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Channel,
    CollectionRun,
    Message,
    SourceAssignment,
    SourceProgress,
    TelegramAccount,
)
from app.timeutil import utcnow

#: A source assigned to an active account and not attempted within this
#: window has nobody on it. Deliberately several multiples of the default
#: cycle pause: a single missed sweep is a busy fleet, not a fault.
STALL_AFTER = timedelta(hours=6)

#: Attempted repeatedly across at least this span without the watermark
#: moving. Long enough that a genuinely quiet channel is not reported —
#: which is why the finding also requires attempts to have happened.
SILENT_AFTER = timedelta(days=3)


@dataclass(frozen=True)
class Finding:
    """One named condition on one source, with what proves it."""

    source_id: int
    kind: str
    detail: str

    #: Nothing is trying.
    STALLED = "STALLED"
    #: Trying, and getting nowhere.
    SILENT_FAILURE = "SILENT_FAILURE"
    #: Progress claims a position the stored data does not support.
    COVERAGE_GAP = "COVERAGE_GAP"
    #: A run has been RUNNING with no heartbeat for longer than the
    #: recovery window, and no recovery sweep has closed it.
    ABANDONED_RUN = "ABANDONED_RUN"


def stalled(db: Session, workspace_id: int, *, now: datetime | None = None) -> list[Finding]:
    """Assigned to a usable account, and not attempted recently.

    Requires the account to be ACTIVE: a source assigned to a disabled
    account is not stalled, it is *blocked*, and the account's own state
    already says so. Reporting it here would bury the one finding that
    means "something is broken and nothing says why" under a pile of
    findings that have an explanation already.
    """
    moment = now or utcnow()
    cutoff = moment - STALL_AFTER
    rows = db.execute(
        select(Channel.id, SourceProgress.last_attempt_at)
        .join(SourceAssignment, SourceAssignment.source_id == Channel.id)
        .join(TelegramAccount, TelegramAccount.id == SourceAssignment.account_id)
        .outerjoin(
            SourceProgress,
            (SourceProgress.source_id == Channel.id) & (SourceProgress.track == SourceProgress.LIVE),
        )
        .where(
            Channel.workspace_id == workspace_id,
            SourceAssignment.released_at.is_(None),
            TelegramAccount.state == TelegramAccount.ACTIVE,
        )
    ).all()

    out: list[Finding] = []
    for source_id, last_attempt in rows:
        if last_attempt is None:
            out.append(Finding(source_id, Finding.STALLED, "assigned to an active account and never attempted"))
        elif last_attempt < cutoff:
            out.append(
                Finding(
                    source_id,
                    Finding.STALLED,
                    f"last attempted {last_attempt.isoformat()}, older than {STALL_AFTER}",
                )
            )
    return out


def silent_failures(db: Session, workspace_id: int, *, now: datetime | None = None) -> list[Finding]:
    """Being attempted, and not moving.

    The condition is precise on purpose: attempted **recently**, and no
    progress for a long time. A channel that genuinely has nothing new
    fails the first half after its first quiet interval only if nothing
    attempts it — and something is attempting it, which is the point.

    A source that has never progressed *and* has never been attempted is
    not reported here; that is ``stalled``'s finding, and reporting one
    condition twice makes both cheaper to ignore.
    """
    moment = now or utcnow()
    attempt_floor = moment - STALL_AFTER
    progress_ceiling = moment - SILENT_AFTER
    rows = db.execute(
        select(
            SourceProgress.source_id,
            SourceProgress.last_attempt_at,
            SourceProgress.last_progress_at,
            SourceProgress.created_at,
        ).where(
            SourceProgress.workspace_id == workspace_id,
            SourceProgress.track == SourceProgress.LIVE,
            SourceProgress.last_attempt_at.is_not(None),
            SourceProgress.last_attempt_at >= attempt_floor,
        )
    ).all()

    out: list[Finding] = []
    for source_id, last_attempt, last_progress, created in rows:
        # A row created five minutes ago has not "failed silently for
        # three days"; it has existed for five minutes.
        reference = last_progress or created
        if reference is None or reference > progress_ceiling:
            continue
        moved = "has never moved" if last_progress is None else f"last moved {last_progress.isoformat()}"
        out.append(
            Finding(
                source_id,
                Finding.SILENT_FAILURE,
                f"attempted {last_attempt.isoformat()} but the watermark {moved}",
            )
        )
    return out


def coverage_gaps(db: Session, workspace_id: int) -> list[Finding]:
    """Progress that claims more than the stored data can support.

    Two provable forms, and only these two:

    1. The watermark is above zero and **no message row exists at or
       below it**. Something set a resume point past content this system
       has no record of examining, so everything below it will be read by
       nobody. This is the shape a pre-runtime deployment leaves behind,
       and it is a fact about our own tables rather than a guess about
       Telegram's.
    2. The progress row's ``coverage_status`` is already
       ``DETECTED_GAP`` — recorded by whatever detected it.

    Absence of a message row is checked against ``messages``, which is
    written for every message examined including ones that held no links.
    That is what makes "we examined nothing here" distinguishable from
    "we examined it and it had no links".
    """
    rows = db.execute(
        select(
            SourceProgress.source_id,
            SourceProgress.current_watermark,
            SourceProgress.coverage_status,
            func.count(Message.id),
        )
        .outerjoin(
            Message,
            (Message.channel_id == SourceProgress.source_id)
            & (Message.tg_message_id <= SourceProgress.current_watermark),
        )
        .where(
            SourceProgress.workspace_id == workspace_id,
            SourceProgress.track == SourceProgress.LIVE,
        )
        .group_by(
            SourceProgress.source_id,
            SourceProgress.current_watermark,
            SourceProgress.coverage_status,
        )
    ).all()

    out: list[Finding] = []
    for source_id, watermark, coverage, examined in rows:
        if coverage == SourceProgress.DETECTED_GAP:
            out.append(Finding(source_id, Finding.COVERAGE_GAP, "recorded as DETECTED_GAP"))
            continue
        if watermark > 0 and examined == 0:
            out.append(
                Finding(
                    source_id,
                    Finding.COVERAGE_GAP,
                    f"watermark is {watermark} but no message at or below it was ever examined",
                )
            )
    return out


def abandoned_runs(db: Session, workspace_id: int, *, now: datetime | None = None) -> list[Finding]:
    """RUNNING rows whose worker stopped saying anything.

    Duplicates what startup recovery closes — on purpose. Recovery only
    runs when a process starts; this is how the condition is visible
    while one is still running, which is the case where a worker has hung
    rather than died.
    """
    from app.collection.runs import ABANDONED_AFTER

    moment = now or utcnow()
    cutoff = moment - ABANDONED_AFTER
    rows = db.execute(
        select(CollectionRun.source_id, CollectionRun.id, CollectionRun.heartbeat_at).where(
            CollectionRun.workspace_id == workspace_id,
            CollectionRun.state == CollectionRun.RUNNING,
            CollectionRun.heartbeat_at.is_not(None),
            CollectionRun.heartbeat_at < cutoff,
        )
    ).all()
    return [
        Finding(source_id, Finding.ABANDONED_RUN, f"run {run_id} last beat {beat.isoformat()}")
        for source_id, run_id, beat in rows
    ]


def report(db: Session, workspace_id: int, *, now: datetime | None = None) -> list[Finding]:
    """Every finding, most-actionable first. No score, by design."""
    moment = now or utcnow()
    return [
        *abandoned_runs(db, workspace_id, now=moment),
        *silent_failures(db, workspace_id, now=moment),
        *stalled(db, workspace_id, now=moment),
        *coverage_gaps(db, workspace_id),
    ]


__all__ = [
    "SILENT_AFTER",
    "STALL_AFTER",
    "Finding",
    "abandoned_runs",
    "coverage_gaps",
    "report",
    "silent_failures",
    "stalled",
]
