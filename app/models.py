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
    BigInteger,
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
    # --- optional second factor (idea 79) --------------------------------
    # Encrypted, not hashed: verifying a code needs the original secret
    # back, exactly like a Telethon session string. A dump holding these in
    # plaintext would be a second-factor bypass, not merely a data leak.
    totp_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Separate from "a secret exists": setup writes the secret first and
    # only flips this once a real code proves the authenticator works, so a
    # mistyped setup cannot lock the account out.
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # The last accepted time step. A TOTP code stays valid for its whole
    # step, so without this an observed code can be replayed for the rest
    # of that window.
    totp_last_step: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # SHA-256 of each unused recovery code, newline separated. Hashed
    # because these never need reading back, only comparing — and they are
    # single-use bearer credentials that bypass the second factor entirely.
    totp_recovery_hashes: Mapped[str | None] = mapped_column(Text, nullable=True)

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
    # When this account last completed a collection run, and last failed
    # one. Kept as two timestamps rather than one "last run": a run that
    # failed tells you nothing about when the account last actually worked,
    # which is the question you ask when deciding whether to re-authorise it.
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # The most recent failure's message, truncated. Shown to the operator
    # because "this account is failing" without saying how is a dead end —
    # a revoked session and a network blip need opposite responses.
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Consecutive failed runs. Reset to zero by any success. Drives the
    # automatic disable, so that a revoked account stops being retried
    # every hour forever with nobody noticing.
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    # Why the account is not active, when it was disabled automatically.
    # NULL on an account a human disabled, which is the distinction that
    # matters: one needs investigating, the other was intended.
    disabled_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Links this account has ever collected. A running counter rather than
    # a join through channels: channels can be reassigned between accounts,
    # so a computed join would retroactively credit one account's history
    # to another. The counter records what actually happened.
    links_collected: Mapped[int] = mapped_column(Integer, default=0)
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
        # Serves the top-domains panel as an index-only scan. The column
        # order is not cosmetic and neither is the query that uses it:
        # measured on 50k rows, (workspace_id, domain) alone was ignored by
        # the planner, and this index with count(id) was ignored too —
        # because id is not in the index, so no index-only scan is
        # possible. Only this index *together with* count(*) works:
        # 10.7ms/1118 buffers -> 1.9ms/13 buffers, Heap Fetches: 0.
        Index("ix_links_ws_archived_domain", "workspace_id", "is_archived", "domain"),
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
    # A free-text note the user writes about this link. Separate from
    # raw_text, which is what the *source message* said — conflating the
    # two would let an edit destroy the original context.
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Distinct from is_favorite on purpose. "Favourite" is affection;
    # "pinned" is "this is the reference I keep coming back to". Merging
    # them would mean starring a link you like buries the one you rely on.
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # Incremented by the /r/{id} redirect. Zero means "never opened through
    # the redirect", which is not the same as "never opened" — a copied URL
    # is invisible here, and the dashboard says so rather than implying the
    # count is complete.
    click_count: Mapped[int] = mapped_column(Integer, default=0)
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
    # Where the request came from, when the platform knows. Nullable for
    # two distinct reasons that both mean "no address": an action taken by
    # a background job rather than a request, and every row written before
    # this column existed.
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ApiKey(Base):
    """A personal credential for programmatic access, in place of the cookie.

    Stored exactly like a session token — hashed, never recoverable — for
    the same reason: a database dump must not hand anyone a working
    credential. The raw key is shown once, at creation, and never again.

    What it deliberately cannot do is in ``app/apikeys.py``. The short
    version: a key reads and writes links, and cannot destroy the account
    or mint another key.
    """

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # The leading characters of the raw key, in the clear. Purely so a
    # person can tell two keys apart in a list — a name alone stops
    # helping the moment someone has to match a key they pasted into a
    # script against a row on screen. Too short to narrow a brute force.
    prefix: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Nullable because a key that has never been used is a real state, and
    # the useful question ("is this one still in use?") needs to tell it
    # apart from "used long ago".
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    use_count: Mapped[int] = mapped_column(Integer, default=0)

    user: Mapped[User] = relationship()


class NotificationPreference(Base):
    """Whether one alert type is on for one workspace.

    Sparse on purpose: a row exists only where the choice *differs* from
    the default in ``app/alerts.py``. Materialising every type for every
    workspace would mean a backfill today and another one every time a
    type is added, and would freeze the defaults at the moment of the
    migration — so a default the project later decides was wrong would
    keep applying to everyone who never touched it.
    """

    __tablename__ = "notification_preferences"
    __table_args__ = (UniqueConstraint("workspace_id", "alert_type", name="uq_notification_pref_workspace_type"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    alert_type: Mapped[str] = mapped_column(String(50))
    enabled: Mapped[bool] = mapped_column(Boolean)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Notification(Base):
    """One alert that was raised, whether or not it reached a chat.

    Serves three backlog items with one table, deliberately: the
    dashboard's notification centre (156), the auditable record of what
    was sent (161), and the recent-activity strip (165) are the same rows
    read three ways. Three tables would have made "was I told about this?"
    a question with three possible answers.

    Recorded even when delivery fails or no chat is linked, because "the
    platform noticed" and "you were told" are different facts and the gap
    between them is the interesting one.
    """

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_ws_created", "workspace_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    alert_type: Mapped[str] = mapped_column(String(50), index=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    # How many linked chats actually received it. Zero is a normal state
    # (no bot linked) and is not an error — but it is worth being able to
    # see, because "no alerts arrived" and "no alerts were raised" look
    # identical from the outside otherwise.
    delivered_count: Mapped[int] = mapped_column(Integer, default=0)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class WorkflowRun(Base):
    """The outcome of one scheduled GitHub Actions run, as reported by it.

    Ideas 181 and 183 describe *reading* the GitHub Actions API to show
    workflow history. That would mean storing a GitHub token with repo
    scope in this database — a credential strictly more powerful than
    anything else here, held by a service whose whole security model has
    been built around not holding such things.

    So the direction is inverted: the workflow **pushes** its result here
    when it finishes, authenticating with a personal API key (phase 8a)
    that can already do far less than a GitHub token. Nothing new is
    stored, no new scope is granted, and a leaked key still cannot touch
    the repository.

    The cost of the inversion is stated rather than hidden: a run that
    never starts reports nothing, so "no recent row" means *either*
    healthy-and-idle or the workflow is not running at all. That is
    exactly what ``looks_stalled`` on the collector already handles, and
    the same reading applies here.
    """

    __tablename__ = "workflow_runs"
    __table_args__ = (Index("ix_workflow_runs_ws_name_started", "workspace_id", "name", "started_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(100), index=True)
    # "success" | "failure" | "cancelled" — whatever the workflow reports.
    conclusion: Mapped[str] = mapped_column(String(30))
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # The commit the run was made from, so a failure can be tied to code.
    commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __mapper_args__ = {"eager_defaults": True}


class SavedSearch(Base):
    """A filter combination someone wants back with one click.

    Stored server-side rather than in the browser so the same saved
    searches are there on a phone and a laptop. The whole filter set is
    kept as one JSON blob instead of a column per filter: filters are
    added every other batch, and a schema migration per new filter would
    make adding one more expensive than it should be. Nothing queries
    *inside* this value — it is read back whole and handed to the search
    endpoint — so there is no index to lose by not normalising it.
    """

    __tablename__ = "saved_searches"
    __table_args__ = (
        # Two saved searches with the same name in one workspace would be
        # indistinguishable in the UI that lists them.
        UniqueConstraint("workspace_id", "name", name="uq_saved_search_per_workspace"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    # JSON-encoded query parameters, e.g. {"q": "كتاب", "category": "books_courses"}.
    filters: Mapped[str] = mapped_column(Text)
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
    __table_args__ = (
        Index("ix_feedback_workspace_created", "workspace_id", "created_at"),
        # Idea 163 groups corrections by site, and the URL cannot serve as
        # that key: two corrections on the same domain are two URLs.
        Index("ix_feedback_workspace_domain", "workspace_id", "domain"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    link_id: Mapped[int] = mapped_column(Integer, index=True)
    url: Mapped[str] = mapped_column(Text)
    # Derived from ``url`` at write time. Stored rather than computed on
    # read because it is a grouping key, and a grouping key computed in
    # Python means loading every row to group any of them.
    domain: Mapped[str] = mapped_column(String(300), default="", server_default="")
    previous_category: Mapped[str] = mapped_column(String(50))
    new_category: Mapped[str] = mapped_column(String(50))
    previous_confidence: Mapped[float] = mapped_column(default=0.0)
    previous_matched_rule: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
