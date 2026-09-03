"""Account state, access states, and the queue for private sources.

Phase 2's schema. Three things, and one rename:

**Account state.** ``is_active`` answers "is this account collected from"
and nothing else — it cannot say whether an account is switched off by a
person, disabled by repeated failure, waiting on re-authorisation or
being throttled by Telegram, and those need four different responses. The
new ``state`` column says which, and a CHECK constraint binds the two so
they cannot disagree: exactly ACTIVE means active. There is no service to
call and no trigger to satisfy — the database refuses a write that sets
one without the other.

**Account identity.** ``tg_user_id`` is the stable one. A label is a
nickname, a phone number can move, and a session string is replaced on
every re-authorisation — keying on any of them would turn re-authorising
an account into creating a second one, holding none of the first's
assignments or history. Nullable because nothing observed it before, and
UNKNOWN is the honest value for a row that predates the column.

**Access states.** Phase 1 shipped three; the model needs seven. The two
additions that matter are ``UNKNOWN`` (nobody has looked — not the same as
a failed check) and ``REQUEST_SENT`` (somebody asked — not the same as an
answer). A system that collapses either pair reports coverage it does not
have.

**The queue.** ``join_requests`` records the process of getting an account
access to a private source: what was tried, what came back, when it is
worth trying again. It records; it does not join. Performing an access
attempt is gated on authorisation and belongs to the runtime.

The rename is ``acquisition_method``: 0026 wrote PUBLIC_ACQUISITION and
AUTHORIZED_ACCOUNT, the target vocabulary is PUBLIC and AUTHORIZED_USER.
Two names for one concept is the thing worth removing, so the values are
normalised rather than mapped in code forever.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0028_account_access_join_queue"
down_revision: str | None = "0027_assignment_single_authority"
branch_labels: str | None = None
depends_on: str | None = None

POLICY = "workspace_isolation"
SETTING = "app.workspace_id"

# Written by this migration and carrying FORCE row-level security, which a
# migration's tenant-less connection cannot write through. Third time in
# four migrations; see 0027 for the full reasoning.
FORCED_TABLES = ("channels",)

ACCESS_STATES = (
    "UNKNOWN",
    "ACCESSIBLE",
    "INACCESSIBLE",
    "NEEDS_ACCESS",
    "REQUEST_SENT",
    "ACCESS_DENIED",
    "BLOCKED",
)
JOIN_STATUSES = (
    "READY",
    "ATTEMPTING",
    "REQUEST_SENT",
    "GRANTED",
    "DENIED",
    "FAILED",
    "MANUAL_INTERVENTION",
    "BLOCKED",
)


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # --- accounts ---------------------------------------------------------
    op.add_column("telegram_accounts", sa.Column("tg_user_id", sa.String(64), nullable=True))
    op.add_column("telegram_accounts", sa.Column("state", sa.String(20), nullable=False, server_default="ACTIVE"))
    op.add_column("telegram_accounts", sa.Column("state_reason", sa.String(300), nullable=True))
    op.add_column("telegram_accounts", sa.Column("state_changed_at", sa.DateTime(), nullable=True))
    op.create_index("ix_telegram_accounts_tg_user_id", "telegram_accounts", ["tg_user_id"])
    op.create_unique_constraint("uq_account_identity", "telegram_accounts", ["workspace_id", "tg_user_id"])

    # Derived, not invented. An inactive account with a disabled_reason was
    # switched off by the failure counter; one without was switched off by a
    # person. Nothing is backfilled into AUTH_REQUIRED, RATE_LIMITED or the
    # rest: no column ever recorded them, and a guess would be
    # indistinguishable from a measurement.
    bind.execute(
        sa.text(
            "UPDATE telegram_accounts SET state = CASE "
            "  WHEN is_active THEN 'ACTIVE' "
            "  WHEN disabled_reason IS NOT NULL THEN 'DISABLED' "
            "  ELSE 'INACTIVE' END, "
            "state_reason = CASE WHEN is_active THEN NULL ELSE disabled_reason END"
        )
    )
    # After the backfill: a CHECK added first would reject every existing
    # inactive row, all of which arrive here with state still at its default.
    op.create_check_constraint(
        "ck_account_state_matches_is_active", "telegram_accounts", "(state = 'ACTIVE') = is_active"
    )

    # --- access states ----------------------------------------------------
    op.create_check_constraint("ck_source_access_state", "source_access", f"state IN ({_quoted(ACCESS_STATES)})")

    # --- the queue --------------------------------------------------------
    op.create_table(
        "join_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("channels.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("telegram_accounts.id"), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="READY"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("next_action_at", sa.DateTime(), nullable=True),
        sa.Column("result", sa.String(300), nullable=True),
        sa.Column("evidence_id", sa.Integer(), sa.ForeignKey("evidence.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(f"status IN ({_quoted(JOIN_STATUSES)})", name="ck_join_request_status"),
    )
    op.create_index("ix_join_requests_workspace_id", "join_requests", ["workspace_id"])
    op.create_index("ix_join_requests_source_id", "join_requests", ["source_id"])
    op.create_index("ix_join_requests_account_id", "join_requests", ["account_id"])
    op.create_index("ix_join_requests_due", "join_requests", ["workspace_id", "status", "next_action_at"])
    # One open request per (source, account): two would race at Telegram's
    # rate limits on behalf of one account, which is the fastest way to lose
    # the account the request was meant to use.
    op.create_index(
        "uq_join_request_open",
        "join_requests",
        ["source_id", "account_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('READY', 'ATTEMPTING', 'REQUEST_SENT')"),
        postgresql_where=sa.text("status IN ('READY', 'ATTEMPTING', 'REQUEST_SENT')"),
    )

    # --- one vocabulary for acquisition ----------------------------------
    if is_postgres:
        for table in FORCED_TABLES:
            op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")

    bind.execute(
        sa.text(
            "UPDATE channels SET acquisition_method = CASE acquisition_method "
            "  WHEN 'PUBLIC_ACQUISITION' THEN 'PUBLIC' "
            "  WHEN 'AUTHORIZED_ACCOUNT' THEN 'AUTHORIZED_USER' "
            "  ELSE acquisition_method END "
            "WHERE acquisition_method IN ('PUBLIC_ACQUISITION', 'AUTHORIZED_ACCOUNT')"
        )
    )

    if is_postgres:
        for table in FORCED_TABLES:
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            forced = bind.execute(
                sa.text("SELECT relforcerowsecurity FROM pg_class WHERE relname = :t"), {"t": table}
            ).scalar()
            if not forced:
                raise RuntimeError(
                    f"0028 left {table} without FORCE ROW LEVEL SECURITY — refusing to finish a "
                    "migration that would leave the table readable across tenants"
                )

        op.execute("ALTER TABLE join_requests ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE join_requests FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {POLICY} ON join_requests "
            f"USING (workspace_id = NULLIF(current_setting('{SETTING}', true), '')::int)"
        )


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    if is_postgres:
        op.execute(f"DROP POLICY IF EXISTS {POLICY} ON join_requests")
        op.execute("ALTER TABLE join_requests NO FORCE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE join_requests DISABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE channels NO FORCE ROW LEVEL SECURITY")

    bind.execute(
        sa.text(
            "UPDATE channels SET acquisition_method = CASE acquisition_method "
            "  WHEN 'PUBLIC' THEN 'PUBLIC_ACQUISITION' "
            "  WHEN 'AUTHORIZED_USER' THEN 'AUTHORIZED_ACCOUNT' "
            "  ELSE acquisition_method END "
            "WHERE acquisition_method IN ('PUBLIC', 'AUTHORIZED_USER')"
        )
    )
    if is_postgres:
        op.execute("ALTER TABLE channels FORCE ROW LEVEL SECURITY")

    op.drop_table("join_requests")
    op.drop_constraint("ck_source_access_state", "source_access", type_="check")
    op.drop_constraint("ck_account_state_matches_is_active", "telegram_accounts", type_="check")
    op.drop_constraint("uq_account_identity", "telegram_accounts", type_="unique")
    op.drop_index("ix_telegram_accounts_tg_user_id", table_name="telegram_accounts")
    op.drop_column("telegram_accounts", "state_changed_at")
    op.drop_column("telegram_accounts", "state_reason")
    op.drop_column("telegram_accounts", "state")
    op.drop_column("telegram_accounts", "tg_user_id")
