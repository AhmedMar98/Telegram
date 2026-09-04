"""Two timestamps the models call required and the database calls optional.

``alembic check`` has failed on every push for this, and it is right to.
``Message.collected_at`` and ``CoverageSnapshot.created_at`` are declared
``Mapped[datetime]`` — not ``Mapped[datetime | None]`` — which is how the
other thirty-odd ``created_at`` columns in this schema are declared, and
SQLAlchemy reads that as NOT NULL. The two migrations that created these
tables wrote ``nullable=True`` instead. So the models promise a value that
the database does not require, and every reader that treats the column as
always-present is relying on a guarantee nothing enforces.

The drift is the migration's, not the model's: the house convention is a
required creation timestamp, ``default=utcnow`` fills it on every ORM
write, and no code path deliberately writes NULL. This migration makes the
database agree with the declaration rather than weakening the declaration
to match the database.

The RLS trap, for the sixth migration running
---------------------------------------------
Both tables carry FORCE row-level security and this connection carries no
tenant, so an UPDATE would match zero rows and report success — the defect
0028 documents and 43.9 documents before it. The backfill therefore runs
inside a FORCE window that is closed again immediately, and the close is
verified rather than assumed: a migration that leaves a protected table
readable across tenants is worse than a migration that fails.

``SET NOT NULL`` itself needs no window. It is DDL, and row-level security
filters DML; the validation scan PostgreSQL runs is internal and sees every
row regardless of policy.

Backfill value
--------------
``CURRENT_TIMESTAMP`` for a row whose creation time was never recorded is
the honest option available: it says "no earlier than now", and the
alternative — inventing a plausible past — would put a fabricated time in a
column whose whole purpose is to be trusted. In production these tables are
young and expected to hold no NULLs at all; the UPDATE is a safety net, not
the point of the migration.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0032_timestamp_not_null"
down_revision: str | None = "0031_collection_runtime"
branch_labels: str | None = None
depends_on: str | None = None

# (table, column) pairs whose model declares the value required.
_REQUIRED: tuple[tuple[str, str], ...] = (
    ("messages", "collected_at"),
    ("coverage_snapshots", "created_at"),
)


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    if is_postgres:
        for table, _ in _REQUIRED:
            op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")

    try:
        for table, column in _REQUIRED:
            op.execute(f"UPDATE {table} SET {column} = CURRENT_TIMESTAMP WHERE {column} IS NULL")
    finally:
        if is_postgres:
            for table, _ in _REQUIRED:
                op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    if is_postgres:
        # Verified, not assumed. The window is the dangerous part of this
        # migration; a silent failure to close it is the one outcome worth
        # refusing to finish on.
        for table, _ in _REQUIRED:
            forced = bind.execute(
                sa.text("SELECT relforcerowsecurity FROM pg_class WHERE relname = :t"),
                {"t": table},
            ).scalar()
            if not forced:
                raise RuntimeError(
                    f"0032 left {table} without FORCE ROW LEVEL SECURITY — refusing to finish a "
                    "migration that would leave the table readable across tenants"
                )

    for table, column in _REQUIRED:
        op.alter_column(table, column, existing_type=sa.DateTime(), nullable=False)


def downgrade() -> None:
    for table, column in _REQUIRED:
        op.alter_column(table, column, existing_type=sa.DateTime(), nullable=True)
