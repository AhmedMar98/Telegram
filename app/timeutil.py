"""
Time utilities.

Python 3.12 deprecated ``datetime.utcnow()`` in favour of the timezone-aware
``datetime.now(timezone.utc)``. Our SQLAlchemy models store naive datetimes
(SQLite has no first-class TZ awareness), so we standardise on a single
helper that returns a naive UTC datetime — future-compatible without
breaking the storage layer.
"""
from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return the current UTC time as a naive ``datetime``."""
    return datetime.now(UTC).replace(tzinfo=None)
