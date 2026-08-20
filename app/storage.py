"""How much room is left on a size-limited free plan.

The failure mode this exists to make visible: the database fills up and
writes start failing. Everything looks fine until it abruptly does not,
and the dashboard had no way to see it coming.

Every number here is read from the database itself rather than estimated,
and anything that cannot be read is reported as ``None`` — an unknown
size is more useful than a plausible one.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import Link

logger = logging.getLogger(__name__)


def database_bytes(db: Session) -> int | None:
    """Total size of the database, or None if it cannot be determined.

    Postgres answers with ``pg_database_size``; SQLite has no equivalent
    that reflects the file on disk from inside a connection, and a managed
    role may lack permission even on Postgres. Both cases return None
    rather than a guess.
    """
    if db.bind is None or db.bind.dialect.name != "postgresql":
        return None
    try:
        return db.execute(text("SELECT pg_database_size(current_database())")).scalar()
    except SQLAlchemyError as exc:
        # A restricted role is a normal deployment, not an error worth
        # failing the whole stats endpoint over.
        logger.info("database size unavailable: %s", exc)
        return None


def largest_table(db: Session) -> str | None:
    """The table using the most space, so the number above is actionable."""
    if db.bind is None or db.bind.dialect.name != "postgresql":
        return None
    try:
        return db.execute(
            text("SELECT relname FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 1")
        ).scalar()
    except SQLAlchemyError as exc:
        logger.info("largest table unavailable: %s", exc)
        return None


def link_count(db: Session, workspace_id: int) -> int:
    return (
        db.execute(select(func.count()).select_from(Link).where(Link.workspace_id == workspace_id)).scalar() or 0
    )
