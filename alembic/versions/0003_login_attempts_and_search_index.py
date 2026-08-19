"""add login_attempts table and composite search-ordering index

Revision ID: 0003_login_attempts
Revises: 0002_postgres_fts_index
Create Date: 2026-08-19

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_login_attempts"
down_revision: str | None = "0002_postgres_fts_index"
branch_labels: Sequence[str] | str | None = None
depends_on: Sequence[str] | str | None = None


def upgrade() -> None:
    op.create_table(
        "login_attempts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("identifier", sa.String(length=320), nullable=False),
        sa.Column("successful", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_login_attempts_identifier", "login_attempts", ["identifier"])
    op.create_index("ix_login_attempts_created_at", "login_attempts", ["created_at"])

    # Search lists a workspace's links newest-first. Without this composite
    # index every page is a full scan of the workspace's rows plus a sort;
    # with it, both the filter and the ordering are served by one index.
    op.create_index("ix_links_workspace_created", "links", ["workspace_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_links_workspace_created", table_name="links")
    op.drop_index("ix_login_attempts_created_at", table_name="login_attempts")
    op.drop_index("ix_login_attempts_identifier", table_name="login_attempts")
    op.drop_table("login_attempts")
