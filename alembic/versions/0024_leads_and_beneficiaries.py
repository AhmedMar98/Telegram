"""watching for people who need something, not only for links

Revision ID: 0024_leads_and_beneficiaries
Revises: 0023_platform_and_source
Create Date: 2026-09-01

Three tables that turn the same message stream into a second product: a
keyword rule set per workspace, the matches those rules produced, and the
people behind them.

**These tables stay empty unless LEADS_ENABLED is set.** That is not a
feature flag for convenience — it is the honest shape of the decision.
Everything the system stored until now was a *link*; a beneficiaries table
stores identifiable third parties who never opted in, and a deployment
should have to say yes to that rather than discover it.

What is deliberately absent from ``beneficiaries``: phone numbers, emails,
and any per-person message archive. Telegram exposes a phone on some peers
and storing it would turn a lead list into a contact database, which is a
different thing with different obligations.

``leads`` is uniquely keyed on (workspace, channel, message) because every
message is seen twice in normal operation — once by the live listener as it
arrives, once by the hourly collector reading history. Without the
constraint each restart would duplicate the pipeline.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0024_leads_and_beneficiaries"
down_revision = "0023_platform_and_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "beneficiaries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("tg_user_id", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=200), nullable=True),
        sa.Column("display_name", sa.String(length=300), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("workspace_id", "tg_user_id", name="uq_beneficiary_per_workspace"),
    )
    op.create_index("ix_beneficiaries_workspace_id", "beneficiaries", ["workspace_id"])
    op.create_index("ix_beneficiaries_tg_user_id", "beneficiaries", ["tg_user_id"])
    op.create_index("ix_beneficiaries_last_seen_at", "beneficiaries", ["last_seen_at"])

    op.create_table(
        "keyword_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("phrase", sa.String(length=200), nullable=False),
        sa.Column("weight", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("workspace_id", "phrase", name="uq_keyword_per_workspace"),
    )
    op.create_index("ix_keyword_rules_workspace_id", "keyword_rules", ["workspace_id"])

    op.create_table(
        "leads",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("beneficiary_id", sa.Integer(), sa.ForeignKey("beneficiaries.id"), nullable=True),
        sa.Column("channel_id", sa.Integer(), sa.ForeignKey("channels.id"), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("matched", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="new"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("workspace_id", "channel_id", "message_id", name="uq_lead_per_message"),
    )
    op.create_index("ix_leads_workspace_id", "leads", ["workspace_id"])
    op.create_index("ix_leads_beneficiary_id", "leads", ["beneficiary_id"])
    op.create_index("ix_leads_channel_id", "leads", ["channel_id"])
    op.create_index("ix_leads_score", "leads", ["score"])
    op.create_index("ix_leads_status", "leads", ["status"])
    op.create_index("ix_leads_workspace_created", "leads", ["workspace_id", "created_at"])


def downgrade() -> None:
    op.drop_table("leads")
    op.drop_table("keyword_rules")
    op.drop_table("beneficiaries")
