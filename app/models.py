"""
SQLAlchemy ORM models for the Link Intelligence Platform.

Entities:
    Account        — Telegram userbot account (encrypted credentials)
    Channel        — A monitored Telegram channel/chat
    Link           — A discovered URL with classification + vitality state
    LinkEmbedding  — Vector embedding for semantic search (sqlite-vec)
    ClassificationLog — Audit trail of how each link was classified
    VitalityCheck  — History of alive/dead checks per link
    LLMProviderState — Per-provider quota tracking
    AuditEvent     — Security & admin audit log
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base class."""


# ============================================================
# Enums
# ============================================================

class LinkCategory(str, PyEnum):
    WHATSAPP = "whatsapp"
    TELEGRAM = "telegram"
    FILE = "file"
    SETTING = "setting"
    SOCIAL = "social"
    NEWS = "news"
    TOOL = "tool"
    OTHER = "other"
    UNKNOWN = "unknown"


class LinkStatus(str, PyEnum):
    NEW = "new"
    CLASSIFIED = "classified"
    ALIVE = "alive"
    DEAD = "dead"
    ARCHIVED = "archived"


class ClassificationTier(str, PyEnum):
    RULES = "rules"          # ~80%
    METADATA = "metadata"    # ~15%
    LLM = "llm"              # ~4%
    LLM_DEEP = "llm_deep"    # ~1%


# ============================================================
# Tables
# ============================================================

class Account(Base):
    """A Telegram userbot account."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # Encrypted api_id, api_hash, phone (AES-256-GCM)
    api_id_enc: Mapped[str] = mapped_column(Text, nullable=False)
    api_hash_enc: Mapped[str] = mapped_column(Text, nullable=False)
    phone_enc: Mapped[str] = mapped_column(Text, nullable=False)
    session_file: Mapped[str] = mapped_column(String(255), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_main: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    channels: Mapped[list[Channel]] = relationship(back_populates="account")

    def __repr__(self) -> str:
        return f"<Account {self.name} main={self.is_main}>"


class Channel(Base):
    """A Telegram channel/chat being monitored by an account."""

    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    channel_username: Mapped[str] = mapped_column(String(255), nullable=False)
    channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    account: Mapped[Account] = relationship(back_populates="channels")

    __table_args__ = (
        UniqueConstraint("account_id", "channel_username", name="uq_account_channel"),
    )


class Link(Base):
    """A discovered URL."""

    __tablename__ = "links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, default=1, index=True)  # v4.1 multi-tenant
    url: Mapped[str] = mapped_column(String(2048), nullable=False, index=True)
    url_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    category: Mapped[str] = mapped_column(String(32), default=LinkCategory.UNKNOWN.value)
    status: Mapped[str] = mapped_column(String(16), default=LinkStatus.NEW.value)
    classification_tier: Mapped[str | None] = mapped_column(String(16), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)

    # Source tracking
    source_channel: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    # Vitality
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    alive: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # Metadata (JSON — varies per category)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)

    # Counts
    click_count: Mapped[int] = mapped_column(Integer, default=0)
    search_hit_count: Mapped[int] = mapped_column(Integer, default=0)

    # Soft delete
    archived: Mapped[bool] = mapped_column(Boolean, default=False)

    classification_logs: Mapped[list[ClassificationLog]] = relationship(back_populates="link")
    vitality_checks: Mapped[list[VitalityCheck]] = relationship(back_populates="link")

    __table_args__ = (
        Index("ix_links_category_status", "category", "status"),
        Index("ix_links_discovered", "discovered_at"),
        Index("ix_links_tenant_hash", "tenant_id", "url_hash"),
    )

    def __repr__(self) -> str:
        return f"<Link #{self.id} {self.category} {self.status} {self.url[:60]}>"


class LinkEmbedding(Base):
    """Vector embedding for semantic search (sqlite-vec virtual table mirror)."""

    __tablename__ = "link_embeddings"

    link_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Store as JSON-serialized list (sqlite-vec uses a separate virtual table for actual KNN)
    embedding_json: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str] = mapped_column(String(64), default="all-MiniLM-L6-v2")
    dim: Mapped[int] = mapped_column(Integer, default=384)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class ClassificationLog(Base):
    """Audit trail: how a link was classified at each tier."""

    __tablename__ = "classification_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    link_id: Mapped[int] = mapped_column(ForeignKey("links.id"), nullable=False, index=True)
    tier: Mapped[str] = mapped_column(String(16), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    rule_matched: Mapped[str | None] = mapped_column(String(128), nullable=True)
    llm_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    llm_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    semantic_reused: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    link: Mapped[Link] = relationship(back_populates="classification_logs")


class VitalityCheck(Base):
    """Per-check record of link alive/dead status."""

    __tablename__ = "vitality_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    link_id: Mapped[int] = mapped_column(ForeignKey("links.id"), nullable=False, index=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    alive: Mapped[bool] = mapped_column(Boolean, nullable=False)
    response_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    link: Mapped[Link] = relationship(back_populates="vitality_checks")


class LLMProviderState(Base):
    """Tracks per-provider quota usage from response headers."""

    __tablename__ = "llm_provider_state"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    is_healthy: Mapped[bool] = mapped_column(Boolean, default=True)
    requests_today: Mapped[int] = mapped_column(Integer, default=0)
    requests_total: Mapped[int] = mapped_column(Integer, default=0)
    quota_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quota_remaining: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quota_resets_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuditEvent(Base):
    """Security & admin audit trail."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


# ============================================================
# v4.1 — Semantic Memory cache
# ============================================================

class SemanticCache(Base):
    """
    Cached classification keyed by embedding similarity.

    When a new link is classified, we store its embedding + result.
    Next time a similar URL (cosine sim > 0.92) is seen, we reuse the
    classification without calling the LLM. Saves quota + latency.
    """

    __tablename__ = "semantic_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    embedding_json: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    tier: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# ============================================================
# v4.1 — Daily summary
# ============================================================

class DailySummary(Base):
    """Daily trending-links summary, generated by cron job."""

    __tablename__ = "daily_summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    summary_date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)  # YYYY-MM-DD
    total_new: Mapped[int] = mapped_column(Integer, default=0)
    total_alive: Mapped[int] = mapped_column(Integer, default=0)
    total_dead: Mapped[int] = mapped_column(Integer, default=0)
    top_links_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    category_breakdown_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    sent_to_subscribers: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    __table_args__ = (UniqueConstraint("summary_date", name="uq_daily_summary_date"),)


# ============================================================
# v4.1 — Multi-tenant: tenants + tenant-scoped data
# ============================================================

class Tenant(Base):
    """A tenant in the multi-tenant SaaS deployment."""

    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    plan: Mapped[str] = mapped_column(String(32), default="free")  # free|pro|enterprise
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


# ============================================================
# v5.0 — Distributed userbot registry
# ============================================================

class WorkerRegistry(Base):
    """Tracks active distributed collectors (userbots) with heartbeats."""

    __tablename__ = "worker_registry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    worker_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    account_name: Mapped[str] = mapped_column(String(64), nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=True)
    pid: Mapped[int] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="starting")  # starting|active|idle|stopping|dead
    channels_assigned: Mapped[str] = mapped_column(Text, default="[]")  # JSON list
    links_collected: Mapped[int] = mapped_column(Integer, default=0)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# ============================================================
# v5.0 — SaaS billing
# ============================================================

class Subscription(Base):
    """Stripe-backed subscription for a tenant."""

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    plan: Mapped[str] = mapped_column(String(32), default="free")
    status: Mapped[str] = mapped_column(String(32), default="active")  # active|past_due|canceled
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    requests_this_period: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
