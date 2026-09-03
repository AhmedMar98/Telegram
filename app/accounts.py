"""Collector account health: recording what happened, and acting on it.

Lives in ``app/`` rather than in the collector script because the web API
reads this state to show the account panel, and both sides must agree on
what "failing" means. The collector writes it; the dashboard reads it.

The rule this exists to enforce: **an account that cannot work must stop
being retried silently.** Before this, a revoked session was retried every
hour forever, logging an error nobody was reading, while the channels it
owned quietly collected nothing.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models import TelegramAccount
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

#: Which state changes are allowed, and from where. Absent pairs are not
#: forbidden by the database — they are what "unexpected" looks like, and
#: ``set_state`` logs rather than raises so a real transition is never lost
#: to a table that had not anticipated it.
#:
#: ACTIVE is reachable from every other state because every other state is
#: recoverable: a person re-enables, re-authorises, or waits out a limit.
EXPECTED_TRANSITIONS: dict[str, frozenset[str]] = {
    TelegramAccount.ACTIVE: frozenset(
        {
            TelegramAccount.INACTIVE,
            TelegramAccount.DISABLED,
            TelegramAccount.AUTH_REQUIRED,
            TelegramAccount.UNAVAILABLE,
            TelegramAccount.RATE_LIMITED,
            TelegramAccount.NEEDS_REVIEW,
        }
    ),
    TelegramAccount.AUTH_REQUIRED: frozenset({TelegramAccount.AUTH_FAILED, TelegramAccount.ACTIVE}),
}


def set_state(
    db: Session,
    account: TelegramAccount,
    state: str,
    *,
    reason: str | None = None,
    commit: bool = False,
) -> None:
    """Move an account to ``state``, keeping ``is_active`` in step.

    The two columns are bound by a database CHECK — exactly ACTIVE means
    active — so they are written together here rather than left to every
    call site to remember. Setting one without the other is refused by the
    database, which is the point: there is no way to end up with an
    account that is ACTIVE and not active, or disabled and still collected
    from.
    """
    if state not in TelegramAccount.STATES:
        raise ValueError(f"unknown account state: {state!r}")

    previous = account.state
    if previous != state:
        allowed = EXPECTED_TRANSITIONS.get(previous)
        if allowed is not None and state not in allowed:
            logger.info("account %s: unexpected transition %s -> %s", account.id, previous, state)
        account.state_changed_at = utcnow()

    account.state = state
    account.is_active = state == TelegramAccount.ACTIVE
    account.state_reason = reason
    # Kept in step with the state it explains. The dashboard reads this to
    # tell an automatic disable from one a person chose.
    account.disabled_reason = reason if state == TelegramAccount.DISABLED else None
    if commit:
        db.commit()


# Consecutive failed runs before an account is disabled automatically.
# Three is a judgement, not a measurement: with the hourly schedule it is
# roughly three hours of consistent failure, which outlasts a transient
# network problem but does not leave a revoked session running for days.
MAX_CONSECUTIVE_FAILURES = 3


def record_success(db: Session, account: TelegramAccount, *, links_collected: int) -> None:
    """A run finished. Clears the failure streak."""
    account.last_success_at = utcnow()
    account.consecutive_failures = 0
    account.last_error = None
    account.links_collected = (account.links_collected or 0) + links_collected
    db.commit()


def record_failure(db: Session, account: TelegramAccount, error: str) -> bool:
    """A run failed. Returns whether this failure disabled the account.

    The error text is stored truncated and shown to the operator, because
    "this account is failing" without saying how is a dead end: a revoked
    session and a network blip need opposite responses.
    """
    account.last_failure_at = utcnow()
    account.consecutive_failures = (account.consecutive_failures or 0) + 1
    account.last_error = error[:300]

    disabled = False
    if account.consecutive_failures >= MAX_CONSECUTIVE_FAILURES and account.is_active:
        set_state(
            db,
            account,
            TelegramAccount.DISABLED,
            reason=(
                f"disabled automatically after {account.consecutive_failures} consecutive failures: {error[:150]}"
            ),
        )
        disabled = True
        logger.error(
            "account %s (%s) disabled after %d consecutive failures: %s",
            account.id,
            account.label,
            account.consecutive_failures,
            error[:150],
        )

    db.commit()
    return disabled


def reactivate(db: Session, account: TelegramAccount) -> None:
    """Bring an account back after the operator has fixed it.

    Clears the streak as well as the flag: leaving the counter at its old
    value would disable the account again on its next single failure,
    which is not what re-enabling means.
    """
    set_state(db, account, TelegramAccount.ACTIVE, reason=None)
    account.consecutive_failures = 0
    account.last_error = None
    db.commit()
