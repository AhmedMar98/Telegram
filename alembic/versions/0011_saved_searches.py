"""saved searches

Revision ID: 0011_saved_searches
Revises: 0010_vitality_maturity
Create Date: 2026-08-20

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011_saved_searches"
down_revision: str | None = "0010_vitality_maturity"
branch_labels: Sequence[str] | str | None = None
depends_on: Sequence[str] | str | None = None


def upgrade() -> None:
    op.create_table(
        "saved_searches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        # One JSON blob rather than a column per filter: nothing queries
        # inside it, and a migration per new filter would make adding a
        # filter more expensive than writing one.
        sa.Column("filters", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("workspace_id", "name", name="uq_saved_search_per_workspace"),
    )
    op.create_index("ix_saved_searches_workspace_id", "saved_searches", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("ix_saved_searches_workspace_id", table_name="saved_searches")
    op.drop_table("saved_searches")
