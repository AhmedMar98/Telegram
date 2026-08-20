"""add link vitality columns (last_checked_at, http_status, is_alive)

Revision ID: 0007_link_vitality
Revises: 0006_favorites
Create Date: 2026-08-20

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_link_vitality"
down_revision: str | None = "0006_favorites"
branch_labels: Sequence[str] | str | None = None
depends_on: Sequence[str] | str | None = None


def upgrade() -> None:
    op.add_column("links", sa.Column("last_checked_at", sa.DateTime(), nullable=True))
    op.add_column("links", sa.Column("http_status", sa.Integer(), nullable=True))
    op.add_column("links", sa.Column("is_alive", sa.Boolean(), nullable=True))
    op.create_index("ix_links_last_checked_at", "links", ["last_checked_at"])


def downgrade() -> None:
    op.drop_index("ix_links_last_checked_at", table_name="links")
    op.drop_column("links", "is_alive")
    op.drop_column("links", "http_status")
    op.drop_column("links", "last_checked_at")
