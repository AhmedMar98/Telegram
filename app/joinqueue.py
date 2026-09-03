"""The queue of private sources waiting on access, and what happened to them.

A private source nobody can read is not a failure and not an error — it is
a source in a particular state, waiting on a particular thing. This is the
record of that waiting: who would join, what was tried, what came back,
and when it is worth trying again.

**This module does not join anything.** It records intent and outcome. The
attempt itself belongs to the runtime and is gated on authorisation, rate
limits and the source's own rules; a row here is not permission, and
nothing in this module treats it as such. That separation is why
``REQUEST_SENT`` cannot become ``GRANTED`` here without an observation:
``granted()`` requires the caller to have seen the access work, and writes
the access state alongside so the two cannot disagree.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import access
from app.models import Channel, JoinRequest, SourceAccess
from app.timeutil import utcnow

#: How long before a sent-but-unanswered request is worth looking at again.
#: A judgement, not a measurement: long enough that a moderator has had a
#: chance to act, short enough that a forgotten request surfaces.
REQUEST_FOLLOW_UP = timedelta(days=1)

#: Backoff between attempts, by attempt number. Deliberately short and
#: finite: this is a queue a person watches, not a retry storm.
RETRY_BACKOFF = (timedelta(minutes=30), timedelta(hours=4), timedelta(days=1))


def enqueue(
    db: Session,
    channel: Channel,
    account_id: int,
    *,
    priority: int = 0,
    at: datetime | None = None,
) -> JoinRequest:
    """Queue an access attempt, or return the one already open. No commit.

    Idempotent because the alternative is two attempts racing at
    Telegram's rate limits on behalf of one account, which is the fastest
    way to lose the account this was meant to use.
    """
    existing = db.execute(
        select(JoinRequest).where(
            JoinRequest.source_id == channel.id,
            JoinRequest.account_id == account_id,
            JoinRequest.status.in_(JoinRequest.OPEN_STATUSES),
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    row = JoinRequest(
        workspace_id=channel.workspace_id,
        source_id=channel.id,
        account_id=account_id,
        status=JoinRequest.READY,
        priority=priority,
        next_action_at=at or utcnow(),
    )
    db.add(row)
    db.flush()
    return row


def due(db: Session, workspace_id: int, *, now: datetime | None = None, limit: int = 50) -> list[JoinRequest]:
    """Requests worth acting on, highest priority and oldest first."""
    moment = now or utcnow()
    return list(
        db.execute(
            select(JoinRequest)
            .where(
                JoinRequest.workspace_id == workspace_id,
                JoinRequest.status.in_((JoinRequest.READY, JoinRequest.REQUEST_SENT)),
                JoinRequest.next_action_at.is_not(None),
                JoinRequest.next_action_at <= moment,
            )
            .order_by(JoinRequest.priority.desc(), JoinRequest.next_action_at, JoinRequest.id)
            .limit(limit)
        )
        .scalars()
        .all()
    )


def _touch(row: JoinRequest, status: str, result: str | None) -> None:
    row.status = status
    row.result = result[:300] if result else None
    row.updated_at = utcnow()


def attempting(db: Session, row: JoinRequest) -> JoinRequest:
    """Mark an attempt as in flight, and count it."""
    row.attempt_count = (row.attempt_count or 0) + 1
    row.last_attempt_at = utcnow()
    _touch(row, JoinRequest.ATTEMPTING, None)
    row.next_action_at = None
    db.flush()
    return row


def request_sent(
    db: Session, row: JoinRequest, channel: Channel, *, result: str = "join request sent"
) -> JoinRequest:
    """A request went out. **This is not access** and does not pretend to be.

    The access state moves to REQUEST_SENT, not ACCESSIBLE, and the source
    stays uncollectable until somebody answers.
    """
    _touch(row, JoinRequest.REQUEST_SENT, result)
    row.next_action_at = utcnow() + REQUEST_FOLLOW_UP
    access.record(
        db,
        channel,
        SourceAccess.REQUEST_SENT,
        account_id=row.account_id,
        observed_at=utcnow(),
        evidence_kind="join_request",
        evidence_summary=result,
    )
    db.flush()
    return row


def granted(db: Session, row: JoinRequest, channel: Channel, *, observation: str) -> JoinRequest:
    """Access was **verified**, not merely requested.

    ``observation`` is required and is stored as the evidence, because the
    only thing that makes this state different from REQUEST_SENT is that
    somebody looked.
    """
    if not observation:
        raise ValueError("granted() needs the observation that proves access; refusing to record a bare state")
    _touch(row, JoinRequest.GRANTED, observation)
    row.next_action_at = None
    access.record(
        db,
        channel,
        SourceAccess.ACCESSIBLE,
        account_id=row.account_id,
        observed_at=utcnow(),
        evidence_kind="join_result",
        evidence_summary=observation,
    )
    db.flush()
    return row


def denied(db: Session, row: JoinRequest, channel: Channel, *, reason: str) -> JoinRequest:
    _touch(row, JoinRequest.DENIED, reason)
    row.next_action_at = None
    access.record(
        db,
        channel,
        SourceAccess.ACCESS_DENIED,
        account_id=row.account_id,
        observed_at=utcnow(),
        evidence_kind="join_result",
        evidence_summary=reason,
    )
    db.flush()
    return row


def failed(db: Session, row: JoinRequest, *, error: str) -> JoinRequest:
    """A retryable failure. Backs off, and gives up saying so.

    Running out of attempts produces MANUAL_INTERVENTION rather than a
    silent stop: a source waiting on a person is a state somebody can act
    on, and a queue that quietly abandons rows is one nobody can trust.
    """
    _touch(row, JoinRequest.FAILED, error)
    index = min((row.attempt_count or 1) - 1, len(RETRY_BACKOFF) - 1)
    if (row.attempt_count or 0) >= len(RETRY_BACKOFF):
        _touch(row, JoinRequest.MANUAL_INTERVENTION, f"gave up after {row.attempt_count} attempts: {error}")
        row.next_action_at = None
    else:
        row.status = JoinRequest.READY
        row.next_action_at = utcnow() + RETRY_BACKOFF[index]
    db.flush()
    return row


def blocked(db: Session, row: JoinRequest, channel: Channel, *, reason: str) -> JoinRequest:
    """Policy or Telegram says no in a way retrying will not fix."""
    _touch(row, JoinRequest.BLOCKED, reason)
    row.next_action_at = None
    access.record(
        db,
        channel,
        SourceAccess.BLOCKED,
        account_id=row.account_id,
        observed_at=utcnow(),
        evidence_kind="join_result",
        evidence_summary=reason,
    )
    db.flush()
    return row
