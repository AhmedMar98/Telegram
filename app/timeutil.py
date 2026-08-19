from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return the current UTC time as a naive ``datetime`` (matches DB columns)."""
    return datetime.now(UTC).replace(tzinfo=None)
