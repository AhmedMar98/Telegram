"""The life of one collection run, and what it is allowed to claim.

A run is the record that makes collection auditable: which source, which
account, which path, what range, what moved, what failed. It is also where
the phase's hardest rule lives — **a run may not report success it cannot
support**:

- a connection that opened is not a run that completed
- a worker that exited cleanly is not a run that completed
- zero new messages *is* a completed run, when the scope was examined

``complete()`` therefore takes what was examined and what was persisted,
and refuses to record a completion for a run whose scope was never read.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collection.failures import FailureKind
from app.models import Channel, CollectionRun, SourceProgress
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

#: A RUNNING row whose heartbeat is older than this had a worker that died
#: without saying so. Generous relative to the heartbeat interval, because
#: the cost of declaring a live run dead is a duplicate attempt, and the
#: cost of leaving a dead run RUNNING is a source nobody picks up.
ABANDONED_AFTER = timedelta(minutes=15)


def start(
    db: Session,
    channel: Channel,
    *,
    mode: str,
    account_id: int | None,
    acquisition_path: str = "userbot",
    watermark_before: int | None = None,
    range_from: datetime | None = None,
    range_to: datetime | None = None,
) -> CollectionRun:
    """Open a run. No commit — the caller owns the transaction."""
    if mode not in (SourceProgress.LIVE, SourceProgress.HISTORICAL):
        raise ValueError(f"unknown collection mode: {mode!r}")
    now = utcnow()
    run = CollectionRun(
        workspace_id=channel.workspace_id,
        source_id=channel.id,
        account_id=account_id,
        acquisition_path=acquisition_path,
        mode=mode,
        state=CollectionRun.RUNNING,
        range_from=range_from,
        range_to=range_to,
        watermark_before=watermark_before,
        started_at=now,
        heartbeat_at=now,
    )
    db.add(run)
    db.flush()
    return run


def heartbeat(db: Session, run: CollectionRun) -> None:
    """Say the worker is still alive. Claims nothing about progress."""
    run.heartbeat_at = utcnow()
    db.flush()


def complete(
    db: Session,
    run: CollectionRun,
    *,
    messages_seen: int,
    links_stored: int,
    watermark_after: int | None,
    scope_examined: bool,
    coverage: str = SourceProgress.UNKNOWN_COVERAGE,
    detail: str | None = None,
) -> CollectionRun:
    """Close a run that did its work.

    ``scope_examined`` is the caller's assertion that the requested range
    was actually read — not that a connection opened, and not that the
    function returned. A run that cannot assert it is recorded as a
    failure with ``UNKNOWN_FAILURE``, because "we do not know whether this
    worked" is not a success and must not be stored as one.
    """
    if not scope_examined:
        return fail(
            db,
            run,
            kind=FailureKind.UNKNOWN_FAILURE,
            detail=detail or "run ended without examining its requested scope",
        )

    run.state = CollectionRun.COMPLETED
    run.messages_seen = messages_seen
    run.links_stored = links_stored
    run.watermark_after = watermark_after
    run.finished_at = utcnow()
    run.heartbeat_at = None
    run.detail = detail
    if coverage not in SourceProgress.COVERAGE_STATES:
        raise ValueError(f"unknown coverage status: {coverage!r}")
    db.flush()
    return run


def fail(
    db: Session,
    run: CollectionRun,
    *,
    kind: FailureKind,
    detail: str | None = None,
    messages_seen: int | None = None,
    links_stored: int | None = None,
) -> CollectionRun:
    """Close a run that did not.

    Everything already persisted stays persisted — a failed run is not a
    rollback of the work that succeeded before it, and treating it as one
    would re-collect data that is already stored.
    """
    run.state = CollectionRun.FAILED
    run.failure_kind = kind.value
    run.detail = (detail or "")[:500] or None
    if messages_seen is not None:
        run.messages_seen = messages_seen
    if links_stored is not None:
        run.links_stored = links_stored
    run.finished_at = utcnow()
    run.heartbeat_at = None
    db.flush()
    return run


def cancel(db: Session, run: CollectionRun, *, detail: str) -> CollectionRun:
    """Stop a run on purpose. Distinct from failing, and from completing."""
    run.state = CollectionRun.CANCELLED
    run.detail = detail[:500]
    run.finished_at = utcnow()
    run.heartbeat_at = None
    db.flush()
    return run


def open_runs(db: Session, workspace_id: int) -> list[CollectionRun]:
    return list(
        db.execute(
            select(CollectionRun).where(
                CollectionRun.workspace_id == workspace_id,
                CollectionRun.state.in_((CollectionRun.RUNNING, CollectionRun.RECOVERING)),
            )
        )
        .scalars()
        .all()
    )


def recover_abandoned(
    db: Session, workspace_id: int, *, now: datetime | None = None, older_than: timedelta = ABANDONED_AFTER
) -> list[CollectionRun]:
    """Close out runs whose worker died without saying so.

    Called at startup and by the reconciliation sweep. The rows are marked
    ``FAILED`` with ``WORKER_FAILURE`` rather than deleted or quietly
    completed: an interrupted run is a fact about what happened, and the
    watermark it left behind is exactly as far as its persisted data
    reached — which is why it is safe to resume from and why nothing here
    touches it.
    """
    moment = now or utcnow()
    cutoff = moment - older_than
    stale = list(
        db.execute(
            select(CollectionRun).where(
                CollectionRun.workspace_id == workspace_id,
                CollectionRun.state == CollectionRun.RUNNING,
                CollectionRun.heartbeat_at.is_not(None),
                CollectionRun.heartbeat_at < cutoff,
            )
        )
        .scalars()
        .all()
    )
    for run in stale:
        last_beat = run.heartbeat_at.isoformat() if run.heartbeat_at else "never"
        logger.warning("run %s (source %s) abandoned: last heartbeat %s", run.id, run.source_id, last_beat)
        fail(
            db,
            run,
            kind=FailureKind.WORKER_FAILURE,
            detail=f"no heartbeat since {last_beat}; recovered at startup",
        )
    return stale
