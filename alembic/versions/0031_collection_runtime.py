"""Progress and runs, with the watermark made un-reversible in the database.

Two tables and two triggers. The tables are ordinary; the triggers are the
point of the migration.

Why monotonicity is a trigger and not a rule in Python
------------------------------------------------------
``app/progress.py`` already refuses a watermark below the current one. That
guard protects the one path that goes through it. It cannot protect against
a script, a psql session, a future call site, or a bug in a later version of
the very module that holds the rule — and the cost of any of those is not a
crash. A watermark that moves backwards is re-read data (harmless). A
watermark that moves *forwards* past messages nobody read is a permanent,
silent gap: every future run starts above them, no counter disagrees, and
nothing in the product can distinguish it from a quiet channel.

So the database enforces it. ``source_progress.current_watermark`` may rise
or stay; an UPDATE that lowers it is refused with a message naming the
module that is allowed to move it.

The same trigger goes on ``channels.last_message_id``, which is now a mirror
of the live track in exactly the way ``channels.account_id`` is a mirror of
``source_assignments``. The two existing writers of that column
(``scripts/collect.py`` and ``app/publicsource.py``) are both repointed at
``app.progress`` by this phase; the trigger is what makes a third one
impossible rather than merely discouraged.

Why the mirror is not guarded by a session flag
-----------------------------------------------
0029 guarded ``channels.account_id`` with ``app.assignment_write`` because
an assignment has no ordering — any value can legitimately follow any other,
so "who wrote this" is the only question a trigger can ask. A watermark does
have an ordering, so the stronger and simpler question is available: is the
new value at least the old one? That holds for every legitimate writer and
fails for every corruption this phase is trying to prevent, without any
caller having to announce itself.

The RLS trap, for the fifth migration running
---------------------------------------------
``channels`` carries FORCE row-level security and this connection has no
tenant, so the backfill below reads zero rows and writes nothing while
reporting success. FORCE is lifted for the backfill and restored — and the
restoration is *asserted*, because a migration that quietly left it off
would expose every tenant's rows to every other.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0031_collection_runtime"
down_revision: str | None = "0030_account_access_join_queue"
branch_labels: str | None = None
depends_on: str | None = None

POLICY = "workspace_isolation"
SETTING = "app.workspace_id"

NEW_TENANT_TABLES = ("source_progress", "collection_runs")

# Read from, and written to, by the backfill below. FORCE applies to this
# connection too, and this connection carries no tenant.
FORCED_TABLES = ("channels",)

TRACKS = ("LIVE", "HISTORICAL")
COVERAGE_STATES = ("NO_DETECTED_GAP", "DETECTED_GAP", "UNKNOWN_COVERAGE")
RUN_STATES = ("PENDING", "RUNNING", "COMPLETED", "FAILED", "CANCELLED", "RECOVERING")
RUN_MODES = ("LIVE", "HISTORICAL")

WATERMARK_FUNCTION = "source_progress_monotonic_guard"
WATERMARK_TRIGGER = "trg_source_progress_monotonic"
MIRROR_FUNCTION = "channels_watermark_monotonic_guard"
MIRROR_TRIGGER = "trg_channels_watermark_monotonic"


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _assert_readable(bind, is_postgres: bool, tables: tuple[str, ...]) -> None:
    """Refuse to backfill through a table this connection cannot see.

    FORCE row-level security applies to the table owner too, and a
    migration carries no tenant — so a read comes back empty and a write
    is refused, both without an error. Everything downstream then agrees
    with itself about nothing.
    """
    if not is_postgres:
        return
    for table in tables:
        forced = bind.execute(
            sa.text("SELECT relforcerowsecurity FROM pg_class WHERE relname = :t"), {"t": table}
        ).scalar()
        if forced:
            raise RuntimeError(
                f"0031 was about to read {table} with FORCE ROW LEVEL SECURITY still on. "
                "This connection has no tenant, so every row would be invisible and the "
                "backfill would report success having written nothing."
            )


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # --- 1. tables --------------------------------------------------------
    op.create_table(
        "source_progress",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("channels.id"), nullable=False),
        sa.Column("track", sa.String(12), nullable=False),
        sa.Column("current_watermark", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_progress_at", sa.DateTime(), nullable=True),
        sa.Column("last_run_id", sa.Integer(), nullable=True),
        sa.Column("coverage_status", sa.String(20), nullable=False, server_default="UNKNOWN_COVERAGE"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("source_id", "track", name="uq_source_progress_track"),
        sa.CheckConstraint(f"track IN ({_quoted(TRACKS)})", name="ck_source_progress_track"),
        sa.CheckConstraint(f"coverage_status IN ({_quoted(COVERAGE_STATES)})", name="ck_source_progress_coverage"),
    )
    op.create_index("ix_source_progress_workspace_id", "source_progress", ["workspace_id"])
    op.create_index("ix_source_progress_source_id", "source_progress", ["source_id"])
    op.create_index("ix_source_progress_workspace_track", "source_progress", ["workspace_id", "track"])

    op.create_table(
        "collection_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("channels.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("telegram_accounts.id"), nullable=True),
        sa.Column("acquisition_path", sa.String(20), nullable=False, server_default="userbot"),
        sa.Column("mode", sa.String(12), nullable=False),
        sa.Column("state", sa.String(12), nullable=False, server_default="PENDING"),
        sa.Column("range_from", sa.DateTime(), nullable=True),
        sa.Column("range_to", sa.DateTime(), nullable=True),
        sa.Column("watermark_before", sa.Integer(), nullable=True),
        sa.Column("watermark_after", sa.Integer(), nullable=True),
        sa.Column("messages_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("links_stored", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_kind", sa.String(32), nullable=True),
        sa.Column("detail", sa.String(500), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("evidence_id", sa.Integer(), sa.ForeignKey("evidence.id"), nullable=True),
        sa.CheckConstraint(f"state IN ({_quoted(RUN_STATES)})", name="ck_collection_run_state"),
        sa.CheckConstraint(f"mode IN ({_quoted(RUN_MODES)})", name="ck_collection_run_mode"),
    )
    op.create_index("ix_collection_runs_workspace_id", "collection_runs", ["workspace_id"])
    op.create_index("ix_collection_runs_source_id", "collection_runs", ["source_id"])
    op.create_index("ix_collection_runs_account_id", "collection_runs", ["account_id"])
    op.create_index("ix_collection_runs_source_started", "collection_runs", ["source_id", "started_at"])
    op.create_index("ix_collection_runs_workspace_state", "collection_runs", ["workspace_id", "state"])
    # Finding runs abandoned by a crashed worker, which is the one query
    # startup recovery runs and the one it must not table-scan for.
    op.create_index("ix_collection_runs_live_heartbeat", "collection_runs", ["state", "heartbeat_at"])

    # --- 2. backfill the live track from the legacy watermark -------------
    # Started at ``channels.last_message_id`` rather than at zero. Zero
    # would re-read every archive from the beginning and store nothing (the
    # messages are already there) while burning the whole rate-limit budget
    # rediscovering it. The legacy column is where the old collector
    # actually got to, and it is the only honest starting point.
    #
    # ``coverage_status`` is UNKNOWN_COVERAGE for every backfilled row and
    # stays that way. Nothing in the old schema recorded whether a range was
    # examined, so claiming NO_DETECTED_GAP here would be inventing a
    # measurement that was never taken.
    if is_postgres:
        for table in FORCED_TABLES:
            op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")

    # Asserted, not assumed. The count comparison below cannot detect the
    # RLS trap on its own: with FORCE still on, the INSERT..SELECT reads
    # zero channels *and* the verification count reads zero channels, so
    # the two agree and the migration reports a clean backfill of nothing.
    # This was demonstrated by deliberately removing the NO FORCE loop
    # above and watching the migration pass. The only check that survives
    # that sabotage is one on the precondition itself.
    _assert_readable(bind, is_postgres, FORCED_TABLES)

    inserted = bind.execute(
        sa.text(
            "INSERT INTO source_progress "
            "(workspace_id, source_id, track, current_watermark, last_attempt_at, "
            " last_progress_at, coverage_status, created_at, updated_at) "
            "SELECT c.workspace_id, c.id, 'LIVE', COALESCE(c.last_message_id, 0), "
            "       c.last_collected_at, NULL, 'UNKNOWN_COVERAGE', "
            "       CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
            "FROM channels c "
            "WHERE NOT EXISTS (SELECT 1 FROM source_progress p "
            "                  WHERE p.source_id = c.id AND p.track = 'LIVE')"
        )
    ).rowcount
    channels = bind.execute(sa.text("SELECT count(*) FROM channels")).scalar() or 0

    # A backfill that reports success having written nothing is the failure
    # this project has now hit three times, always because of RLS. Asserted
    # rather than logged: the migration is the only moment the discrepancy
    # is cheap to notice.
    if inserted is not None and inserted >= 0 and inserted != channels:
        raise RuntimeError(
            f"0031 backfilled {inserted} progress row(s) for {channels} channel(s). "
            "Every channel must get a LIVE progress row; a shortfall means the "
            "backfill could not see or could not write the rows it claimed to."
        )

    if is_postgres:
        for table in FORCED_TABLES:
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            forced = bind.execute(
                sa.text("SELECT relforcerowsecurity FROM pg_class WHERE relname = :t"), {"t": table}
            ).scalar()
            if not forced:
                raise RuntimeError(
                    f"0031 left {table} without FORCE ROW LEVEL SECURITY — refusing to finish a "
                    "migration that would leave the table readable across tenants"
                )

    if not is_postgres:
        return

    # --- 3. monotonicity, in the database ---------------------------------
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {WATERMARK_FUNCTION}() RETURNS trigger AS $$
        BEGIN
            IF NEW.current_watermark < OLD.current_watermark THEN
                RAISE EXCEPTION
                    'source_progress.current_watermark may not move backwards '
                    '(% -> % on source %); a watermark records how far stored data '
                    'reaches, and lowering it re-reads at best and hides a gap at worst. '
                    'Use app.progress.advance().',
                    OLD.current_watermark, NEW.current_watermark, NEW.source_id
                    USING ERRCODE = 'raise_exception';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        f"CREATE TRIGGER {WATERMARK_TRIGGER} "
        f"BEFORE UPDATE OF current_watermark ON source_progress "
        f"FOR EACH ROW EXECUTE FUNCTION {WATERMARK_FUNCTION}()"
    )

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {MIRROR_FUNCTION}() RETURNS trigger AS $$
        BEGIN
            IF NEW.last_message_id < OLD.last_message_id THEN
                RAISE EXCEPTION
                    'channels.last_message_id is a mirror of the LIVE source_progress '
                    'track and may not move backwards (% -> % on channel %). '
                    'Use app.progress.advance().',
                    OLD.last_message_id, NEW.last_message_id, NEW.id
                    USING ERRCODE = 'raise_exception';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        f"CREATE TRIGGER {MIRROR_TRIGGER} "
        f"BEFORE UPDATE OF last_message_id ON channels "
        f"FOR EACH ROW EXECUTE FUNCTION {MIRROR_FUNCTION}()"
    )

    # --- 4. tenant isolation, after the backfill has written --------------
    # Last, for the reason 0028 records: FORCE applies to the migration's
    # own connection, which carries no tenant.
    for table in NEW_TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {POLICY} ON {table} "
            f"USING (workspace_id = NULLIF(current_setting('{SETTING}', true), '')::int)"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(f"DROP TRIGGER IF EXISTS {MIRROR_TRIGGER} ON channels")
        op.execute(f"DROP FUNCTION IF EXISTS {MIRROR_FUNCTION}()")
        op.execute(f"DROP TRIGGER IF EXISTS {WATERMARK_TRIGGER} ON source_progress")
        op.execute(f"DROP FUNCTION IF EXISTS {WATERMARK_FUNCTION}()")
        for table in NEW_TENANT_TABLES:
            op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {table}")
            op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_table("collection_runs")
    op.drop_table("source_progress")
