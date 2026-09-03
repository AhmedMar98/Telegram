"""Who collects a source — decided in one place, recorded in one table.

``source_assignments`` is the source of truth. ``Channel.account_id`` is a
**derived mirror** of it, kept only because the collector, the dashboard
counts and the reassignment endpoint all read that column today, and
repointing the collection runtime is a later phase's work with a larger
blast radius than this decision deserves.

Two authorities for one fact is the failure this module exists to prevent.
The rule is therefore narrow and total:

    every write to Channel.account_id goes through this module

and a database trigger (migration 0027) refuses any other write that would
make the column disagree with the open assignment. So the mirror cannot
drift silently — not through a forgotten call site, not through a script,
not through a hand-written UPDATE.

**Order matters and is load-bearing.** The assignment row is written
first, then the mirror. The trigger compares the incoming ``account_id``
against the open assignment, so writing the mirror first would fail its
own guard.

What "assigned" does and does not claim
--------------------------------------
An assignment names the account operationally responsible for a source. It
says nothing about whether collection has happened, is happening, or
succeeded — ``Assigned + Never Collected``, ``Assigned + Access Lost`` and
``Assigned + Collection Failed`` are all representable, and all different.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import Channel, SourceAssignment
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

#: Set for the length of one transaction while this module writes the
#: mirror. Migration 0027 installs a trigger that refuses any write to
#: ``channels.account_id`` without it, which is what makes "every write
#: goes through this module" enforced rather than merely intended.
WRITE_FLAG = "app.assignment_write"


def _permit_mirror_write(db: Session) -> None:
    """Announce to the database that this write is the sanctioned one.

    ``SET LOCAL`` so it dies with the transaction — a flag that outlived
    its statement would hand the next caller the permission this one
    earned. No-op off PostgreSQL, which has no trigger to satisfy.
    """
    if db.bind is None or db.bind.dialect.name != "postgresql":
        return
    db.execute(text(f"SET LOCAL {WRITE_FLAG} = 'on'"))


class AssignmentConflict(RuntimeError):
    """Two open assignments for one source were about to exist.

    Raised before the database would raise it, so the caller gets a
    sentence rather than a constraint name.
    """


def open_assignment(db: Session, source_id: int) -> SourceAssignment | None:
    """The row naming the current Primary Collector, or None."""
    return db.execute(
        select(SourceAssignment).where(
            SourceAssignment.source_id == source_id,
            SourceAssignment.released_at.is_(None),
        )
    ).scalar_one_or_none()


def current_account_id(db: Session, source_id: int) -> int | None:
    """Which account is responsible for this source, authoritatively.

    Reads the assignment table rather than the mirror. Call sites that
    want the truth rather than the compatibility value use this.
    """
    row = open_assignment(db, source_id)
    return row.account_id if row is not None else None


def assign(
    db: Session,
    channel: Channel,
    account_id: int | None,
    *,
    reason: str,
    evidence_id: int | None = None,
    at: datetime | None = None,
) -> SourceAssignment | None:
    """Make ``account_id`` the Primary Collector for ``channel``. No commit.

    Passing ``None`` releases the source instead, which is the same
    operation seen from the other side: the open row closes and nothing
    replaces it.

    Idempotent by design. Re-assigning a source to the account that
    already holds it changes nothing and closes nothing — an assignment
    that "changed" to itself would put a spurious entry in the history and
    make the record of real moves harder to read.
    """
    moment = at or utcnow()
    current = open_assignment(db, channel.id)

    if current is not None and current.account_id == account_id:
        return current

    if current is not None:
        current.released_at = moment
        current.reason = current.reason or reason
        db.flush()

    row: SourceAssignment | None = None
    if account_id is not None:
        row = SourceAssignment(
            workspace_id=channel.workspace_id,
            source_id=channel.id,
            account_id=account_id,
            assigned_at=moment,
            reason=reason,
            evidence_id=evidence_id,
        )
        db.add(row)
        db.flush()

    # The mirror, last, and only with the flag the trigger looks for.
    _permit_mirror_write(db)
    channel.account_id = account_id
    db.flush()
    return row


def release(db: Session, channel: Channel, *, reason: str, at: datetime | None = None) -> None:
    """Close the open assignment and leave the source unassigned."""
    assign(db, channel, None, reason=reason, at=at)


def release_account(db: Session, workspace_id: int, account_id: int, *, reason: str) -> list[int]:
    """Release every source this account holds. Returns the source ids.

    Used when an account is removed: the sources survive it, and each one
    keeps the history of having been held by an account that no longer
    exists.
    """
    channels = (
        db.query(Channel).filter(Channel.workspace_id == workspace_id, Channel.account_id == account_id).all()
    )
    for channel in channels:
        release(db, channel, reason=reason)
    return [channel.id for channel in channels]


def assignments_for_account(db: Session, account_id: int) -> list[SourceAssignment]:
    """Every source this account currently holds.

    The first half of "what breaks if this account goes away" — the rest
    is whether each source has another account that can reach it, which is
    ``app.access`` territory.
    """
    return list(
        db.execute(
            select(SourceAssignment).where(
                SourceAssignment.account_id == account_id,
                SourceAssignment.released_at.is_(None),
            )
        )
        .scalars()
        .all()
    )


def history_for_source(db: Session, source_id: int) -> list[SourceAssignment]:
    """Every assignment this source has ever had, oldest first."""
    return list(
        db.execute(
            select(SourceAssignment).where(SourceAssignment.source_id == source_id).order_by(SourceAssignment.id)
        )
        .scalars()
        .all()
    )


def mirror_disagreements(db: Session, workspace_id: int) -> list[tuple[int, int | None, int | None]]:
    """Sources where the mirror and the assignment table disagree.

    Should always be empty — the trigger makes divergence impossible on
    PostgreSQL. It exists because SQLite has no trigger, so the test suite
    needs a way to assert the property rather than assume it, and because
    a deployment that predates the trigger deserves a way to check.

    Returns ``(source_id, mirror_account_id, authoritative_account_id)``.
    """
    rows = db.query(Channel).filter(Channel.workspace_id == workspace_id).all()
    out: list[tuple[int, int | None, int | None]] = []
    for channel in rows:
        truth = current_account_id(db, channel.id)
        if channel.account_id != truth:
            out.append((channel.id, channel.account_id, truth))
    return out


def failure_impact(db: Session, workspace_id: int, account_id: int) -> dict[str, list[int]]:
    """What stops collecting if this account does, and what survives it.

    Two lists, because they need different responses. A *recoverable*
    source has another account that could take it: the fleet absorbs the
    loss. A *stranded* one does not: losing this account loses the source
    until somebody gets access, and that is the number worth knowing
    before the account fails rather than after.

    Deliberately a query and not a score. Ranking sources by importance is
    optimisation, needs data nobody has measured yet, and would obscure
    the plain fact this answers.
    """
    from app import eligibility
    from app.assignment import capacity_per_account
    from app.models import TelegramAccount

    held = [row.source_id for row in assignments_for_account(db, account_id)]
    if not held:
        return {"held": [], "recoverable": [], "stranded": []}

    survivors = (
        db.query(TelegramAccount)
        .filter(
            TelegramAccount.workspace_id == workspace_id,
            TelegramAccount.id != account_id,
            TelegramAccount.state == TelegramAccount.ACTIVE,
        )
        .order_by(TelegramAccount.id)
        .all()
    )
    capacity = capacity_per_account()

    recoverable: list[int] = []
    stranded: list[int] = []
    for source_id in held:
        channel = db.get(Channel, source_id)
        if channel is None:
            continue
        result = eligibility.evaluate(db, channel, survivors, capacity=capacity)
        (recoverable if result.has_candidate else stranded).append(source_id)

    return {"held": held, "recoverable": recoverable, "stranded": stranded}
