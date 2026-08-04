"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-01-01 00:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(64), unique=True, nullable=False),
        sa.Column("api_id_enc", sa.Text, nullable=False),
        sa.Column("api_hash_enc", sa.Text, nullable=False),
        sa.Column("phone_enc", sa.Text, nullable=False),
        sa.Column("session_file", sa.String(255)),
        sa.Column("is_active", sa.Boolean, default=True),
        sa.Column("is_main", sa.Boolean, default=False),
        sa.Column("last_seen_at", sa.DateTime),
        sa.Column("created_at", sa.DateTime, default=sa.func.now()),
    )
    op.create_table(
        "channels",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("account_id", sa.Integer, sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("channel_username", sa.String(255), nullable=False),
        sa.Column("channel_id", sa.String(64)),
        sa.Column("last_message_id", sa.Integer),
        sa.Column("is_active", sa.Boolean, default=True),
        sa.Column("created_at", sa.DateTime, default=sa.func.now()),
        sa.UniqueConstraint("account_id", "channel_username", name="uq_account_channel"),
    )
    op.create_table(
        "links",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("url_hash", sa.String(64), unique=True, nullable=False),
        sa.Column("title", sa.String(512)),
        sa.Column("description", sa.Text),
        sa.Column("category", sa.String(32), default="unknown"),
        sa.Column("status", sa.String(16), default="new"),
        sa.Column("classification_tier", sa.String(16)),
        sa.Column("confidence", sa.Float, default=0.0),
        sa.Column("source_channel", sa.String(255)),
        sa.Column("source_account_id", sa.Integer),
        sa.Column("source_message_id", sa.Integer),
        sa.Column("discovered_at", sa.DateTime, default=sa.func.now()),
        sa.Column("last_checked_at", sa.DateTime),
        sa.Column("http_status", sa.Integer),
        sa.Column("alive", sa.Boolean),
        sa.Column("metadata", sa.JSON),
        sa.Column("click_count", sa.Integer, default=0),
        sa.Column("search_hit_count", sa.Integer, default=0),
        sa.Column("archived", sa.Boolean, default=False),
    )
    op.create_index("ix_links_url", "links", ["url"])
    op.create_index("ix_links_url_hash", "links", ["url_hash"])
    op.create_index("ix_links_category_status", "links", ["category", "status"])
    op.create_index("ix_links_discovered", "links", ["discovered_at"])

    op.create_table(
        "link_embeddings",
        sa.Column("link_id", sa.Integer, primary_key=True),
        sa.Column("embedding_json", sa.Text, nullable=False),
        sa.Column("model_name", sa.String(64), default="all-MiniLM-L6-v2"),
        sa.Column("dim", sa.Integer, default=384),
        sa.Column("created_at", sa.DateTime, default=sa.func.now()),
    )

    op.create_table(
        "classification_logs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("link_id", sa.Integer, sa.ForeignKey("links.id"), nullable=False),
        sa.Column("tier", sa.String(16), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("confidence", sa.Float, default=0.0),
        sa.Column("rule_matched", sa.String(128)),
        sa.Column("llm_provider", sa.String(32)),
        sa.Column("llm_response", sa.Text),
        sa.Column("semantic_reused", sa.Boolean, default=False),
        sa.Column("created_at", sa.DateTime, default=sa.func.now()),
    )
    op.create_index("ix_classification_logs_link_id", "classification_logs", ["link_id"])

    op.create_table(
        "vitality_checks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("link_id", sa.Integer, sa.ForeignKey("links.id"), nullable=False),
        sa.Column("checked_at", sa.DateTime, default=sa.func.now()),
        sa.Column("http_status", sa.Integer),
        sa.Column("alive", sa.Boolean, nullable=False),
        sa.Column("response_time_ms", sa.Integer),
        sa.Column("error", sa.Text),
    )
    op.create_index("ix_vitality_checks_link_id", "vitality_checks", ["link_id"])

    op.create_table(
        "llm_provider_state",
        sa.Column("name", sa.String(32), primary_key=True),
        sa.Column("is_healthy", sa.Boolean, default=True),
        sa.Column("requests_today", sa.Integer, default=0),
        sa.Column("requests_total", sa.Integer, default=0),
        sa.Column("quota_limit", sa.Integer),
        sa.Column("quota_remaining", sa.Integer),
        sa.Column("quota_resets_at", sa.DateTime),
        sa.Column("last_error", sa.Text),
        sa.Column("last_success_at", sa.DateTime),
        sa.Column("cooldown_until", sa.DateTime),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("target", sa.String(255)),
        sa.Column("details", sa.JSON),
        sa.Column("created_at", sa.DateTime, default=sa.func.now()),
    )
    op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"])


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("llm_provider_state")
    op.drop_table("vitality_checks")
    op.drop_table("classification_logs")
    op.drop_table("link_embeddings")
    op.drop_table("links")
    op.drop_table("channels")
    op.drop_table("accounts")
