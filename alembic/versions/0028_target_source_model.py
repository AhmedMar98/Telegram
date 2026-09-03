"""Target source model: identity, access, assignment, resource, occurrence.

Phase 1 of the Telegram Intelligence Platform migration. Everything here
is **additive**: six new tables, four new columns, and a backfill that
derives the new shape from rows that already exist. No column changes
meaning, no table is renamed, no reader is repointed — so the running
system behaves exactly as it did, while the target shape becomes real and
verifiable underneath it.

What the backfill can and cannot honestly produce
-------------------------------------------------
``resources`` and ``occurrences`` are a faithful split of ``links``: the
legacy uniqueness key is ``(channel_id, url_hash)``, so every link row is
already exactly one appearance of one URL. Grouping by ``url_hash`` gives
the resource; the rows themselves give the occurrences. Nothing is
invented and nothing is dropped — ``occurrences.legacy_link_id`` points at
the row each one came from, which is what makes the result checkable
rather than merely plausible.

``source_access`` is backfilled **only where evidence exists**. A source
that has actually been collected by an account proves that account could
read it at that moment, and that is recorded with the moment attached. A
source nobody has collected gets no row at all: absence means "never
evaluated", which is true, whereas a row saying UNKNOWN would be a
measurement nobody took.

``source_events`` is created empty on purpose. Every existing row would
need a previous state, a transition time and a reason that the current
schema never recorded; a backfill could only fabricate all three.

Reversibility
-------------
``downgrade`` drops what ``upgrade`` created and nothing else. Because the
legacy tables were never modified, a downgrade loses only data written
through the new model after this migration ran — not a single pre-existing
row. That property is why the split is additive rather than in-place.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0028_target_source_model"
down_revision: str | None = "0027_coverage_snapshots"
branch_labels: str | None = None
depends_on: str | None = None


# Mirrors app/rls.py's policy, copied rather than imported: a migration is
# a snapshot of one moment and must keep working after the application's
# constants move on.
POLICY = "workspace_isolation"
SETTING = "app.workspace_id"

NEW_TENANT_TABLES = (
    "evidence",
    "resources",
    "occurrences",
    "source_access",
    "source_assignments",
    "source_events",
)


def _canonical_id(raw: str | None) -> str | None:
    """Local copy of ``app.identity.canonical_id``.

    Copied deliberately. A migration that imports application code changes
    behaviour when that code changes, which makes a run today and a run
    next year produce different databases from the same revision.
    ``tests/test_target_source_model.py`` asserts this copy and the
    application's rule still agree.
    """
    try:
        text = str(int(str(raw).strip()))
    except (TypeError, ValueError):
        return None
    if text.startswith("-100"):
        return text[4:]
    if text.startswith("-"):
        return text[1:]
    return text


def _identity_key(tg_channel_id: str | None) -> str | None:
    if tg_channel_id is None:
        return None
    raw = tg_channel_id.strip()
    if not raw:
        return None
    return _canonical_id(raw) or raw


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # --- 1. columns on the tables that already stand for these entities ---
    op.add_column("channels", sa.Column("identity_key", sa.String(64), nullable=True))
    op.add_column("channels", sa.Column("acquisition_method", sa.String(30), nullable=True))
    op.create_index("ix_channels_identity_key", "channels", ["workspace_id", "identity_key"])

    op.add_column("messages", sa.Column("processed_at", sa.DateTime(), nullable=True))
    op.add_column("messages", sa.Column("acquisition_path", sa.String(20), nullable=True))

    # --- 2. the new tables ------------------------------------------------
    op.create_table(
        "evidence",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=True),
        sa.Column("summary", sa.String(300), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_evidence_workspace_id", "evidence", ["workspace_id"])
    op.create_index("ix_evidence_workspace_kind", "evidence", ["workspace_id", "kind"])

    op.create_table(
        "resources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("representative_url", sa.Text(), nullable=False),
        sa.Column("platform", sa.String(20), nullable=False, server_default="web"),
        sa.Column("link_type", sa.String(30), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", "fingerprint", name="uq_resource_identity"),
    )
    op.create_index("ix_resources_workspace_id", "resources", ["workspace_id"])
    op.create_index("ix_resources_fingerprint", "resources", ["fingerprint"])
    op.create_index("ix_resources_workspace_platform", "resources", ["workspace_id", "platform"])
    op.create_index("ix_resources_workspace_last_seen", "resources", ["workspace_id", "last_seen_at"])

    op.create_table(
        "occurrences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("resource_id", sa.Integer(), sa.ForeignKey("resources.id"), nullable=False),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("channels.id"), nullable=False),
        sa.Column("observation_id", sa.Integer(), sa.ForeignKey("messages.id"), nullable=True),
        sa.Column("tg_message_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("extraction_method", sa.String(20), nullable=False, server_default="text"),
        sa.Column("posted_at", sa.DateTime(), nullable=True),
        sa.Column("observed_at", sa.DateTime(), nullable=True),
        sa.Column("acquisition_path", sa.String(20), nullable=True),
        sa.Column("legacy_link_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_occurrences_workspace_id", "occurrences", ["workspace_id"])
    op.create_index("ix_occurrences_resource_id", "occurrences", ["resource_id"])
    op.create_index("ix_occurrences_source_observed", "occurrences", ["source_id", "observed_at"])
    op.create_index("ix_occurrences_observation_id", "occurrences", ["observation_id"])
    op.create_index("uq_occurrence_legacy_link", "occurrences", ["legacy_link_id"], unique=True)
    # Partial: manual and imported rows all carry message id 0, so an
    # unrestricted index would collapse every hand-added link from one
    # source into a single occurrence.
    op.create_index(
        "uq_occurrence_identity",
        "occurrences",
        ["resource_id", "source_id", "tg_message_id", "extraction_method"],
        unique=True,
        sqlite_where=sa.text("tg_message_id > 0"),
        postgresql_where=sa.text("tg_message_id > 0"),
    )

    op.create_table(
        "source_access",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("channels.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("telegram_accounts.id"), nullable=True),
        sa.Column("path_kind", sa.String(20), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=True),
        sa.Column("evidence_id", sa.Integer(), sa.ForeignKey("evidence.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_source_access_workspace_id", "source_access", ["workspace_id"])
    op.create_index("ix_source_access_source_id", "source_access", ["source_id"])
    op.create_index("ix_source_access_account_id", "source_access", ["account_id"])
    op.create_index("ix_source_access_workspace_state", "source_access", ["workspace_id", "state"])
    # COALESCE rather than a plain unique constraint: the public path has
    # no account, and NULLs do not compare equal, so a bare constraint
    # would let one public path be recorded any number of times.
    op.create_index(
        "uq_source_access_path",
        "source_access",
        ["source_id", "path_kind", sa.text("COALESCE(account_id, -1)")],
        unique=True,
    )

    op.create_table(
        "source_assignments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("channels.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("telegram_accounts.id"), nullable=False),
        sa.Column("assigned_at", sa.DateTime(), nullable=True),
        sa.Column("released_at", sa.DateTime(), nullable=True),
        sa.Column("reason", sa.String(200), nullable=True),
        sa.Column("evidence_id", sa.Integer(), sa.ForeignKey("evidence.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_source_assignments_workspace_id", "source_assignments", ["workspace_id"])
    op.create_index("ix_source_assignments_source_id", "source_assignments", ["source_id"])
    op.create_index("ix_source_assignments_account_id", "source_assignments", ["account_id"])
    op.create_index("ix_source_assignments_account", "source_assignments", ["account_id", "released_at"])
    # The Primary Collector invariant, enforced by the database rather than
    # by whoever remembers it: at most one open assignment per source.
    op.create_index(
        "uq_source_assignment_open",
        "source_assignments",
        ["source_id"],
        unique=True,
        sqlite_where=sa.text("released_at IS NULL"),
        postgresql_where=sa.text("released_at IS NULL"),
    )

    op.create_table(
        "source_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("channels.id"), nullable=False),
        sa.Column("dimension", sa.String(40), nullable=False),
        sa.Column("previous_state", sa.String(40), nullable=True),
        sa.Column("new_state", sa.String(40), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("reason", sa.String(200), nullable=True),
        sa.Column("actor", sa.String(100), nullable=True),
        sa.Column("evidence_id", sa.Integer(), sa.ForeignKey("evidence.id"), nullable=True),
    )
    op.create_index("ix_source_events_workspace_id", "source_events", ["workspace_id"])
    op.create_index("ix_source_events_source_id", "source_events", ["source_id"])
    op.create_index("ix_source_events_source_time", "source_events", ["source_id", "occurred_at"])

    # --- 3. backfill: sources --------------------------------------------
    # ``channels`` has forced row-level security from migration 0020, and
    # this connection carries no tenant — so every read below would return
    # zero rows and every UPDATE would match nothing, while the migration
    # reported success. That is not hypothetical: it is what this migration
    # did on the first run against real Postgres, and what
    # tests/test_target_source_model.py caught.
    #
    # A migration is a schema operation rather than a request, so lifting
    # FORCE for the length of the backfill is the right instrument. Same
    # reasoning, and the same assert-it-came-back check, as 0025.
    if is_postgres:
        op.execute("ALTER TABLE channels NO FORCE ROW LEVEL SECURITY")

    # Row by row rather than in SQL, because the identity rule is a Python
    # function with a documented edge case (see _canonical_id) and
    # re-expressing it as a CASE would be re-implementing it. Channels
    # number in the hundreds; links number in the millions, and those are
    # done set-based below.
    channels = bind.execute(
        sa.text("SELECT id, workspace_id, tg_channel_id, source, account_id, last_collected_at FROM channels")
    ).fetchall()

    for row in channels:
        key = _identity_key(row.tg_channel_id)
        synthetic = row.tg_channel_id == "manual" or str(row.tg_channel_id).startswith("import:")
        # "manual" is not acquired from anywhere; leaving it NULL says so.
        method = None
        if not synthetic:
            method = "PUBLIC_ACQUISITION" if row.source == "public" else "AUTHORIZED_ACCOUNT"
        bind.execute(
            sa.text("UPDATE channels SET identity_key = :k, acquisition_method = :m WHERE id = :i"),
            {"k": key, "m": method, "i": row.id},
        )

    # --- 4. backfill: access, where evidence actually exists --------------
    for row in channels:
        synthetic = row.tg_channel_id == "manual" or str(row.tg_channel_id).startswith("import:")
        if synthetic or row.last_collected_at is None:
            # Never collected, or not a Telegram dialog at all: nothing was
            # observed, so nothing is recorded. Absence is the statement.
            continue
        public = row.source == "public"
        if not public and row.account_id is None:
            # Collected at some point by an account we can no longer name.
            # A row would have to invent which one.
            continue
        # RETURNING works on both backends here (SQLite >= 3.35), which
        # keeps one code path instead of a dialect branch that only one
        # side of CI would ever exercise.
        new_id = bind.execute(
            sa.text(
                "INSERT INTO evidence (workspace_id, kind, observed_at, summary, detail, created_at) "
                "VALUES (:w, 'collection_history', :o, :s, :d, CURRENT_TIMESTAMP) RETURNING id"
            ),
            {
                "w": row.workspace_id,
                "o": row.last_collected_at,
                "s": "access inferred from a completed collection run",
                "d": "backfilled by migration 0028 from channels.last_collected_at",
            },
        ).scalar()
        bind.execute(
            sa.text(
                "INSERT INTO source_access "
                "(workspace_id, source_id, account_id, path_kind, state, observed_at, evidence_id, "
                " created_at, updated_at) "
                "VALUES (:w, :s, :a, :p, 'ACCESSIBLE', :o, :e, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {
                "w": row.workspace_id,
                "s": row.id,
                "a": None if public else row.account_id,
                "p": "public" if public else "userbot",
                "o": row.last_collected_at,
                "e": new_id,
            },
        )

    # --- 5. backfill: assignments from the scalar column ------------------
    # assigned_at stays NULL: channels.account_id never recorded when it
    # was set, and a timestamp invented here would be indistinguishable
    # from one that was measured.
    bind.execute(
        sa.text(
            "INSERT INTO source_assignments "
            "(workspace_id, source_id, account_id, assigned_at, released_at, reason, created_at) "
            "SELECT c.workspace_id, c.id, c.account_id, NULL, NULL, "
            "'migrated from channels.account_id', CURRENT_TIMESTAMP "
            "FROM channels c WHERE c.account_id IS NOT NULL"
        )
    )

    if is_postgres:
        op.execute("ALTER TABLE channels FORCE ROW LEVEL SECURITY")
        forced = bind.execute(
            sa.text("SELECT relforcerowsecurity FROM pg_class WHERE relname = 'channels'")
        ).scalar()
        if not forced:
            raise RuntimeError(
                "0028 left channels without FORCE ROW LEVEL SECURITY — refusing to finish a "
                "migration that would leave the table readable across tenants"
            )

    # --- 6. backfill: resources and occurrences ---------------------------
    # One resource per (workspace, fingerprint). representative_url and
    # platform come from the earliest row of the group rather than an
    # aggregate: MIN(url) would return a lexicographic winner that no
    # message ever wrote, and platform has to stay the value the classifier
    # actually produced for a real URL.
    #
    # ``DISTINCT ON`` is PostgreSQL-only, and that is deliberate rather than
    # careless: this migration chain already cannot run on SQLite — it uses
    # ALTER forms SQLite has no support for, which is why the test suite
    # builds its schema with ``create_all`` instead — so portability here
    # would buy nothing and cost a great deal.
    #
    # **Measured.** On 600k links across 1,000 sources with 200k distinct
    # URLs, the obvious form — a correlated ``ORDER BY … LIMIT 1`` lookup
    # per group, twice — ran for over ten minutes without finishing. Render
    # runs ``alembic upgrade head`` ahead of uvicorn in its start command,
    # so that version would have held a deploy open past its health check.
    # This one is a single pass over ``links``.
    bind.execute(
        sa.text(
            "INSERT INTO resources "
            "(workspace_id, fingerprint, representative_url, platform, first_seen_at, last_seen_at, created_at) "
            "SELECT DISTINCT ON (workspace_id, url_hash) "
            "  workspace_id, url_hash, url, platform, "
            "  MIN(COALESCE(posted_at, created_at)) OVER (PARTITION BY workspace_id, url_hash), "
            "  MAX(COALESCE(posted_at, created_at)) OVER (PARTITION BY workspace_id, url_hash), "
            "  CURRENT_TIMESTAMP "
            "FROM links "
            "ORDER BY workspace_id, url_hash, created_at, id"
        )
    )
    bind.execute(
        sa.text(
            "INSERT INTO occurrences "
            "(workspace_id, resource_id, source_id, observation_id, tg_message_id, extraction_method, "
            " posted_at, observed_at, acquisition_path, legacy_link_id, created_at) "
            "SELECT l.workspace_id, r.id, l.channel_id, l.message_ref_id, l.message_id, "
            "  COALESCE(l.source_type, 'text'), l.posted_at, l.created_at, NULL, l.id, CURRENT_TIMESTAMP "
            "FROM links l "
            "JOIN resources r ON r.workspace_id = l.workspace_id AND r.fingerprint = l.url_hash"
        )
    )

    # --- 7. tenant isolation, after the backfill has written --------------
    # Enabled last on purpose: FORCE applies to the migration's own
    # connection too, and this connection carries no tenant.
    if is_postgres:
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
        for table in NEW_TENANT_TABLES:
            op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {table}")
            op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_table("source_events")
    op.drop_table("source_assignments")
    op.drop_table("source_access")
    op.drop_table("occurrences")
    op.drop_table("resources")
    op.drop_table("evidence")

    op.drop_column("messages", "acquisition_path")
    op.drop_column("messages", "processed_at")
    op.drop_index("ix_channels_identity_key", table_name="channels")
    op.drop_column("channels", "acquisition_method")
    op.drop_column("channels", "identity_key")
