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
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.identity import source_identity_key
from app.timeutil import utcnow


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Idea 162. Encrypted rather than plain because incoming-webhook URLs
    # from every major service carry a secret token in the path — holding
    # one is holding the ability to post into somebody's channel, which
    # puts it in the same class as a Telegram session string, not in the
    # same class as a hostname.
    webhook_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The last attempt's outcome, so "is my webhook working?" is
    # answerable from the dashboard. Kept for the same reason a
    # notification is recorded even when it could not be delivered.
    webhook_last_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    webhook_last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

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
    """A userbot session (Telethon StringSession) used to collect messages.

    **Identity is the Telegram user, not the row.** ``tg_user_id`` is the
    stable one: a label is a nickname, a phone can be changed, and a
    session string is replaced every time the account is re-authorised —
    keying on any of those would make re-authentication produce a second
    account holding none of the first one's assignments or history. It is
    nullable because nothing recorded it before this phase, and UNKNOWN is
    the honest value for a row that predates it.
    """

    __tablename__ = "telegram_accounts"
    __table_args__ = (
        # ``state`` is the operational truth and ``is_active`` is the
        # boolean the collector and the dashboard already filter on. Rather
        # than a second authority, they are bound: exactly one state means
        # active, and the database refuses any write that sets one without
        # the other. No service, no trigger, no call site to remember.
        CheckConstraint(
            "(state = 'ACTIVE') = is_active",
            name="ck_account_state_matches_is_active",
        ),
        # The identity, when it is known. Two rows for one Telegram user in
        # one workspace is the duplicate this prevents; NULLs stay distinct,
        # so rows that predate the column do not collide with each other.
        UniqueConstraint("workspace_id", "tg_user_id", name="uq_account_identity"),
    )

    #: Working, and eligible to be assigned sources.
    ACTIVE = "ACTIVE"
    #: Switched off by a person. Nothing is wrong with it.
    INACTIVE = "INACTIVE"
    #: Switched off by the system after repeated failure.
    DISABLED = "DISABLED"
    #: The session is gone or was never completed; a person must re-authorise.
    AUTH_REQUIRED = "AUTH_REQUIRED"
    #: Re-authorisation was attempted and refused.
    AUTH_FAILED = "AUTH_FAILED"
    #: Reachable in principle, not usable now (Telegram-side outage, ban).
    UNAVAILABLE = "UNAVAILABLE"
    #: Telegram is throttling this account; it recovers on its own.
    RATE_LIMITED = "RATE_LIMITED"
    #: Something inconsistent that a person has to look at.
    NEEDS_REVIEW = "NEEDS_REVIEW"

    STATES = (
        ACTIVE,
        INACTIVE,
        DISABLED,
        AUTH_REQUIRED,
        AUTH_FAILED,
        UNAVAILABLE,
        RATE_LIMITED,
        NEEDS_REVIEW,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    label: Mapped[str] = mapped_column(String(100))
    session_string: Mapped[str] = mapped_column(Text)
    # The Telegram user this session belongs to. NULL means never observed:
    # it is read off the connection, and nothing has connected as this
    # account since the column existed.
    tg_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Why the account is in the state it is in, and since when. Separate
    # from ``last_error``: that is the last thing that went wrong, this is
    # the reason for the current state, and a healthy account can have the
    # first without the second.
    state: Mapped[str] = mapped_column(String(20), default=ACTIVE, server_default=ACTIVE)
    state_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    state_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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
    __table_args__ = (
        UniqueConstraint("workspace_id", "tg_channel_id", name="uq_channel_per_workspace"),
        # Identity lookups are always tenant-scoped, so the workspace leads.
        # Not unique yet — see the identity_key column for why.
        Index("ix_channels_identity_key", "workspace_id", "identity_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("telegram_accounts.id"), nullable=True)
    tg_channel_id: Mapped[str] = mapped_column(String(64))
    username: Mapped[str | None] = mapped_column(String(200), nullable=True)
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Which sort of dialog this row stands for: "channel", "group" or
    # "private". A row is still a row — the collector, the ingest path and
    # search treat all three identically — but the distinction is real to
    # a reader, and collection scope is configured per kind, so it is
    # stored rather than inferred from the id's sign at read time.
    kind: Mapped[str] = mapped_column(String(16), default="channel", server_default="channel")
    # Which reader owns this row: "userbot" (MTProto) or "public" (the
    # t.me web preview, which needs no account at all).
    #
    # Not descriptive — protective. ``last_message_id`` below is a single
    # watermark. Two readers on one row means whichever finishes last moves
    # it past messages the other never read, and those are skipped
    # permanently with nothing raised. One row, one reader.
    source: Mapped[str] = mapped_column(String(20), default="userbot", server_default="userbot")
    last_message_id: Mapped[int] = mapped_column(Integer, default=0)
    # When the scheduled collector last read this dialog. Ordering by it
    # (never-collected first) is what keeps the per-run channel cap a
    # rotation rather than a cliff once an account holds more dialogs than
    # one run may touch.
    last_collected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # --- what the measurement contract (§46) reads -----------------------
    #
    # ``last_collected_at`` above means "last *successful* read", which is
    # what the rotation ordering needs. It cannot answer "was this source
    # attempted and did it fail?", and coverage is meaningless without
    # that: a source nobody tried and a source that failed both look like
    # a source with an old timestamp.
    #
    # **Every column in this block is DERIVED.** The measurement contract
    # (§46) and the collection runtime (§50) were built on separate
    # branches and arrived at the same facts twice; the reconciliation
    # settled which copy decides. These four are the compatibility copies,
    # written in exactly one place each so that no reader can find them
    # disagreeing with the row they reflect:
    #
    #   last_attempt_at    <- source_progress.last_attempt_at   (LIVE track)
    #   caught_up          <- source_progress.coverage_status, projected
    #   watermark_regressions <- counted from app.progress refusals
    #   last_outcome / last_failure_kind
    #                      <- the latest collection_runs row for this source
    #
    # The first three are written by ``app.progress`` alone; the last two
    # by ``scripts/collect._record_outcome`` alone. Nothing else may write
    # any of them, for the reason ``account_id`` above carries a database
    # trigger: two writers of one fact is how the two branches produced two
    # answers in the first place.
    #
    # AUTHORITATIVE: source_progress · collection_runs
    # DERIVED / LEGACY COMPATIBILITY: everything below, until every reader
    # has moved to the authority and they can be dropped.
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: "succeeded" | "failed" | "skipped" — see app/coverage.py. DERIVED
    #: from the newest ``collection_runs`` row; that table keeps the series,
    #: this column keeps only the latest value.
    last_outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)
    #: One of app.coverage.FAILURE_KINDS. NULL on success, and never the
    #: exception class: "failed" tells an operator something is wrong,
    #: "access_denied" tells them what to do about it. DERIVED, as above.
    #:
    #: Note the two vocabularies are deliberately *not* merged:
    #: ``app.coverage.FAILURE_KINDS`` is what the measurement reports and
    #: ``app.collection.failures.FailureKind`` is what the runtime acts on.
    #: One is a operator-facing summary, the other drives retry policy.
    last_failure_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Whether the last successful read reached the end of the channel.
    #: False means the per-run cap stopped it with a backlog remaining —
    #: not an error, an unfinished window. NULL means never read.
    #:
    #: DERIVED from ``source_progress.coverage_status``, and lossy on
    #: purpose: the enum can say UNKNOWN_COVERAGE, a boolean cannot, and
    #: NULL is the only honest rendering of "cannot tell". Never read this
    #: to decide whether a gap exists — read the enum.
    caught_up: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: Times this source's watermark was asked to move backwards. Must
    #: stay zero; it is a counter rather than a flag so the *rate* is
    #: visible if it ever stops being zero.
    #:
    #: DERIVED: the authoritative event is the refusal in
    #: ``app.progress.advance`` (and, on PostgreSQL, the trigger from
    #: migration 0031 that makes the write impossible at all). This counter
    #: is incremented from that one verdict, never from a second comparison.
    watermark_regressions: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # --- target source model (phase 1) ---------------------------------
    # One spelling of this dialog's identity, so that the same channel
    # written ``-1001234567890`` by Telethon and ``1234567890`` by a person
    # is one source rather than two.
    #
    # ``tg_channel_id`` above keeps whatever spelling arrived — it is what
    # ``client.get_entity`` accepts back, so rewriting it would break
    # resolution — and this column carries the comparable form. The rule is
    # ``app.dialogs.canonical_id``; synthetic rows ("manual", "import:...")
    # keep their own id, because they are identities too, just not
    # Telegram's.
    #
    # Indexed, **not yet unique**: the current schema allowed both
    # spellings to be inserted through ``get_or_create_channel``, so a
    # deployment may already hold a colliding pair. Merging two sources
    # decides which watermark and which links survive, which is a product
    # decision and not a migration's to make. scripts/check_source_identity.py
    # reports collisions; the constraint follows once a deployment is clean.
    identity_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # How this source is read: "userbot" needs an account with access,
    # "public" needs none. Migrated from ``source`` above, which already
    # carries exactly this fact — the new name is the target's word for it,
    # and the old column stays as the live one until its readers move.
    #
    # NULL on synthetic rows: "manual" is not acquired from anywhere.
    acquisition_method: Mapped[str | None] = mapped_column(String(30), nullable=True)

    workspace: Mapped[Workspace] = relationship(back_populates="channels")
    links: Mapped[list[Link]] = relationship(back_populates="channel")


class Message(Base):
    """One Telegram message this system has finished processing.

    The identity that ``links`` never had. A link is unique per
    ``(channel_id, url_hash)``, which answers "have I stored this URL from
    this channel?" — and cannot answer "have I already read this message?"
    A message carrying no links at all had no answer of any kind: it left
    no trace, so the live listener and the scheduled collector each did its
    work from scratch every time they overlapped on one.

    **There is deliberately no ``text`` column.** The claim that prompted
    this table — that a message with twenty links stores its text twenty
    times — does not survive reading ``split_context``: each link gets its
    own *slice* of the message, and only a single-link message keeps the
    whole body. Adding ``text`` here would therefore not remove a
    duplication; it would create one, and it would turn a 1 GiB free-tier
    database into a full Telegram archive within weeks. The text stays
    where it is already used: the per-link context on ``Link.raw_text``,
    which search indexes, and the request text on ``Lead``.

    **Rows are written lazily** — only once a message has produced
    something durable (a link, or a matched lead) and that work has
    committed. So the presence of a row means "fully processed", which is
    what makes the early return in ``ingest_text`` correct rather than
    approximate: an interrupted run leaves no row, and the message is
    reprocessed rather than silently skipped.
    """

    __tablename__ = "messages"
    __table_args__ = (
        # Identity is per channel, not per workspace: the same message id
        # in two channels is two messages.
        UniqueConstraint("channel_id", "tg_message_id", name="uq_message_per_channel"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    tg_message_id: Mapped[int] = mapped_column(Integer)
    # Attribution, read off the message rather than fetched: see
    # scripts/collect.py for why an extra Telegram round trip per message
    # is not paid here. Cleared by app.leads.forget() when a person asks to
    # be forgotten — the message survives because it is the provenance of a
    # *link*, but it stops naming them.
    sender_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    sender_username: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sender_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Three different times, and the reason they are three columns:
    # ``posted_at`` is Telegram's, ``collected_at`` is when this system saw
    # it (the target model calls that observed_at — the column keeps its
    # name because the API schema exposes it), and ``processed_at`` is when
    # the extraction pipeline finished with it. Using the last as a stand-in
    # for the first is what turns a six-hour-old backfill into a "six-hour
    # collection lag".
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # NULL means not processed under the new pipeline — which is every
    # migrated row, because nothing recorded this before. Filled by the
    # phase that owns extraction, not guessed here.
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Which path produced this observation: userbot | public. NULL on
    # migrated rows: the legacy schema recorded the path on the source, and
    # the source's value today is not evidence about an old sighting.
    acquisition_path: Mapped[str | None] = mapped_column(String(20), nullable=True)


class CoverageSnapshot(Base):
    """One run's measurement, kept so the trend is visible (§46 → §47).

    ``GET /status/coverage`` answers "how are we doing right now", and a
    right-now number cannot answer the question that actually matters:
    **is it getting worse?** 99.2%, then 98.7%, then 94.1% is a system
    degrading in plain sight, and each of those readings on its own looks
    acceptable. So each run writes a row, and the series is the signal.

    PostgreSQL rather than a metrics stack, deliberately. Prometheus and
    Grafana would each be a service to run, on a deployment whose whole
    constraint is that it runs one; a table with a retention window costs
    nothing and answers the same question.
    """

    __tablename__ = "coverage_snapshots"
    __table_args__ = (Index("ix_coverage_workspace_finished", "workspace_id", "finished_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    #: Groups every row a single run wrote. A run that collects for two
    #: workspaces writes two rows sharing one id, so "what did that run
    #: do?" stays answerable after the fact.
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[datetime] = mapped_column(DateTime)

    # --- source coverage, copied from the contract at run end ------------
    sources_expected: Mapped[int] = mapped_column(Integer, default=0)
    sources_due: Mapped[int] = mapped_column(Integer, default=0)
    sources_attempted: Mapped[int] = mapped_column(Integer, default=0)
    sources_succeeded: Mapped[int] = mapped_column(Integer, default=0)
    sources_failed: Mapped[int] = mapped_column(Integer, default=0)
    sources_skipped: Mapped[int] = mapped_column(Integer, default=0)

    # --- what the run actually moved -------------------------------------
    #: Messages the run was offered, including ones it recognised as
    #: already processed. ``processed`` is the subset it did work on, so
    #: ``seen - processed`` is the overlap between the two readers.
    messages_seen: Mapped[int] = mapped_column(Integer, default=0)
    messages_processed: Mapped[int] = mapped_column(Integer, default=0)
    links_found: Mapped[int] = mapped_column(Integer, default=0)
    links_stored: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_occurrences: Mapped[int] = mapped_column(Integer, default=0)

    # --- freshness, as a distribution rather than an average -------------
    #: An average lag hides the one source that is a day behind. The
    #: median says what a typical source looks like; p95 says how bad the
    #: tail is, and the tail is what breaks first.
    collection_lag_p50: Mapped[float | None] = mapped_column(nullable=True)
    collection_lag_p95: Mapped[float | None] = mapped_column(nullable=True)

    # --- integrity, never folded into the rates above --------------------
    watermark_regressions: Mapped[int] = mapped_column(Integer, default=0)
    gap_events: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


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
    # The message row this link was extracted from, when there is one.
    # Nullable on purpose and permanently: links added by hand or imported
    # from a bookmarks file have no Telegram message behind them, and rows
    # collected before ``messages`` existed have none either. NULL here is
    # a true statement about the link's origin, not missing data.
    message_ref_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id"), nullable=True, index=True)
    url: Mapped[str] = mapped_column(Text)
    url_hash: Mapped[str] = mapped_column(String(64), index=True)
    domain: Mapped[str] = mapped_column(String(300), index=True)
    # Which service the link points at — Telegram, WhatsApp, YouTube, a
    # plain web page. Deliberately a second axis beside ``category``, not a
    # replacement for it: a t.me link can be a film or a course, and a
    # course can live on Telegram or on a university's own site. One column
    # would force a choice between "how many Telegram links" and "how many
    # course links", and both are questions people actually ask.
    #
    # Stored rather than computed per query because it is filtered and
    # grouped on; derived deterministically from ``url`` by
    # ``app.classifier.platform.link_platform``.
    platform: Mapped[str] = mapped_column(String(20), index=True, default="web", server_default="web")
    category: Mapped[str] = mapped_column(String(50), index=True, default="other")
    confidence: Mapped[float] = mapped_column(default=0.0)
    # Which classifier version produced this row's category, e.g.
    # "rules-v2", or "manual" when a human corrected it. It used to say
    # only "rules" or "llm"; with the LLM tier removed (§43) that made it
    # a constant, so it now carries the version instead — which answers
    # "why did this row get this category?" months later, when the rules
    # have moved on and the row has not.
    classified_by: Mapped[str] = mapped_column(String(20), default="rules-v2")
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
    """A Telegram chat authorized to query one workspace through the bot.

    The three ``last_*`` columns are the chat's most recent result filter.
    They exist because the number in a result line ("3. [news] ...") is a
    position *within the active filter and page*, while ``/details 3`` had
    no way to know what that filter was and re-queried the unfiltered list
    — so after any search it answered with a different link than the one
    numbered 3 on screen.

    A Telegram callback arrives with no memory of what produced it, and
    ``callback_data`` is capped at 64 bytes, so the filter cannot ride the
    button either: a search term over 30 characters was silently truncated
    and page 2 listed results from a *different* query than page 1.

    Scoped per chat, which is the trade-off worth naming: two people
    searching simultaneously in the same group chat share one context and
    the later search wins. Per-result buttons carry their own link id and
    are unaffected.
    """

    __tablename__ = "bot_links"

    chat_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_query: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_favorite: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


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


# --- lead detection (idea 2: watching for people who need help) ------------
#
# This is the point where a link archive becomes a database about *people*,
# and that is a decision rather than a default. Everything below is inert
# until LEADS_ENABLED is set: no rows are written, nothing is matched, and
# the interface says so.


class Beneficiary(Base):
    """A person seen asking for something in a monitored dialog.

    The field list is short on purpose, and what is *absent* is the design:
    no phone number, no email, no message archive per person. Telegram
    exposes a phone on some peers and storing it would turn a lead list
    into a contact database nobody consented to.

    ``tg_user_id`` is the identity. A username can be changed or dropped
    and a display name is not unique, so keying on either would either
    split one person across rows or merge two people into one.
    """

    __tablename__ = "beneficiaries"
    __table_args__ = (UniqueConstraint("workspace_id", "tg_user_id", name="uq_beneficiary_per_workspace"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    tg_user_id: Mapped[str] = mapped_column(String(64), index=True)
    username: Mapped[str | None] = mapped_column(String(200), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    # How many requests this person has been matched on. Kept as a counter
    # rather than derived, because the leads it counts are purged on a
    # retention schedule and the count of "how often has this person asked"
    # is worth keeping after the texts themselves are gone.
    request_count: Mapped[int] = mapped_column(Integer, default=0)


class KeywordRule(Base):
    """One phrase the workspace watches for, and what it is worth.

    Weights rather than a flat list: "مشروع تخرج" is a far stronger signal
    than "مساعدة", and treating them the same means either drowning in
    noise or missing the real requests. The sum of matched weights is the
    seriousness score.
    """

    __tablename__ = "keyword_rules"
    __table_args__ = (UniqueConstraint("workspace_id", "phrase", name="uq_keyword_per_workspace"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    phrase: Mapped[str] = mapped_column(String(200))
    weight: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Lead(Base):
    """One message that matched the workspace's keywords.

    ``(workspace_id, channel_id, message_id)`` is unique because the same
    message is seen twice in normal operation — the live listener catches
    it as it arrives and the hourly collector reads it again from history.
    Without the constraint every restart would duplicate the pipeline.
    """

    __tablename__ = "leads"
    __table_args__ = (
        UniqueConstraint("workspace_id", "channel_id", "message_id", name="uq_lead_per_message"),
        Index("ix_leads_workspace_created", "workspace_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    beneficiary_id: Mapped[int | None] = mapped_column(ForeignKey("beneficiaries.id"), nullable=True, index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    message_id: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    # Which phrases fired, comma separated. Without this a score is a number
    # with no explanation, and "why was this flagged" is the first question
    # anyone asks about a false positive.
    matched: Mapped[str] = mapped_column(String(300), default="")
    score: Mapped[int] = mapped_column(Integer, default=0, index=True)
    # new | contacted | converted | ignored
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --- Phase 1: the target source/resource model -----------------------------
#
# Everything below exists to hold four separations the current schema
# cannot express, and that the target product treats as load-bearing:
#
#     Source     ≠ Resource        a dialog is not a link
#     Resource   ≠ Occurrence      one link seen 100 times is 1 + 100
#     Identity   ≠ State           a source stays itself when it goes private
#     Assignment ≠ Collection      being responsible is not having collected
#
# The tables are **additive**. No existing column changed meaning and no
# reader was repointed, so the legacy path keeps working unchanged while
# the new shape is filled in. ``Channel`` remains the physical Source table
# and ``links`` remains the legacy link store; both are mapped, neither is
# rewritten. Retiring them is a later phase's work, after the readers move.


class SourceAccess(Base):
    """Whether one path can actually read one source, and when we saw that.

    Access is **not** a property of the source. The same channel can be
    readable by account 3, invisible to account 7, and available over the
    public path all at once, so the fact has to live on the relationship
    rather than on either end of it.

    A missing row means *never evaluated* — which is the honest state for
    almost everything today, and the reason this table is not backfilled
    with a row per source. Only pairs with real evidence get a row: a
    source that has actually been collected by an account proves that
    account could read it at that moment.

    ``observed_at`` is when the access was last *seen* to hold, not when
    the row was written. State and observation are different facts, and
    conflating them is what turns "worked an hour ago" into "works now".
    """

    __tablename__ = "source_access"
    __table_args__ = (
        # One row per (source, path, account). ``COALESCE`` rather than a
        # plain unique constraint because the public path has no account,
        # and NULLs do not compare equal — so a bare constraint would let
        # the same public path be recorded any number of times.
        Index(
            "uq_source_access_path",
            "source_id",
            "path_kind",
            text("COALESCE(account_id, -1)"),
            unique=True,
        ),
        Index("ix_source_access_workspace_state", "workspace_id", "state"),
        CheckConstraint(
            "state IN ('UNKNOWN', 'ACCESSIBLE', 'INACCESSIBLE', 'NEEDS_ACCESS', "
            "'REQUEST_SENT', 'ACCESS_DENIED', 'BLOCKED')",
            name="ck_source_access_state",
        ),
    )

    #: Nothing has been measured. **Not** the same as INACCESSIBLE: one is
    #: an absent measurement, the other is a failed one, and treating them
    #: alike is how a source nobody has tried becomes a source that does
    #: not work.
    UNKNOWN = "UNKNOWN"
    #: The system reached the source over this path, and has evidence.
    ACCESSIBLE = "ACCESSIBLE"
    #: The path was tried and did not work. Not the same as an invalid source.
    INACCESSIBLE = "INACCESSIBLE"
    #: A valid source that needs membership or authorisation nobody has yet.
    NEEDS_ACCESS = "NEEDS_ACCESS"
    #: A join or access request went out and has not been answered.
    #: **Not** a grant. The distinction is the whole reason this state
    #: exists: a request that is pending looks like progress and is not
    #: access, and a system that conflates them reports coverage it does
    #: not have.
    REQUEST_SENT = "REQUEST_SENT"
    #: The request was answered, and the answer was no.
    ACCESS_DENIED = "ACCESS_DENIED"
    #: Refused by policy or by Telegram in a way retrying will not fix.
    BLOCKED = "BLOCKED"

    STATES = (
        UNKNOWN,
        ACCESSIBLE,
        INACCESSIBLE,
        NEEDS_ACCESS,
        REQUEST_SENT,
        ACCESS_DENIED,
        BLOCKED,
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    # NULL for a path that needs no account (the public preview). Not a
    # missing value — the absence *is* the statement.
    account_id: Mapped[int | None] = mapped_column(ForeignKey("telegram_accounts.id"), nullable=True, index=True)
    # "userbot" | "public" — the same vocabulary ``Channel.source`` uses,
    # so one word does not mean two things in two places.
    path_kind: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(20))
    # When the state above was last observed to be true. Nullable because
    # a row can record a conclusion drawn without a fresh probe.
    observed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    evidence_id: Mapped[int | None] = mapped_column(ForeignKey("evidence.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SourceAssignment(Base):
    """Which account is responsible for collecting a source, over time.

    ``Channel.account_id`` answers "who collects this?" and nothing else:
    it cannot say when the answer changed, why, or what the previous one
    was, and it cannot distinguish *assigned* from *collected*. This table
    is that column with a history and a reason attached; the column stays
    as the live pointer until the readers move off it.

    **At most one open assignment per source** is enforced in the database
    by a partial unique index, not by convention. Two open assignments
    mean two writers on one watermark, which is the failure this model
    exists to make impossible rather than unlikely.

    A closed row (``released_at`` set) is history and is never deleted.
    """

    __tablename__ = "source_assignments"
    __table_args__ = (
        # The Primary Collector invariant. Partial, so closed rows may
        # accumulate freely — history is the point.
        Index(
            "uq_source_assignment_open",
            "source_id",
            unique=True,
            sqlite_where=text("released_at IS NULL"),
            postgresql_where=text("released_at IS NULL"),
        ),
        Index("ix_source_assignments_account", "account_id", "released_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("telegram_accounts.id"), index=True)
    # Nullable on purpose, and the nullability carries meaning: rows
    # migrated from the scalar ``Channel.account_id`` have no assignment
    # time because that column never recorded one. Inventing a timestamp
    # here would be inventing history.
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Free text, written by whatever made the decision. Not an enum: the
    # set of real reasons is not known yet, and freezing a guess into a
    # constraint is worse than reading a sentence.
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    evidence_id: Mapped[int | None] = mapped_column(ForeignKey("evidence.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SourceEvent(Base):
    """One recorded transition in a source's life.

    The current state of a source answers "what is it now"; this answers
    "what happened to it", which is the question that survives the next
    change. Public → Private → Needs Access → Collecting is a history, and
    a status column can only ever hold its last frame.

    **Deliberately not backfilled.** Every existing row would need a
    previous state, a time and a reason that nothing in the current schema
    records, so a backfill could only fabricate them. An empty table is a
    true statement about what the system knows.
    """

    __tablename__ = "source_events"
    __table_args__ = (Index("ix_source_events_source_time", "source_id", "occurred_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    # e.g. "access_state", "acquisition_method", "assignment", "identity".
    # Which dimension moved, so the history of one dimension can be read
    # without filtering on the shape of the values.
    dimension: Mapped[str] = mapped_column(String(40))
    previous_state: Mapped[str | None] = mapped_column(String(40), nullable=True)
    new_state: Mapped[str | None] = mapped_column(String(40), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Who or what caused it: a user id, a script name, a worker.
    actor: Mapped[str | None] = mapped_column(String(100), nullable=True)
    evidence_id: Mapped[int | None] = mapped_column(ForeignKey("evidence.id"), nullable=True)


class Evidence(Base):
    """Why the system believed something, kept apart from who did what.

    Audit (``AuditLog``) records administrative acts by people. Evidence
    records observations by the system: what it saw, over which path, at
    what time. Using one for the other is how "the operator paused it"
    and "the account could not read it" end up indistinguishable.

    Scope is deliberately narrow in this phase. Rows are written only
    where an observation genuinely exists — today that is the access
    backfill. Decision-level evidence for acquisition, classification and
    recovery arrives with the phases that make those decisions; claiming
    it now would be claiming coverage that does not exist.
    """

    __tablename__ = "evidence"
    __table_args__ = (Index("ix_evidence_workspace_kind", "workspace_id", "kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    # What sort of observation this is, e.g. "access_probe",
    # "collection_history", "identity_resolution".
    kind: Mapped[str] = mapped_column(String(40))
    # When the thing was observed — not when the row was written.
    observed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # One line a human can read without decoding anything.
    summary: Mapped[str] = mapped_column(String(300))
    # Anything structured the writer wants to keep, as JSON text. Not a
    # JSON column: SQLite and Postgres disagree about those, and nothing
    # queries inside this value.
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Resource(Base):
    """One link, once, however many times it has been seen.

    ``links`` is unique per ``(channel_id, url_hash)``, which makes the
    same URL in three channels three rows and the same URL twice in one
    channel *nothing at all* — the second insert is rejected as a
    duplicate and no trace of the repeat survives. Both halves are wrong
    for the target: the first inflates the link count, the second throws
    away exactly the spread data that makes a link interesting.

    So identity moves here and appearances move to ``Occurrence``.

    **The canonical URL string is not stored.** ``fingerprint`` is
    ``sha256(canonical_url(url))`` — the same value ``Link.url_hash``
    already carries — and ``representative_url`` is one raw spelling that
    was actually observed. Storing a canonical *string* as well would
    create a second value that can disagree with the function that
    derives it, and the function is the definition.
    """

    __tablename__ = "resources"
    __table_args__ = (
        # The identity invariant: one fingerprint, one resource, per
        # workspace. This is what makes "the same link from four sources"
        # one row instead of four.
        UniqueConstraint("workspace_id", "fingerprint", name="uq_resource_identity"),
        Index("ix_resources_workspace_platform", "workspace_id", "platform"),
        Index("ix_resources_workspace_last_seen", "workspace_id", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    # sha256 of the canonicalised URL. 64 hex characters, fixed width, so
    # it can carry a btree unique index that raw URL text cannot.
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    # A raw URL exactly as some message wrote it. Never rewritten — the
    # project's first invariant — and never treated as the identity.
    representative_url: Mapped[str] = mapped_column(Text)
    # Destination platform: telegram | whatsapp | ... Derived from the
    # host by app.classifier.platform, which is deterministic and needs no
    # network. Copied from the legacy row at migration time rather than
    # recomputed, so the two cannot silently disagree.
    platform: Mapped[str] = mapped_column(String(20), default="web", server_default="web")
    # Telegram/WhatsApp link type (channel, group, invite, contact, ...).
    # NULL means *not yet resolved*, which is the truth for every migrated
    # row: nothing in the legacy schema recorded it. Filled by the phase
    # that builds link typing, not guessed here.
    link_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Occurrence(Base):
    """One appearance of one resource, in one source, at one time.

    This is the row that can repeat: same resource, different message;
    same message, different extraction path. Its provenance columns are
    the whole point — a resource with no occurrences is a URL with no
    story, and "where did this come from" has to be answerable from the
    row itself rather than by joining back through a guess.

    ``legacy_link_id`` is unique and points at the ``links`` row this was
    migrated from. It is what makes the migration reversible and
    verifiable: every legacy link has exactly one occurrence, and that can
    be asserted rather than assumed.
    """

    __tablename__ = "occurrences"
    __table_args__ = (
        # One occurrence per (resource, source, message, extraction path).
        # Restricted to real Telegram messages: manual and imported rows
        # all carry message id 0, so including them would collapse every
        # hand-added link from one source into a single occurrence.
        Index(
            "uq_occurrence_identity",
            "resource_id",
            "source_id",
            "tg_message_id",
            "extraction_method",
            unique=True,
            sqlite_where=text("tg_message_id > 0"),
            postgresql_where=text("tg_message_id > 0"),
        ),
        # The migration's own guarantee: no legacy link becomes two
        # occurrences, on a re-run or otherwise.
        Index("uq_occurrence_legacy_link", "legacy_link_id", unique=True),
        Index("ix_occurrences_source_observed", "source_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    resource_id: Mapped[int] = mapped_column(ForeignKey("resources.id"), index=True)
    # Not index=True: ``ix_occurrences_source_observed`` below leads with
    # this column, and Postgres uses a composite index for its leading
    # column. A second index here would be paid for on every write and
    # read by nothing.
    source_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
    # The observation this link was extracted from, when one exists.
    # NULL for links older than the ``messages`` table and for every
    # manual or imported row, which have no Telegram message behind them.
    observation_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id"), nullable=True, index=True)
    # Kept alongside observation_id rather than only through it: the
    # legacy rows carry a message id with no ``messages`` row to point at,
    # and losing that number would lose the only link back to Telegram.
    tg_message_id: Mapped[int] = mapped_column(Integer, default=0)
    # How the URL was found: text | hyperlink | button. Same vocabulary as
    # ``Link.source_type``, which is where migrated values come from.
    extraction_method: Mapped[str] = mapped_column(String(20), default="text")
    # When Telegram says the message was posted, when the system saw it.
    # Separate columns because they answer different questions and one is
    # routinely absent.
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Which acquisition path produced this sighting: userbot | public.
    # NULL on migrated rows — the legacy schema recorded it on the source,
    # not on the sighting, and the source's value today is not evidence
    # about a sighting from six months ago.
    acquisition_path: Mapped[str | None] = mapped_column(String(20), nullable=True)
    legacy_link_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --- keeping identity_key filled, without asking anyone to remember ------
#
# Channels are created from six places: manual entry, two importers, the
# demo seeder, dialog discovery and the live listener. Setting the column
# at each of them is six chances to forget, and a forgotten one produces a
# row whose identity is silently NULL — invisible until the day it matters.
#
# A mapper-level hook is the one place all six already go through.


def _fill_identity_key(_mapper, _connection, target: Channel) -> None:
    """Derive identity_key from whatever spelling tg_channel_id holds."""
    target.identity_key = source_identity_key(target.tg_channel_id)


event.listen(Channel, "before_insert", _fill_identity_key)
event.listen(Channel, "before_update", _fill_identity_key)


class JoinRequest(Base):
    """One attempt to get an account access to a private source.

    Separate from ``source_access`` because they answer different
    questions. Access is a *state*: can this account read this source now.
    A join request is a *process*: it is tried, it can be retried, it can
    sit unanswered for days, and it ends in an outcome that then updates
    the access state. Folding the process into the state is how
    ``REQUEST_SENT`` turns into ``ACCESSIBLE`` without anybody verifying
    anything.

    **Nothing here joins anything.** This phase builds the record and its
    rules; performing the access attempt belongs to the runtime, and it is
    gated on authorisation rather than run because a row exists. A request
    is never marked GRANTED by the act of sending it — only by a later
    observation that the account can actually read the source.
    """

    __tablename__ = "join_requests"
    __table_args__ = (
        # One open request per (source, account). Two would mean two
        # attempts racing at Telegram's rate limits on behalf of the same
        # account, which is the thing most likely to get it restricted.
        Index(
            "uq_join_request_open",
            "source_id",
            "account_id",
            unique=True,
            sqlite_where=text("status IN ('READY', 'ATTEMPTING', 'REQUEST_SENT')"),
            postgresql_where=text("status IN ('READY', 'ATTEMPTING', 'REQUEST_SENT')"),
        ),
        Index("ix_join_requests_due", "workspace_id", "status", "next_action_at"),
        CheckConstraint(
            "status IN ('READY', 'ATTEMPTING', 'REQUEST_SENT', 'GRANTED', 'DENIED', "
            "'FAILED', 'MANUAL_INTERVENTION', 'BLOCKED')",
            name="ck_join_request_status",
        ),
    )

    #: Queued, nothing attempted yet.
    READY = "READY"
    #: An attempt is in flight.
    ATTEMPTING = "ATTEMPTING"
    #: A request was sent and is unanswered. Not access.
    REQUEST_SENT = "REQUEST_SENT"
    #: Verified: the account can now read the source.
    GRANTED = "GRANTED"
    #: Answered, and refused.
    DENIED = "DENIED"
    #: The attempt failed for a reason that may not repeat.
    FAILED = "FAILED"
    #: A person has to do something the system will not do on its own.
    MANUAL_INTERVENTION = "MANUAL_INTERVENTION"
    #: Policy or Telegram forbids it; retrying is not the answer.
    BLOCKED = "BLOCKED"

    #: The statuses that occupy the one-open-request slot.
    OPEN_STATUSES = (READY, ATTEMPTING, REQUEST_SENT)
    STATUSES = (READY, ATTEMPTING, REQUEST_SENT, GRANTED, DENIED, FAILED, MANUAL_INTERVENTION, BLOCKED)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("telegram_accounts.id"), index=True)
    status: Mapped[str] = mapped_column(String(24), default=READY, server_default=READY)
    # Operational ordering only. Higher runs first; it says nothing about
    # how good the source is.
    priority: Mapped[int] = mapped_column(Integer, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # When this is worth touching again. NULL means "not on a schedule" —
    # a terminal row, or one waiting on a person.
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # What actually happened, in the words of whatever reported it.
    result: Mapped[str | None] = mapped_column(String(300), nullable=True)
    evidence_id: Mapped[int | None] = mapped_column(ForeignKey("evidence.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --- Phase 3: the collection runtime's own records -------------------------


class SourceProgress(Base):
    """How far collection has got with one source, on one track.

    ``Channel.last_message_id`` is a single number, and a single number
    cannot carry two readers. A source being backfilled from January while
    live updates arrive today has two independent frontiers, and forcing
    them through one scalar produces exactly the corruption the target
    model forbids: the backfill's cursor lands in the live watermark, the
    live reader resumes from January, and every message in between is
    skipped by both — permanently, and invisibly, because no counter
    disagrees.

    So progress is per ``(source, track)``. The live track's watermark is
    mirrored back to ``Channel.last_message_id`` for the readers that have
    not moved yet; the mirror is written only by ``app.progress``.

    Three times, because they answer three questions:

    - ``last_attempt_at``   when did anything last try
    - ``last_progress_at``  when did anything last *succeed* at moving
    - ``current_watermark`` where is it safe to resume from

    An attempt that fails updates the first and neither of the others,
    which is what makes "tried recently and got nowhere" a state you can
    see rather than infer.
    """

    __tablename__ = "source_progress"
    __table_args__ = (
        UniqueConstraint("source_id", "track", name="uq_source_progress_track"),
        Index("ix_source_progress_workspace_track", "workspace_id", "track"),
        CheckConstraint("track IN ('LIVE', 'HISTORICAL')", name="ck_source_progress_track"),
        CheckConstraint(
            "coverage_status IN ('NO_DETECTED_GAP', 'DETECTED_GAP', 'UNKNOWN_COVERAGE')",
            name="ck_source_progress_coverage",
        ),
    )

    #: New messages as they arrive.
    LIVE = "LIVE"
    #: A requested window in the past.
    HISTORICAL = "HISTORICAL"
    TRACKS = (LIVE, HISTORICAL)

    #: Nothing the system can detect is missing. **Not** a claim that
    #: nothing is missing — only that no gap was detected.
    NO_DETECTED_GAP = "NO_DETECTED_GAP"
    #: A specific range is known to have been skipped.
    DETECTED_GAP = "DETECTED_GAP"
    #: The system cannot say either way, which is the honest default.
    UNKNOWN_COVERAGE = "UNKNOWN_COVERAGE"
    COVERAGE_STATES = (NO_DETECTED_GAP, DETECTED_GAP, UNKNOWN_COVERAGE)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    track: Mapped[str] = mapped_column(String(12))
    # The resume point. Monotonic, enforced by a trigger rather than by
    # whoever remembers: a watermark that can go backwards is a watermark
    # that can skip messages nobody will ever read again.
    current_watermark: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_progress_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # The run that last moved this forward, so a watermark can be traced to
    # the work that produced it.
    last_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    coverage_status: Mapped[str] = mapped_column(
        String(20), default=UNKNOWN_COVERAGE, server_default=UNKNOWN_COVERAGE
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CollectionRun(Base):
    """One attempt to collect one source, by one path, over one range.

    Deferred out of phase 1 on the grounds that its columns follow the
    runtime's shape and inventing them early would be guessing. The
    runtime exists now, so this does too.

    A run is the unit that makes collection auditable: it names the
    source, the account, the acquisition path and the range, and it
    records what moved and what failed. ``Assignment`` says who is
    responsible; a run says what responsibility actually produced.

    **A run is not success.** ``COMPLETED`` means the requested scope was
    examined and progress was persisted safely — not that a connection
    opened, not that a worker exited cleanly, and not that zero rows came
    back. A run that found nothing new completes; a run whose connection
    succeeded and whose scope was never read does not.
    """

    __tablename__ = "collection_runs"
    __table_args__ = (
        Index("ix_collection_runs_source_started", "source_id", "started_at"),
        Index("ix_collection_runs_workspace_state", "workspace_id", "state"),
        # Finding runs abandoned by a crashed worker, which is what startup
        # recovery sweeps for.
        Index("ix_collection_runs_live_heartbeat", "state", "heartbeat_at"),
        CheckConstraint(
            "state IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED', 'RECOVERING')",
            name="ck_collection_run_state",
        ),
        CheckConstraint("mode IN ('LIVE', 'HISTORICAL')", name="ck_collection_run_mode"),
    )

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    #: Picked up by startup recovery after a crash; not yet resolved.
    RECOVERING = "RECOVERING"
    STATES = (PENDING, RUNNING, COMPLETED, FAILED, CANCELLED, RECOVERING)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    # NULL for the public path, which needs no account. Not missing data —
    # the absence is the statement.
    account_id: Mapped[int | None] = mapped_column(ForeignKey("telegram_accounts.id"), nullable=True, index=True)
    acquisition_path: Mapped[str] = mapped_column(String(20), default="userbot")
    mode: Mapped[str] = mapped_column(String(12))
    state: Mapped[str] = mapped_column(String(12), default=PENDING, server_default=PENDING)
    # The requested window, for a historical run. NULL on a live run, whose
    # range is "whatever arrived".
    range_from: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    range_to: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    watermark_before: Mapped[int | None] = mapped_column(Integer, nullable=True)
    watermark_after: Mapped[int | None] = mapped_column(Integer, nullable=True)
    messages_seen: Mapped[int] = mapped_column(Integer, default=0)
    links_stored: Mapped[int] = mapped_column(Integer, default=0)
    # One of app.collection.failures.FailureKind. NULL while the run has
    # not failed; never a bare exception class name.
    failure_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Refreshed while the run is alive. A RUNNING row whose heartbeat has
    # stopped is the signature of a worker that died without saying so,
    # and it is what startup recovery looks for.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    evidence_id: Mapped[int | None] = mapped_column(ForeignKey("evidence.id"), nullable=True)
