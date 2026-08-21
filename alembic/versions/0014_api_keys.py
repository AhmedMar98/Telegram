"""api keys and audit ip

Revision ID: 0014_api_keys
Revises: 0013_account_health
Create Date: 2026-08-21
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0014_api_keys"
down_revision = "0013_account_health"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("use_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_api_keys_workspace_id", "api_keys", ["workspace_id"])
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])
    # Unique, not merely indexed: the hash *is* the credential's identity,
    # and every authenticated request looks a key up by it.
    op.create_index("ix_api_keys_token_hash", "api_keys", ["token_hash"], unique=True)

    # Idea 81: where an audited action came from. Nullable rather than
    # backfilled — every existing row genuinely has no address, and
    # inventing one would make the column worse than useless.
    op.add_column("audit_log", sa.Column("ip_address", sa.String(length=45), nullable=True))


def downgrade() -> None:
    op.drop_column("audit_log", "ip_address")
    op.drop_index("ix_api_keys_token_hash", table_name="api_keys")
    op.drop_index("ix_api_keys_user_id", table_name="api_keys")
    op.drop_index("ix_api_keys_workspace_id", table_name="api_keys")
    op.drop_table("api_keys")
