"""One authority for who collects a source.

``source_assignments`` is the truth; ``Channel.account_id`` is a mirror of
it kept for the collector and the dashboard, which still read that column.
Two writable copies of one fact is the problem — this migration makes the
second copy writable by exactly one path.

Why a session flag and not a subquery
-------------------------------------
The obvious trigger compares ``NEW.account_id`` against the open row in
``source_assignments``. It cannot work here, and the reason is worth
writing down because it is the same trap migration 0028 fell into:
``source_assignments`` has FORCE row-level security, a trigger function
runs as the invoking user, and a session with no tenant set reads **zero
rows** from it. The comparison would then conclude "no open assignment"
and reject every legitimate write — or, with the comparison inverted,
accept every illegitimate one. A guard whose correctness depends on
whether the caller happens to have set a GUC is not a guard.

So the trigger asks a question that has nothing to do with row visibility:
*did the assignment service make this write?* The service sets
``app.assignment_write`` for the length of its transaction, and nothing
else does. Writes from a forgotten call site, a script, or a hand-typed
UPDATE are refused with a message naming the module to use.

The value-level property — mirror equals open assignment — follows from
the service writing both in one transaction, and is asserted directly by
``app.assignments.mirror_disagreements`` so it is checked rather than
assumed.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0029_assignment_single_authority"
down_revision: str | None = "0028_target_source_model"
branch_labels: str | None = None
depends_on: str | None = None

GUARD_FUNCTION = "channels_assignment_mirror_guard"
GUARD_TRIGGER = "trg_channels_assignment_mirror_guard"

# Both tables this migration writes carry FORCE row-level security, and a
# migration carries no tenant — so every write is refused and every read
# comes back empty. This is the third migration in a row to meet it (0025
# on notification_preferences, 0028 on channels, this one on both plus
# source_assignments), which is why it is a named list rather than a line
# of prose each time.
FORCED_TABLES = ("channels", "source_assignments")


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # --- 1. make the two copies agree before locking the door ------------
    # After 0028 they already do. This exists for the window between the
    # two migrations, and for any row written by a path that predates the
    # service. The authoritative direction is used in both cases: a mirror
    # with no assignment gets one (the rule 0028 used), and an assignment
    # with a stale mirror wins over the mirror.
    if is_postgres:
        for table in FORCED_TABLES:
            op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")

    bind.execute(
        sa.text(
            "INSERT INTO source_assignments "
            "(workspace_id, source_id, account_id, assigned_at, released_at, reason, created_at) "
            "SELECT c.workspace_id, c.id, c.account_id, NULL, NULL, "
            "'migrated from channels.account_id', CURRENT_TIMESTAMP "
            "FROM channels c "
            "WHERE c.account_id IS NOT NULL "
            "  AND NOT EXISTS (SELECT 1 FROM source_assignments a "
            "                  WHERE a.source_id = c.id AND a.released_at IS NULL)"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE channels SET account_id = a.account_id "
            "FROM source_assignments a "
            "WHERE a.source_id = channels.id AND a.released_at IS NULL "
            "  AND channels.account_id IS DISTINCT FROM a.account_id"
        )
        if is_postgres
        else sa.text(
            "UPDATE channels SET account_id = ("
            "  SELECT a.account_id FROM source_assignments a "
            "  WHERE a.source_id = channels.id AND a.released_at IS NULL) "
            "WHERE EXISTS (SELECT 1 FROM source_assignments a "
            "              WHERE a.source_id = channels.id AND a.released_at IS NULL "
            "                AND (channels.account_id IS NULL OR channels.account_id <> a.account_id))"
        )
    )

    if not is_postgres:
        return

    # --- 2. the guard ----------------------------------------------------
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {GUARD_FUNCTION}() RETURNS trigger AS $$
        BEGIN
            IF coalesce(current_setting('app.assignment_write', true), '') = 'on' THEN
                RETURN NEW;
            END IF;

            IF TG_OP = 'INSERT' THEN
                IF NEW.account_id IS NOT NULL THEN
                    RAISE EXCEPTION
                        'channels.account_id is a mirror of source_assignments and cannot create an '
                        'assignment on INSERT; create the row unassigned and call app.assignments.assign()'
                        USING ERRCODE = 'raise_exception';
                END IF;
                RETURN NEW;
            END IF;

            IF NEW.account_id IS DISTINCT FROM OLD.account_id THEN
                RAISE EXCEPTION
                    'channels.account_id is a mirror of source_assignments and is not directly writable; '
                    'use app.assignments.assign() or app.assignments.release()'
                    USING ERRCODE = 'raise_exception';
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        f"CREATE TRIGGER {GUARD_TRIGGER} "
        f"BEFORE INSERT OR UPDATE OF account_id ON channels "
        f"FOR EACH ROW EXECUTE FUNCTION {GUARD_FUNCTION}()"
    )

    # Restore both, and prove it. A migration that lifted FORCE and did
    # not put it back would leave every tenant's rows readable by every
    # other, and would do it silently.
    for table in FORCED_TABLES:
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        forced = bind.execute(
            sa.text("SELECT relforcerowsecurity FROM pg_class WHERE relname = :t"), {"t": table}
        ).scalar()
        if not forced:
            raise RuntimeError(
                f"0029 left {table} without FORCE ROW LEVEL SECURITY — refusing to finish a "
                "migration that would leave the table readable across tenants"
            )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(f"DROP TRIGGER IF EXISTS {GUARD_TRIGGER} ON channels")
    op.execute(f"DROP FUNCTION IF EXISTS {GUARD_FUNCTION}()")
