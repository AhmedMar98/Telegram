"""optional second factor

Revision ID: 0015_totp
Revises: 0014_api_keys
Create Date: 2026-08-21
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0015_totp"
down_revision = "0014_api_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Every column is nullable or defaulted, so existing rows need no
    # backfill and every account keeps logging in exactly as before —
    # a second factor nobody opted into must not change anyone's login.
    op.add_column("users", sa.Column("totp_secret", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("totp_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("users", sa.Column("totp_last_step", sa.BigInteger(), nullable=True))
    op.add_column("users", sa.Column("totp_recovery_hashes", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "totp_recovery_hashes")
    op.drop_column("users", "totp_last_step")
    op.drop_column("users", "totp_enabled")
    op.drop_column("users", "totp_secret")
