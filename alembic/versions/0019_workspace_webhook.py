"""an outbound webhook the workspace configures for itself

Revision ID: 0019_workspace_webhook
Revises: 0018_feedback_domain
Create Date: 2026-08-21

Idea 162. Columns on ``workspaces`` rather than a table of their own:
there is exactly one webhook per workspace, and a one-row-per-workspace
table would only add a join to every read of it.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0019_workspace_webhook"
down_revision = "0018_feedback_domain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Text, not String(n): the stored value is a Fernet token, which is
    # several times longer than the URL inside it.
    op.add_column("workspaces", sa.Column("webhook_url", sa.Text(), nullable=True))
    op.add_column("workspaces", sa.Column("webhook_last_status", sa.Integer(), nullable=True))
    op.add_column("workspaces", sa.Column("webhook_last_attempt_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("workspaces", "webhook_last_attempt_at")
    op.drop_column("workspaces", "webhook_last_status")
    op.drop_column("workspaces", "webhook_url")
