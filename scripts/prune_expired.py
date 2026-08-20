"""Delete rows whose only purpose has expired.

Three tables accumulate rows that stop mattering after a fixed window.
What the application already does for each, checked rather than assumed:

``auth_sessions``
    **Never deleted at all.** ``revoke`` sets ``revoked_at`` and expiry is
    only ever checked at read time, so every login ever performed leaves a
    row behind permanently. This is the real growth, and it grows with use.

``action_events``
    ``is_action_rate_limited`` prunes only ``scope + identifier`` — the one
    it was asked about. A workspace that stops adding links is never asked
    about again, so its rows are never swept.

``login_attempts``
    ``record_login_attempt`` already deletes *globally* past the window, so
    this table is largely self-maintaining. It is still swept here because
    that cleanup only happens when somebody attempts a login: an idle
    deployment keeps whatever was there. Expect this count to be zero most
    runs, and that is the correct result, not a broken sweep.

On a size-limited free plan the first two are a genuine leak. This is the
scheduled sweep that closes them. Runs from GitHub Actions alongside the
collector, so there is still no permanent process.

Deletions are bounded and reported rather than silent, and the retention
windows are stated in docs/13-retention.md rather than living only here.

Required environment:
  DATABASE_URL

Optional:
  PRUNE_DRY_RUN=1   report what would be deleted, delete nothing
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import ActionEvent, AuthSession, LoginAttempt  # noqa: E402
from app.timeutil import utcnow  # noqa: E402

logger = logging.getLogger("prune")

# Each window is "long enough that deleting cannot affect a live decision".
# The throttles look at minutes, so a day is already generous; sessions are
# checked against their own expires_at, so a week of grace only keeps the
# audit trail readable for a little while after expiry.
LOGIN_ATTEMPT_RETENTION = timedelta(days=1)
ACTION_EVENT_RETENTION = timedelta(days=1)
EXPIRED_SESSION_GRACE = timedelta(days=7)


def prune(db: Session, *, dry_run: bool = False) -> dict[str, int]:
    """Delete expired rows. Returns how many per table."""
    now = utcnow()
    plans = [
        ("login_attempts", LoginAttempt, LoginAttempt.created_at < now - LOGIN_ATTEMPT_RETENTION),
        ("action_events", ActionEvent, ActionEvent.created_at < now - ACTION_EVENT_RETENTION),
        # Keyed on expires_at, not created_at: a long-lived session that is
        # still valid must never be swept just because it is old.
        ("auth_sessions", AuthSession, AuthSession.expires_at < now - EXPIRED_SESSION_GRACE),
    ]

    counts: dict[str, int] = {}
    for name, model, condition in plans:
        matched = db.query(model).filter(condition).count()
        counts[name] = matched
        if matched and not dry_run:
            db.execute(delete(model).where(condition))

    if not dry_run:
        db.commit()
    return counts


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    dry_run = os.environ.get("PRUNE_DRY_RUN") == "1"

    db = SessionLocal()
    try:
        counts = prune(db, dry_run=dry_run)
    finally:
        db.close()

    total = sum(counts.values())
    verb = "would delete" if dry_run else "deleted"
    logger.info("%s %d row(s): %s", verb, total, ", ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
