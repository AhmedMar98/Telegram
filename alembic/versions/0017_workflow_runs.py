"""workflow run reports

Revision ID: 0017_workflow_runs
Revises: 0016_notifications
Create Date: 2026-08-21
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0017_workflow_runs"
down_revision = "0016_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("conclusion", sa.String(length=30), nullable=False),
        sa.Column("detail", sa.String(length=500), nullable=True),
        sa.Column("commit_sha", sa.String(length=40), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workflow_runs_workspace_id", "workflow_runs", ["workspace_id"])
    op.create_index("ix_workflow_runs_name", "workflow_runs", ["name"])
    # The status board asks for the newest run per workflow within one
    # workspace, which is exactly this order.
    op.create_index("ix_workflow_runs_ws_name_started", "workflow_runs", ["workspace_id", "name", "started_at"])


def downgrade() -> None:
    op.drop_index("ix_workflow_runs_ws_name_started", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_name", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_workspace_id", table_name="workflow_runs")
    op.drop_table("workflow_runs")
