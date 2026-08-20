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
    # Recorded so "end this session" is an informed decision rather than a
    # guess between identical-looking rows. Nullable because sessions
    # created before this existed have no origin to report, and honestly
    # showing "unknown" beats inventing one.
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)  # 45 = max INET6 length
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)


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
    # Which origin the attempt came from. Failed attempts are pruned past
    # the throttle window, so this is a short-lived signal for "is someone
    # actively guessing my password", not a long-term audit trail.
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)


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
        # Lets the vitality checker pick "never checked, then longest since
        # last checked" without a full table scan.
        Index("ix_links_last_checked_at", "last_checked_at"),
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
    # Which rule actually fired, e.g. "extension:pdf" or "keyword:فيلم".
    # The classifier always computed this; it used to be thrown away, so a
    # wrong category could not be explained or debugged after the fact.
    matched_rule: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Where inside the message this URL came from: visible text, an inline
    # keyboard button, or a forward. A link only reachable through a button
    # looks identical to a pasted one once stored, which makes "why is this
    # here?" unanswerable without recording it.
    source_type: Mapped[str] = mapped_column(String(20), default="text", index=True)
    # For forwarded messages: the channel the content originally came from.
    forwarded_from: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # "ar", "en", or "mixed". Derived from the stored context, not declared
    # by the source, so it is a heuristic rather than metadata.
    language: Mapped[str | None] = mapped_column(String(10), nullable=True)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Vitality: None/None/None means "never checked yet" — distinct from a
    # confirmed-dead link, which has a real (usually >= 400) http_status.
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_alive: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # The last time a check actually succeeded. Kept separately from
    # last_checked_at so a currently-dead link can still answer "when was
    # this last working?" — which is what decides whether it is worth
    # hunting for a mirror or just deleting.
    last_alive_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Consecutive non-alive checks. Resets to 0 on any successful check.
    # Drives two things: the re-check backoff (a link that has failed
    # repeatedly is not worth probing every six hours), and the decision to
    # finally call a merely-unreachable link dead.
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    # Hides a link from default search results without deleting it. Dead
    # links accumulate and drown out live ones; deleting them loses the
    # record that the content once existed and where.
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    channel: Mapped[Channel] = relationship(back_populates="links")

    @property
    def status_category(self) -> str:
        """Human-meaningful reading of ``http_status`` / ``is_alive``.

        A plain Python property rather than a stored column: it is a pure
        function of two columns that are already there, so persisting it
        would only create a third value that can disagree with them.
        """
        from app.vitality import status_category

        return status_category(self.http_status, self.is_alive)


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


class ClassificationFeedback(Base):
    """A human correcting the classifier, kept as an auditable trail.

    Serves two purposes that would otherwise need two tables. It is the
    "this is wrong" signal a person sends from the dashboard, and it is the
    history of how one link's category changed over time — the same rows
    answer both questions.

    Rows outlive the link they describe (``link_id`` is not a foreign key
    with a cascade) on purpose: the most useful correction to learn from is
    often on a link that was later deleted, and losing it with the link
    would throw away exactly the data that improves the rules.
    """

    __tablename__ = "classification_feedback"
    __table_args__ = (Index("ix_feedback_workspace_created", "workspace_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    link_id: Mapped[int] = mapped_column(Integer, index=True)
    url: Mapped[str] = mapped_column(Text)
    previous_category: Mapped[str] = mapped_column(String(50))
    new_category: Mapped[str] = mapped_column(String(50))
    previous_confidence: Mapped[float] = mapped_column(default=0.0)
    previous_matched_rule: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
