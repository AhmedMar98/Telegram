"""coverage snapshots: the measurement as a time series

Revision ID: 0027_coverage_snapshots
Revises: 0026_collection_outcome
Create Date: 2026-09-02

``GET /status/coverage`` answers "how are we doing now". It cannot answer
the question that decides whether to act — **is it getting worse?** — and
99.2%, then 98.7%, then 94.1% is a system degrading in plain sight where
every single reading looks acceptable.

One row per run per workspace, pruned past
``app.coverage.SNAPSHOT_RETENTION_DAYS``. PostgreSQL rather than a metrics
stack on purpose: Prometheus and Grafana are each a service to run, on a
deployment whose entire constraint is that it runs one, and a table with a
retention window answers the same question for nothing.

Row-level security like every other workspace-scoped table: a snapshot
names how many sources a workspace has and how many of them are failing,
which is operational intelligence about that tenant.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0027_coverage_snapshots"
down_revision = "0026_collection_outcome"
branch_labels = None
depends_on = None

POLICY = "tenant_isolation"
SETTING = "app.workspace_id"


def upgrade() -> None:
    op.create_table(
        "coverage_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=False),
        sa.Column("sources_expected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sources_due", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sources_attempted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sources_succeeded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sources_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sources_skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("messages_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("messages_processed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("links_found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("links_stored", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duplicate_occurrences", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("collection_lag_p50", sa.Float(), nullable=True),
        sa.Column("collection_lag_p95", sa.Float(), nullable=True),
        sa.Column("watermark_regressions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("gap_events", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_coverage_snapshots_workspace_id", "coverage_snapshots", ["workspace_id"])
    op.create_index("ix_coverage_snapshots_run_id", "coverage_snapshots", ["run_id"])
    op.create_index("ix_coverage_workspace_finished", "coverage_snapshots", ["workspace_id", "finished_at"])

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE coverage_snapshots ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE coverage_snapshots FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {POLICY} ON coverage_snapshots "
            f"USING (workspace_id = NULLIF(current_setting('{SETTING}', true), '')::int)"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(f"DROP POLICY IF EXISTS {POLICY} ON coverage_snapshots")
    op.drop_index("ix_coverage_workspace_finished", table_name="coverage_snapshots")
    op.drop_index("ix_coverage_snapshots_run_id", table_name="coverage_snapshots")
    op.drop_index("ix_coverage_snapshots_workspace_id", table_name="coverage_snapshots")
    op.drop_table("coverage_snapshots")
