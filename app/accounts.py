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
        account.is_active = False
        # Recorded separately from is_active so the dashboard can tell an
        # automatic disable from one a human chose. They need different
        # responses, and a single boolean cannot say which happened.
        account.disabled_reason = (
            f"disabled automatically after {account.consecutive_failures} consecutive failures: {error[:150]}"
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
    account.is_active = True
    account.disabled_reason = None
    account.consecutive_failures = 0
    account.last_error = None
    db.commit()
