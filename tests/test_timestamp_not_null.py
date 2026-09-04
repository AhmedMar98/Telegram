"""Migration 0032: the two timestamps the models call required.

The interesting half of this migration is not ``SET NOT NULL`` — it is the
UPDATE that runs first. Both tables carry FORCE row-level security and the
migration's connection carries no tenant, so an unguarded UPDATE matches
zero rows and reports success: the schema change then either fails on rows
it could not see or, worse, succeeds because there happened to be none this
time. So what is proved here is that the backfill *wrote*, on a database
shaped like production — owned by a role that is neither superuser nor
BYPASSRLS, with FORCE in place — and that the window closed behind it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from app.config import normalize_database_url

REPO = Path(__file__).resolve().parent.parent

# Same contract as tests/test_target_source_model.py: this can only be
# proved where the chain actually runs, and skipping loudly beats passing
# vacuously on SQLite.
# Kept raw here because alembic receives it as DATABASE_URL and app.config
# normalises it there; every create_engine below normalises at its own call
# site, for the reason app.config does it — a bare postgresql:// resolves to
# psycopg2, which this project does not ship.
MIGRATION_DSN = os.environ.get("MIGRATION_TEST_DSN", "")

pg_migration = pytest.mark.skipif(
    not MIGRATION_DSN,
    reason="MIGRATION_TEST_DSN not set — the backfill can only be proved on real Postgres",
)

_COLUMNS = (("messages", "collected_at"), ("coverage_snapshots", "created_at"))


def _alembic(env: dict[str, str], *args: str) -> None:
    subprocess.run([sys.executable, "-m", "alembic", *args], cwd=REPO, env=env, check=True, capture_output=True)


@pytest.fixture
def db_at_0031():
    env = {**os.environ, "DATABASE_URL": MIGRATION_DSN, "SECRET_KEY": "test-secret-key", "ENVIRONMENT": "test"}
    _alembic(env, "downgrade", "base")
    _alembic(env, "upgrade", "0031_collection_runtime")
    yield MIGRATION_DSN, env
    _alembic(env, "downgrade", "base")


def _tenant(conn, workspace_id: int = 1) -> None:
    conn.execute(text("SELECT set_config('app.workspace_id', :w, false)"), {"w": str(workspace_id)})


@pg_migration
def test_the_backfill_actually_writes_through_force_rls(db_at_0031):
    """A row with no timestamp gets one, rather than being silently skipped."""
    url, env = db_at_0031
    engine = create_engine(normalize_database_url(url))

    with engine.begin() as conn:
        _tenant(conn)
        conn.execute(text("INSERT INTO workspaces (id, name, created_at) VALUES (1, 'w', CURRENT_TIMESTAMP)"))
        conn.execute(
            text(
                "INSERT INTO channels (id, workspace_id, tg_channel_id, title, is_active, created_at, "
                "last_message_id) VALUES (1, 1, '-100', 'c', true, CURRENT_TIMESTAMP, 0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO messages (workspace_id, channel_id, tg_message_id, collected_at) "
                "VALUES (1, 1, 11, NULL), (1, 1, 12, NULL)"
            )
        )

    with engine.begin() as conn:
        _tenant(conn)
        before = conn.execute(text("SELECT count(*) FROM messages WHERE collected_at IS NULL")).scalar()
    assert before == 2, "the fixture must start with the NULLs this migration exists to fill"

    _alembic(env, "upgrade", "head")

    with engine.begin() as conn:
        _tenant(conn)
        assert conn.execute(text("SELECT count(*) FROM messages WHERE collected_at IS NULL")).scalar() == 0
        # Filled, not deleted: a backfill that loses the row it could not
        # fill would satisfy the constraint and destroy the record.
        assert conn.execute(text("SELECT count(*) FROM messages")).scalar() == 2
    engine.dispose()


@pg_migration
def test_the_columns_end_up_not_null(db_at_0031):
    url, env = db_at_0031
    _alembic(env, "upgrade", "head")

    engine = create_engine(normalize_database_url(url))
    with engine.begin() as conn:
        for table, column in _COLUMNS:
            nullable = conn.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns WHERE table_name = :t AND column_name = :c"
                ),
                {"t": table, "c": column},
            ).scalar()
            assert nullable == "NO", f"{table}.{column} is still nullable"
    engine.dispose()


@pg_migration
def test_force_row_level_security_is_back_on_both_tables(db_at_0031):
    """The window is the dangerous part. It has to close."""
    url, env = db_at_0031
    _alembic(env, "upgrade", "head")

    engine = create_engine(normalize_database_url(url))
    with engine.begin() as conn:
        for table, _ in _COLUMNS:
            forced = conn.execute(
                text("SELECT relforcerowsecurity FROM pg_class WHERE relname = :t"), {"t": table}
            ).scalar()
            assert forced is True, f"{table} left without FORCE ROW LEVEL SECURITY"
    engine.dispose()


@pg_migration
def test_the_downgrade_returns_the_columns_to_nullable(db_at_0031):
    """Reversible, so a deploy that has to go back can."""
    url, env = db_at_0031
    _alembic(env, "upgrade", "head")
    _alembic(env, "downgrade", "0031_collection_runtime")

    engine = create_engine(normalize_database_url(url))
    with engine.begin() as conn:
        for table, column in _COLUMNS:
            nullable = conn.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns WHERE table_name = :t AND column_name = :c"
                ),
                {"t": table, "c": column},
            ).scalar()
            assert nullable == "YES"
    engine.dispose()
