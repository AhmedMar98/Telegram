"""tenant isolation enforced by Postgres

Revision ID: 0020_row_level_security
Revises: 0019_workspace_webhook
Create Date: 2026-08-21

Postgres only, deliberately. SQLite has no row-level security at all, so
this is a real behavioural difference between the development engine and
the production one — the *second* such difference in this project, after
full-text search (docs/24-decisions.md §2). Both are documented rather
than papered over, and both are covered by the CI job that runs the suite
against real Postgres.

Two details below are each the difference between a working feature and a
broken one, and both were measured on PostgreSQL 16 rather than assumed.

**FORCE is not optional**, and its absence is not a smaller version of
this feature. With RLS enabled but not forced, the table's owner — which
is the role the application connects as, since Alembic created the tables
— sees every row regardless of the policy. The migration that enables
without forcing produces a database that looks protected in ``pg_class``
and protects nothing.

**NULLIF is not decoration.** A transaction-local ``set_config`` reverts
when its transaction ends, and it does not revert to NULL: measured, the
GUC reads back as the **empty string**. So ``current_setting(..., true)``
returns NULL only on a connection that has never carried a tenant, and
``''`` on every connection that has served one request already — which,
on a pool, is nearly all of them. ``''::int`` does not evaluate to false;
it raises *invalid input syntax for type integer*. Without the NULLIF,
the first request on a fresh connection fails closed correctly and every
later one on that connection returns HTTP 500 — the failure mode that
passes a smoke test and breaks in production.
"""

from __future__ import annotations

from alembic import op

revision = "0020_row_level_security"
down_revision = "0019_workspace_webhook"
branch_labels = None
depends_on = None

# Copied, not imported from app.rls, and that is deliberate. A migration
# describes the schema at one moment; if it imported the live list, adding
# a table to that list later would silently change what this
# already-applied migration *did*, and re-running it on a fresh database
# would produce a different result than it produced on an old one. The
# same reasoning is spelled out in 0018_feedback_domain.py.
PROTECTED_TABLES = (
    "channels",
    "audit_log",
    "notification_preferences",
    "notifications",
    "workflow_runs",
    "saved_searches",
    "classification_feedback",
)

POLICY = "tenant_isolation"
SETTING = "app.workspace_id"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for table in PROTECTED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        # No WITH CHECK clause: when one is omitted Postgres applies the
        # USING expression to writes too, which is exactly what is wanted
        # — an INSERT carrying another workspace's id is refused, not
        # merely invisible afterwards.
        #
        # NULLIF turns the empty string back into NULL (see the module
        # docstring). NULL compared to workspace_id yields NULL, which the
        # policy treats as "no", so an unset tenant sees nothing and
        # writes nothing — without the cast ever being handed a value it
        # cannot parse.
        op.execute(
            f"CREATE POLICY {POLICY} ON {table} "
            f"USING (workspace_id = NULLIF(current_setting('{SETTING}', true), '')::int)"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for table in PROTECTED_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
