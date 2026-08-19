"""add is_favorite to links

Revision ID: 0006_favorites
Revises: 0005_action_events
Create Date: 2026-08-19

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_favorites"
down_revision: str | None = "0005_action_events"
branch_labels: Sequence[str] | str | None = None
depends_on: Sequence[str] | str | None = None


def upgrade() -> None:
    op.add_column("links", sa.Column("is_favorite", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index("ix_links_is_favorite", "links", ["is_favorite"])
    # The server_default was only needed to backfill existing rows; new
    # inserts always specify the value explicitly via the ORM default.
    # SQLite has no ALTER COLUMN; harmless to leave the default there since
    # SQLite is dev/test-only and the ORM always sends an explicit value.
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column("links", "is_favorite", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_links_is_favorite", table_name="links")
    op.drop_column("links", "is_favorite")
