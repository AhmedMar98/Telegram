"""One authority for who collects a source, proved from both sides.

``source_assignments`` decides; ``Channel.account_id`` mirrors. The tests
that matter are the ones that fail when that stops being true: an
assignment made by writing the mirror, a mirror that drifts from the
table, a history that loses the account that used to hold a source.

The trigger enforcing it is PostgreSQL-only — SQLite has no trigger and
the suite builds its schema with ``create_all`` rather than migrations —
so the tests that exercise it skip loudly without a DSN instead of
passing somewhere the guard does not exist.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import assignments
from app.database import SessionLocal
from app.models import Channel, SourceAssignment, TelegramAccount, Workspace

REPO = Path(__file__).resolve().parent.parent
MIGRATION_DSN = os.environ.get("MIGRATION_TEST_DSN", "")

pg_only = pytest.mark.skipif(
    not MIGRATION_DSN,
    reason="MIGRATION_TEST_DSN not set — the mirror guard is a PostgreSQL trigger",
)


def _fixture(db: Session, accounts: int = 2) -> tuple[Workspace, Channel, list[TelegramAccount]]:
    workspace = Workspace(name="authority")
    db.add(workspace)
    db.flush()
    rows = []
    for index in range(accounts):
        account = TelegramAccount(workspace_id=workspace.id, label=f"a{index}", session_string="x")
        db.add(account)
        rows.append(account)
    db.flush()
    channel = Channel(workspace_id=workspace.id, tg_channel_id="-1001234", username="src")
    db.add(channel)
    db.flush()
    return workspace, channel, rows


# --- the authority itself -------------------------------------------------


def test_the_assignment_table_decides_and_the_column_follows(client):
    with SessionLocal() as db:
        _, channel, (first, second) = _fixture(db)

        assignments.assign(db, channel, first.id, reason="initial")
        db.commit()
        assert assignments.current_account_id(db, channel.id) == first.id
        assert channel.account_id == first.id, "the mirror did not follow the decision"

        assignments.assign(db, channel, second.id, reason="rebalance")
        db.commit()
        assert assignments.current_account_id(db, channel.id) == second.id
        assert channel.account_id == second.id


def test_reassignment_closes_the_old_row_instead_of_erasing_it(client):
    """The account that used to hold a source is not forgotten."""
    with SessionLocal() as db:
        _, channel, (first, second) = _fixture(db)
        assignments.assign(db, channel, first.id, reason="initial")
        assignments.assign(db, channel, second.id, reason="account unavailable")
        db.commit()

        history = assignments.history_for_source(db, channel.id)
        assert [row.account_id for row in history] == [first.id, second.id]
        assert history[0].released_at is not None, "the previous assignment was erased, not closed"
        assert history[1].released_at is None
        assert history[1].reason == "account unavailable"


def test_assigning_the_same_account_twice_does_not_invent_history(client):
    with SessionLocal() as db:
        _, channel, (first, _) = _fixture(db)
        assignments.assign(db, channel, first.id, reason="initial")
        assignments.assign(db, channel, first.id, reason="rebalance")
        db.commit()
        assert len(assignments.history_for_source(db, channel.id)) == 1


def test_releasing_leaves_the_source_unassigned_and_the_history_intact(client):
    with SessionLocal() as db:
        _, channel, (first, _) = _fixture(db)
        assignments.assign(db, channel, first.id, reason="initial")
        assignments.release(db, channel, reason="account removed")
        db.commit()

        assert assignments.current_account_id(db, channel.id) is None
        assert channel.account_id is None
        history = assignments.history_for_source(db, channel.id)
        assert len(history) == 1 and history[0].released_at is not None


def test_the_two_never_disagree_across_a_sequence_of_moves(client):
    with SessionLocal() as db:
        workspace, channel, (first, second) = _fixture(db)
        for account in (first, second, first, None, second):
            assignments.assign(db, channel, account.id if account else None, reason="churn")
        db.commit()
        assert assignments.mirror_disagreements(db, workspace.id) == []


def test_an_open_assignment_is_exclusive_even_through_the_service(client):
    """The service refuses what the database would refuse anyway."""
    from sqlalchemy.exc import IntegrityError

    with SessionLocal() as db:
        workspace, channel, (first, second) = _fixture(db)
        assignments.assign(db, channel, first.id, reason="initial")
        db.commit()
        # Bypassing the service is the only way to attempt this, which is
        # the point: the constraint is in the database, not in Python.
        db.add(SourceAssignment(workspace_id=workspace.id, source_id=channel.id, account_id=second.id))
        with pytest.raises(IntegrityError):
            db.commit()


# --- legacy compatibility -------------------------------------------------


def test_the_collector_query_still_finds_its_sources(client):
    """The mirror is what ``scripts/collect.py`` filters on. It must work.

    Not a redundant test: the whole point of keeping the column is that
    the collection runtime is untouched by this phase, and a mirror that
    stopped being readable the old way would have moved the breakage
    rather than removed it.
    """
    with SessionLocal() as db:
        workspace, channel, (first, _) = _fixture(db)
        assignments.assign(db, channel, first.id, reason="initial")
        db.commit()

        found = (
            db.query(Channel).filter(Channel.workspace_id == workspace.id, Channel.account_id == first.id).all()
        )
        assert [row.id for row in found] == [channel.id]


def _account_for(workspace_id: int, label: str) -> int:
    from app.crypto import encrypt_field

    with SessionLocal() as db:
        account = TelegramAccount(workspace_id=workspace_id, label=label, session_string=encrypt_field("session"))
        db.add(account)
        db.commit()
        return account.id


def test_the_reassignment_endpoint_writes_through_the_service(client):
    """PATCH /channels/{id} is a user-facing path; it must not bypass."""
    from tests.conftest import register_workspace

    register_workspace(client, email="auth@example.com", workspace_name="ws")
    workspace_id = client.get("/auth/me").json()["workspace_id"]
    account_id = _account_for(workspace_id, "a")
    channel = client.post("/channels", json={"tg_channel_id": "-100999", "username": "u"}).json()

    response = client.patch(f"/channels/{channel['id']}", json={"account_id": account_id})
    assert response.status_code == 200, response.text

    with SessionLocal() as db:
        assert assignments.current_account_id(db, channel["id"]) == account_id
        row = assignments.open_assignment(db, channel["id"])
        assert row is not None and row.reason == "manual reassignment"


def test_creating_a_channel_with_an_account_records_a_real_assignment(client):
    from tests.conftest import register_workspace

    register_workspace(client, email="create@example.com", workspace_name="ws")
    workspace_id = client.get("/auth/me").json()["workspace_id"]
    account_id = _account_for(workspace_id, "a")
    channel = client.post(
        "/channels", json={"tg_channel_id": "-100777", "username": "u", "account_id": account_id}
    ).json()

    assert channel["account_id"] == account_id
    with SessionLocal() as db:
        row = assignments.open_assignment(db, channel["id"])
        assert row is not None and row.account_id == account_id


# --- the guard, where it exists -------------------------------------------


@pytest.fixture
def pg_db():
    """A database at head, with one workspace, account and channel seeded."""
    env = {**os.environ, "DATABASE_URL": MIGRATION_DSN, "SECRET_KEY": "test-secret-key", "ENVIRONMENT": "test"}
    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "base"],
        cwd=REPO,
        env=env,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO,
        env=env,
        check=True,
        capture_output=True,
    )
    engine = create_engine(MIGRATION_DSN)
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.workspace_id', '1', false)"))
        conn.execute(text("INSERT INTO workspaces (id, name, created_at) VALUES (1, 'w', now())"))
        conn.execute(
            text(
                "INSERT INTO telegram_accounts (id, workspace_id, label, session_string, is_active, "
                "consecutive_failures, links_collected, created_at) "
                "VALUES (1, 1, 'a', 'x', true, 0, 0, now()), (2, 1, 'b', 'x', true, 0, 0, now())"
            )
        )
        conn.execute(
            text(
                "INSERT INTO channels (id, workspace_id, tg_channel_id, kind, source, last_message_id, "
                "is_active, created_at) VALUES (1, 1, '-1001', 'channel', 'userbot', 0, true, now())"
            )
        )
    yield engine
    engine.dispose()
    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "base"],
        cwd=REPO,
        env=env,
        check=True,
        capture_output=True,
    )


@pg_only
def test_the_mirror_cannot_be_written_directly(pg_db):
    """A hand-written UPDATE is exactly the drift this closes."""
    with pg_db.begin() as conn:
        conn.execute(text("SELECT set_config('app.workspace_id', '1', false)"))
        with pytest.raises(DBAPIError) as caught:
            conn.execute(text("UPDATE channels SET account_id = 1 WHERE id = 1"))
    assert "not directly writable" in str(caught.value)


@pg_only
def test_an_assignment_cannot_be_created_by_inserting_a_channel(pg_db):
    with pg_db.begin() as conn:
        conn.execute(text("SELECT set_config('app.workspace_id', '1', false)"))
        with pytest.raises(DBAPIError) as caught:
            conn.execute(
                text(
                    "INSERT INTO channels (workspace_id, tg_channel_id, kind, source, account_id, "
                    "last_message_id, is_active, created_at) "
                    "VALUES (1, '-1002', 'channel', 'userbot', 1, 0, true, now())"
                )
            )
    assert "cannot create an assignment on INSERT" in str(caught.value)


@pg_only
def test_the_sanctioned_path_is_allowed_through(pg_db):
    """The guard must not be a wall: the service still has to work."""
    with pg_db.begin() as conn:
        conn.execute(text("SELECT set_config('app.workspace_id', '1', false)"))
        conn.execute(
            text(
                "INSERT INTO source_assignments (workspace_id, source_id, account_id, assigned_at, reason, "
                "created_at) VALUES (1, 1, 1, now(), 'service', now())"
            )
        )
        conn.execute(text("SET LOCAL app.assignment_write = 'on'"))
        conn.execute(text("UPDATE channels SET account_id = 1 WHERE id = 1"))

    with pg_db.connect() as conn:
        conn.execute(text("SELECT set_config('app.workspace_id', '1', false)"))
        mirror = conn.execute(text("SELECT account_id FROM channels WHERE id = 1")).scalar()
        assert mirror == 1


@pg_only
def test_a_write_that_does_not_touch_the_column_is_untouched(pg_db):
    """The trigger fires on account_id only; ordinary updates still work."""
    with pg_db.begin() as conn:
        conn.execute(text("SELECT set_config('app.workspace_id', '1', false)"))
        conn.execute(text("UPDATE channels SET title = 'renamed', last_message_id = 42 WHERE id = 1"))
    with pg_db.connect() as conn:
        conn.execute(text("SELECT set_config('app.workspace_id', '1', false)"))
        assert conn.execute(text("SELECT last_message_id FROM channels WHERE id = 1")).scalar() == 42


@pg_only
def test_the_permission_does_not_outlive_its_transaction(pg_db):
    """SET LOCAL, not SET: the next caller must not inherit the grant."""
    with pg_db.begin() as conn:
        conn.execute(text("SELECT set_config('app.workspace_id', '1', false)"))
        conn.execute(text("SET LOCAL app.assignment_write = 'on'"))
        conn.execute(
            text(
                "INSERT INTO source_assignments (workspace_id, source_id, account_id, assigned_at, reason, "
                "created_at) VALUES (1, 1, 1, now(), 'service', now())"
            )
        )
        conn.execute(text("UPDATE channels SET account_id = 1 WHERE id = 1"))

    with pg_db.begin() as conn:
        conn.execute(text("SELECT set_config('app.workspace_id', '1', false)"))
        with pytest.raises(DBAPIError):
            conn.execute(text("UPDATE channels SET account_id = 2 WHERE id = 1"))
