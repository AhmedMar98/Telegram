"""v4.1 features: semantic_cache, daily_summaries, tenants, worker_registry, subscriptions

Revision ID: 0002_v41_features
Revises: 0001_initial
Create Date: 2026-01-15 00:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002_v41_features"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add tenant_id to links (v4.1 multi-tenant)
    op.add_column("links", sa.Column("tenant_id", sa.Integer, default=1))
    op.create_index("ix_links_tenant_id", "links", ["tenant_id"])
    op.create_index("ix_links_tenant_hash", "links", ["tenant_id", "url_hash"])

    # v4.1 Semantic cache
    op.create_table(
        "semantic_cache",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("url_hash", sa.String(64), unique=True, nullable=False, index=True),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("embedding_json", sa.Text, nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("tier", sa.String(16), nullable=False),
        sa.Column("confidence", sa.Float, default=0.0),
        sa.Column("used_count", sa.Integer, default=0),
        sa.Column("created_at", sa.DateTime, default=sa.func.now()),
        sa.Column("last_used_at", sa.DateTime),
    )

    # v4.1 Daily summaries
    op.create_table(
        "daily_summaries",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("summary_date", sa.String(10), nullable=False, index=True),
        sa.Column("total_new", sa.Integer, default=0),
        sa.Column("total_alive", sa.Integer, default=0),
        sa.Column("total_dead", sa.Integer, default=0),
        sa.Column("top_links_json", sa.Text, nullable=False, default="[]"),
        sa.Column("category_breakdown_json", sa.Text, nullable=False, default="{}"),
        sa.Column("sent_to_subscribers", sa.Boolean, default=False),
        sa.Column("created_at", sa.DateTime, default=sa.func.now()),
        sa.UniqueConstraint("summary_date", name="uq_daily_summary_date"),
    )

    # v4.1 Tenants
    op.create_table(
        "tenants",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("slug", sa.String(64), unique=True, nullable=False, index=True),
        sa.Column("api_key_hash", sa.String(64), unique=True, nullable=False),
        sa.Column("plan", sa.String(32), default="free"),
        sa.Column("is_active", sa.Boolean, default=True),
        sa.Column("created_at", sa.DateTime, default=sa.func.now()),
    )

    # v5.0 Worker registry
    op.create_table(
        "worker_registry",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("worker_id", sa.String(64), unique=True, nullable=False, index=True),
        sa.Column("account_name", sa.String(64), nullable=False),
        sa.Column("hostname", sa.String(255)),
        sa.Column("pid", sa.Integer),
        sa.Column("status", sa.String(16), default="starting"),
        sa.Column("channels_assigned", sa.Text, default="[]"),
        sa.Column("links_collected", sa.Integer, default=0),
        sa.Column("last_heartbeat_at", sa.DateTime),
        sa.Column("started_at", sa.DateTime, default=sa.func.now()),
        sa.Column("stopped_at", sa.DateTime),
    )

    # v5.0 Subscriptions (Stripe)
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tenant_id", sa.Integer, sa.ForeignKey("tenants.id"), nullable=False, index=True),
        sa.Column("stripe_customer_id", sa.String(128)),
        sa.Column("stripe_subscription_id", sa.String(128)),
        sa.Column("plan", sa.String(32), default="free"),
        sa.Column("status", sa.String(32), default="active"),
        sa.Column("current_period_end", sa.DateTime),
        sa.Column("requests_this_period", sa.Integer, default=0),
        sa.Column("created_at", sa.DateTime, default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("subscriptions")
    op.drop_table("worker_registry")
    op.drop_table("tenants")
    op.drop_table("daily_summaries")
    op.drop_table("semantic_cache")
    op.drop_index("ix_links_tenant_hash", table_name="links")
    op.drop_index("ix_links_tenant_id", table_name="links")
    op.drop_column("links", "tenant_id")
