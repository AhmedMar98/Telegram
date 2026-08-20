"""record where sessions and login attempts came from

Revision ID: 0008_session_origin
Revises: 0007_link_vitality
Create Date: 2026-08-20

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_session_origin"
down_revision: str | None = "0007_link_vitality"
branch_labels: Sequence[str] | str | None = None
depends_on: Sequence[str] | str | None = None


def upgrade() -> None:
    # All nullable: rows written before this migration genuinely have no
    # origin, and recording that honestly is better than backfilling a
    # placeholder that reads like real data.
    op.add_column("auth_sessions", sa.Column("ip_address", sa.String(length=45), nullable=True))
    op.add_column("auth_sessions", sa.Column("user_agent", sa.String(length=300), nullable=True))
    op.add_column("login_attempts", sa.Column("ip_address", sa.String(length=45), nullable=True))


def downgrade() -> None:
    op.drop_column("login_attempts", "ip_address")
    op.drop_column("auth_sessions", "user_agent")
    op.drop_column("auth_sessions", "ip_address")
