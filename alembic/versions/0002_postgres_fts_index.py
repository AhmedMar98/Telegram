"""add postgres full-text GIN index on links (no-op on sqlite)

Revision ID: 0002_postgres_fts_index
Revises: a11dc25147c5
Create Date: 2026-08-19

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_postgres_fts_index"
down_revision: str | None = "a11dc25147c5"
branch_labels: Sequence[str] | str | None = None
depends_on: Sequence[str] | str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite (local dev / tests) has no to_tsvector/GIN support; the
        # links router falls back to ILIKE there. Nothing to create.
        return

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_links_fts
        ON links
        USING GIN (to_tsvector('simple', coalesce(raw_text, '') || ' ' || url))
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS ix_links_fts")
