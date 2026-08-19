"""SQLAlchemy ORM models.

Every collectible/searchable row (`Channel`, `Link`) carries a
`workspace_id` foreign key. This is the concrete fix for R-03 (missing
tenant isolation): every query in the app must filter by the caller's
workspace, and there is no code path that returns another workspace's
data because the column does not exist to omit by accident.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.timeutil import utcnow


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    users: Mapped[list[User]] = relationship(back_populates="workspace")
    channels: Mapped[list[Channel]] = relationship(back_populates="workspace")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="member")  # owner | member
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    workspace: Mapped[Workspace] = relationship(back_populates="users")


class AuthSession(Base):
    """Server-side, revocable session record backing the login cookie."""

    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LoginAttempt(Base):
    """Failed/successful login records backing the brute-force lockout.

    Kept in the database rather than in process memory because the free
    Render web service sleeps and restarts freely — in-memory counters
    would reset on every cold start, which is exactly when an attacker
    would benefit. Rows are pruned past the throttle window, so the table
    stays small without a scheduled cleanup job.
    """

    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    identifier: Mapped[str] = mapped_column(String(320), index=True)
    successful: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class TelegramAccount(Base):
    """A userbot session (Telethon StringSession) used to collect messages."""

    __tablename__ = "telegram_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    label: Mapped[str] = mapped_column(String(100))
    session_string: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Channel(Base):
    __tablename__ = "channels"
    __table_args__ = (UniqueConstraint("workspace_id", "tg_channel_id", name="uq_channel_per_workspace"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("telegram_accounts.id"), nullable=True)
    tg_channel_id: Mapped[str] = mapped_column(String(64))
    username: Mapped[str | None] = mapped_column(String(200), nullable=True)
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    last_message_id: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    workspace: Mapped[Workspace] = relationship(back_populates="channels")
    links: Mapped[list[Link]] = relationship(back_populates="channel")


class Link(Base):
    __tablename__ = "links"
    # Dedupe by URL within a channel using a fixed-length hash rather than the
    # raw URL text itself: a btree unique index on unbounded Text can exceed
    # Postgres's index-entry size limit, a hash never can. A single Telegram
    # message can legitimately contain several distinct links, so the
    # uniqueness key is (channel_id, url_hash) rather than (channel_id,
    # message_id) — message_id is retained only as provenance / the
    # collector's watermark, not as part of the identity of a link.
    __table_args__ = (
        UniqueConstraint("channel_id", "url_hash", name="uq_link_per_channel_url"),
        # Search filters by workspace and orders newest-first; one composite
        # index serves both halves of that query.
        Index("ix_links_workspace_created", "workspace_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    message_id: Mapped[int] = mapped_column(Integer)
    url: Mapped[str] = mapped_column(Text)
    url_hash: Mapped[str] = mapped_column(String(64), index=True)
    domain: Mapped[str] = mapped_column(String(300), index=True)
    category: Mapped[str] = mapped_column(String(50), index=True, default="other")
    confidence: Mapped[float] = mapped_column(default=0.0)
    classified_by: Mapped[str] = mapped_column(String(20), default="rules")  # rules | llm
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    channel: Mapped[Channel] = relationship(back_populates="links")


class BotLinkCode(Base):
    """One-time code a logged-in web user generates to link a Telegram chat."""

    __tablename__ = "bot_link_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BotLink(Base):
    """A Telegram chat authorized to query one workspace through the bot."""

    __tablename__ = "bot_links"

    chat_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ActionEvent(Base):
    """Timestamped marker for a generic rate-limited action.

    Generalizes the login throttle (``LoginAttempt``) to any action worth
    rate-limiting — currently manual link submission. ``scope`` names the
    action (e.g. ``"link_add"``) and ``identifier`` is whatever the limit
    is scoped to (a workspace id, a user id, ...), so one table serves
    every throttle instead of a new table per feature.
    """

    __tablename__ = "action_events"
    __table_args__ = (Index("ix_action_events_scope_identifier_created", "scope", "identifier", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(String(50))
    identifier: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    """Append-only record of who did what. Resolves R-18 (no audit trail)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(100))
    target_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
