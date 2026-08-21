"""notifications and per-type preferences

Revision ID: 0016_notifications
Revises: 0015_totp
Create Date: 2026-08-21
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0016_notifications"
down_revision = "0015_totp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_preferences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("alert_type", sa.String(length=50), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        # One row per workspace per type: two contradicting preferences
        # would make "is this alert on?" a question with two answers.
        sa.UniqueConstraint("workspace_id", "alert_type", name="uq_notification_pref_workspace_type"),
    )
    op.create_index("ix_notification_preferences_workspace_id", "notification_preferences", ["workspace_id"])

    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("alert_type", sa.String(length=50), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("delivered_count", sa.Integer(), nullable=False),
        sa.Column("read_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_notifications_workspace_id", "notifications", ["workspace_id"])
    op.create_index("ix_notifications_alert_type", "notifications", ["alert_type"])
    # The centre reads newest-first within a workspace, which is the only
    # access pattern either view uses.
    op.create_index("ix_notifications_ws_created", "notifications", ["workspace_id", "created_at"])

    # No preference rows are seeded. Defaults live in app/alerts.py and are
    # read at call time, so a default the project later reconsiders applies
    # to everyone who never expressed a choice — a backfill here would
    # freeze today's defaults for every existing workspace forever.


def downgrade() -> None:
    op.drop_index("ix_notifications_ws_created", table_name="notifications")
    op.drop_index("ix_notifications_alert_type", table_name="notifications")
    op.drop_index("ix_notifications_workspace_id", table_name="notifications")
    op.drop_table("notifications")
    op.drop_index("ix_notification_preferences_workspace_id", table_name="notification_preferences")
    op.drop_table("notification_preferences")
