"""rebuild the full-text index so URL fragments are searchable

Revision ID: 0004_word_split_fts
Revises: 0003_login_attempts
Create Date: 2026-08-19

The previous index fed raw URLs to to_tsvector, which parses them as
atomic tokens ('example.com', '/python-book.pdf') rather than words. That
made searching for any part of a URL return nothing on Postgres while the
same search worked on SQLite's ILIKE fallback, so production behaved worse
than development and no test caught it.

Splitting every non-alphanumeric run into a space before indexing turns
URLs into ordinary words. The query side applies the identical
transformation (see app/search.py), which is also what allows Postgres to
use this index instead of falling back to a sequential scan.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_word_split_fts"
down_revision: str | None = "0003_login_attempts"
branch_labels: Sequence[str] | str | None = None
depends_on: Sequence[str] | str | None = None

_OLD_DOCUMENT = "to_tsvector('simple', coalesce(raw_text, '') || ' ' || url)"
_NEW_DOCUMENT = (
    "to_tsvector('simple', regexp_replace(coalesce(raw_text, '') || ' ' || url, '[^[:alnum:]]+', ' ', 'g'))"
)


def _rebuild(document_sql: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite has no to_tsvector; the search falls back to ILIKE there.
        return
    op.execute("DROP INDEX IF EXISTS ix_links_fts")
    op.execute(f"CREATE INDEX ix_links_fts ON links USING GIN ({document_sql})")


def upgrade() -> None:
    _rebuild(_NEW_DOCUMENT)


def downgrade() -> None:
    _rebuild(_OLD_DOCUMENT)
