"""add action_events table (generic rate-limiting primitive)

Revision ID: 0005_action_events
Revises: 0004_word_split_fts
Create Date: 2026-08-19

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_action_events"
down_revision: str | None = "0004_word_split_fts"
branch_labels: Sequence[str] | str | None = None
depends_on: Sequence[str] | str | None = None


def upgrade() -> None:
    op.create_table(
        "action_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(length=50), nullable=False),
        sa.Column("identifier", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_action_events_scope_identifier_created",
        "action_events",
        ["scope", "identifier", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_action_events_scope_identifier_created", table_name="action_events")
    op.drop_table("action_events")
