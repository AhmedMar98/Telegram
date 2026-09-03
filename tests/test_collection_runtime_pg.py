"""What the collection runtime can only be proved against real PostgreSQL.

Three things live here because SQLite cannot host them:

1. **The monotonicity triggers.** ``app/progress.py`` refuses a backwards
   watermark, but that protects one code path. Migration 0029 refuses it
   in the database, which protects the psql session, the script nobody
   remembers, and the future version of ``progress.py`` itself. A rule
   only the application knows is a rule the application can forget.
2. **Row-level security on the two new tables.** The trap this repository
   has now met five times: a connection with no tenant reads zero rows and
   writes nothing, quietly. The test is that the tables are FORCEd and
   carry a policy after every migration has run.
3. **Real concurrency.** Ten workers on ten accounts, each with its own
   connection out of a real pool, all writing at once. The SQLite suite
   runs one connection through a StaticPool, so it can prove the logic and
   not the concurrency.

Everything skips loudly without ``MIGRATION_TEST_DSN`` rather than passing
somewhere the guards do not exist.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from app import assignments, progress
from app.collection import runs as run_log
from app.collection.failures import FailureKind
from app.models import Channel, CollectionRun, SourceProgress, TelegramAccount
from app.runtime.supervisor import Supervisor, SupervisorConfig
from app.runtime.worker import AccountWorker, WorkerConfig
from app.timeutil import utcnow
from tests.test_collection_runtime import FakeReader, msg

REPO = Path(__file__).resolve().parent.parent
MIGRATION_DSN = os.environ.get("MIGRATION_TEST_DSN", "")

pg_only = pytest.mark.skipif(
    not MIGRATION_DSN,
    reason="MIGRATION_TEST_DSN not set — these guards are PostgreSQL triggers and policies",
)

WORKSPACE_ID = 1


def _alembic(command: str, target: str) -> None:
    env = {
        **os.environ,
        "DATABASE_URL": MIGRATION_DSN,
        "SECRET_KEY": "test-secret-key",
        "ENVIRONMENT": "test",
    }
    subprocess.run(
        [sys.executable, "-m", "alembic", command, target],
        cwd=REPO,
        env=env,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def pg_engine():
    """A database at head with one workspace. Torn down to base after."""
    _alembic("downgrade", "base")
    _alembic("upgrade", "head")
    engine = create_engine(MIGRATION_DSN)
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.workspace_id', '1', false)"))
        conn.execute(text("INSERT INTO workspaces (id, name, created_at) VALUES (1, 'w', now())"))
    yield engine
    engine.dispose()
    _alembic("downgrade", "base")


@pytest.fixture
def pg_sessions(pg_engine):
    """A session factory bound to the migrated database."""
    return sessionmaker(bind=pg_engine, autoflush=False, autocommit=False, future=True)


def _seed(sessions, *, accounts: int = 1, sources_per_account: int = 1):
    """Accounts, sources and assignments, through the services that own them."""
    created: list[tuple[int, list[int]]] = []
    with sessions() as db:
        from app.rls import scope_session_to_workspace

        scope_session_to_workspace(db, WORKSPACE_ID)
        for index in range(accounts):
            account = TelegramAccount(workspace_id=WORKSPACE_ID, label=f"a{index}", session_string="x")
            db.add(account)
            db.flush()
            source_ids = []
            for n in range(sources_per_account):
                channel = Channel(
                    workspace_id=WORKSPACE_ID,
                    tg_channel_id=f"-100{index}{n}",
                    username=f"src{index}_{n}",
                    title="قناة",
                )
                db.add(channel)
                db.flush()
                assignments.assign(db, channel, account.id, reason="fixture")
                # The row the runtime creates on its first attempt. Seeded
                # here so the tests below have something to move.
                progress.ensure(db, channel, SourceProgress.LIVE)
                source_ids.append(channel.id)
            created.append((account.id, source_ids))
        db.commit()
    return created


# --- 1. the triggers ------------------------------------------------------


@pg_only
def test_the_database_refuses_a_backwards_watermark(pg_engine, pg_sessions):
    """Sabotage: drop ``trg_source_progress_monotonic`` and this passes —
    which is the whole point of having it, because ``app/progress.py``
    cannot police a hand-written UPDATE."""
    (account_id, [source_id]) = _seed(pg_sessions)[0]
    with pg_sessions() as db:
        from app.rls import scope_session_to_workspace

        scope_session_to_workspace(db, WORKSPACE_ID)
        progress.advance(db, db.get(Channel, source_id), 400, account_id=account_id)
        db.commit()

    with pg_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.workspace_id', '1', false)"))
        with pytest.raises(DBAPIError) as caught:
            conn.execute(
                text("UPDATE source_progress SET current_watermark = 10 WHERE source_id = :s"),
                {"s": source_id},
            )
    assert "may not move backwards" in str(caught.value)


@pg_only
def test_the_database_allows_a_watermark_to_move_forward(pg_engine, pg_sessions):
    """A guard that refuses everything is also broken."""
    _seed(pg_sessions)
    with pg_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.workspace_id', '1', false)"))
        conn.execute(text("UPDATE source_progress SET current_watermark = 999"))
        assert conn.execute(text("SELECT max(current_watermark) FROM source_progress")).scalar() == 999


@pg_only
def test_the_legacy_mirror_cannot_move_backwards_either(pg_engine, pg_sessions):
    """``channels.last_message_id`` is a mirror of the LIVE track now.

    Sabotage: drop ``trg_channels_watermark_monotonic`` and a stray UPDATE
    silently rewinds the column the scheduled collector still reads.
    """
    (account_id, [source_id]) = _seed(pg_sessions)[0]
    with pg_sessions() as db:
        from app.rls import scope_session_to_workspace

        scope_session_to_workspace(db, WORKSPACE_ID)
        progress.advance(db, db.get(Channel, source_id), 250, account_id=account_id)
        db.commit()

    with pg_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.workspace_id', '1', false)"))
        with pytest.raises(DBAPIError) as caught:
            conn.execute(text("UPDATE channels SET last_message_id = 3 WHERE id = :s"), {"s": source_id})
    assert "may not move backwards" in str(caught.value)


# --- 2. tenant isolation --------------------------------------------------


@pg_only
def test_the_new_tables_are_forced_and_carry_a_policy(pg_engine):
    """The RLS trap, asserted at the end state rather than assumed.

    Sabotage: delete the ENABLE/FORCE loop at the end of 0029 and this
    fails. Without it the tables would be readable across tenants and
    nothing would say so.
    """
    with pg_engine.connect() as conn:
        for table in ("source_progress", "collection_runs"):
            enabled, forced = conn.execute(
                text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = :t"),
                {"t": table},
            ).one()
            assert enabled and forced, f"{table} is not protected"
            policies = conn.execute(
                text("SELECT count(*) FROM pg_policies WHERE tablename = :t"), {"t": table}
            ).scalar()
            assert policies == 1, f"{table} has no isolation policy"


@pg_only
def test_a_connection_with_no_tenant_sees_no_progress(pg_engine, pg_sessions):
    """What FORCE actually buys, demonstrated rather than described.

    On its own engine with pooling disabled, because a tenant set with
    ``set_config(..., false)`` is *session*-scoped and rides a pooled
    connection into the next checkout. That is a fixture artefact — the
    application uses ``SET LOCAL``, which dies with its transaction — but
    it would make this particular test lie, so it gets a clean connection.
    """
    from sqlalchemy.pool import NullPool

    _seed(pg_sessions)
    fresh = create_engine(MIGRATION_DSN, poolclass=NullPool)
    try:
        with fresh.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM source_progress")).scalar() == 0
        with fresh.connect() as conn:
            conn.execute(text("SELECT set_config('app.workspace_id', '1', true)"))
            assert conn.execute(text("SELECT count(*) FROM source_progress")).scalar() == 1
    finally:
        fresh.dispose()


# --- 3. the migration's backfill -----------------------------------------


@pg_only
def test_the_backfill_gives_every_channel_a_live_track_at_its_old_watermark():
    """0029 must start from where the old collector actually got to.

    Starting at zero would re-read every archive and store nothing while
    spending the whole rate-limit budget rediscovering it.
    """
    _alembic("downgrade", "base")
    _alembic("upgrade", "0028_account_access_join_queue")
    engine = create_engine(MIGRATION_DSN)
    try:
        with engine.begin() as conn:
            # ``channels`` carries FORCE row-level security and this
            # connection is the table owner, so without a tenant the insert
            # is refused. The same trap the migration itself has to dodge.
            conn.execute(text("SELECT set_config('app.workspace_id', '1', false)"))
            conn.execute(text("INSERT INTO workspaces (id, name, created_at) VALUES (1, 'w', now())"))
            conn.execute(
                text(
                    "INSERT INTO channels (workspace_id, tg_channel_id, kind, source, "
                    "last_message_id, is_active, created_at) "
                    "SELECT 1, '-100'||g, 'channel', 'userbot', g * 10, true, now() "
                    "FROM generate_series(1, 5) g"
                )
            )
        _alembic("upgrade", "head")
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.workspace_id', '1', false)"))
            rows = conn.execute(
                text(
                    "SELECT p.current_watermark, p.coverage_status, c.last_message_id "
                    "FROM source_progress p JOIN channels c ON c.id = p.source_id "
                    "WHERE p.track = 'LIVE' ORDER BY c.last_message_id"
                )
            ).all()
        assert [r[0] for r in rows] == [10, 20, 30, 40, 50]
        assert [r[0] for r in rows] == [r[2] for r in rows]
        assert {r[1] for r in rows} == {"UNKNOWN_COVERAGE"}, (
            "nothing in the old schema recorded whether a range was examined, "
            "so claiming NO_DETECTED_GAP would be inventing a measurement"
        )
    finally:
        engine.dispose()
        _alembic("downgrade", "base")


# --- 4. crash recovery on the real thing ----------------------------------


@pg_only
def test_a_crashed_run_is_closed_at_startup_and_the_next_run_resumes(pg_sessions):
    """The full recovery story, end to end.

    A worker dies mid-run: the RUNNING row survives with a stale heartbeat,
    the persisted messages survive, and the watermark points at exactly the
    last one stored. Startup recovery closes the run and the next sweep
    resumes from that watermark — re-reading nothing and skipping nothing.
    """
    (account_id, [source_id]) = _seed(pg_sessions)[0]
    from app.rls import scope_session_to_workspace

    # A run that got half way and then the process died.
    with pg_sessions() as db:
        scope_session_to_workspace(db, WORKSPACE_ID)
        channel = db.get(Channel, source_id)
        progress.advance(db, channel, 3, account_id=account_id)
        run = run_log.start(db, channel, mode=SourceProgress.LIVE, account_id=account_id)
        run.heartbeat_at = utcnow() - timedelta(hours=1)
        db.commit()
        dead_run = run.id

    supervisor = Supervisor(
        workspace_id=WORKSPACE_ID,
        session_factory=pg_sessions,
        reader_factory=lambda account: FakeReader(),
    )
    assert supervisor.recover() == [dead_run]

    with pg_sessions() as db:
        scope_session_to_workspace(db, WORKSPACE_ID)
        closed = db.get(CollectionRun, dead_run)
        assert closed.state == CollectionRun.FAILED
        assert closed.failure_kind == FailureKind.WORKER_FAILURE.value
        assert progress.get(db, source_id).current_watermark == 3

    reader = FakeReader({"src0_0": [msg(n) for n in range(1, 7)]})
    worker = AccountWorker(
        workspace_id=WORKSPACE_ID,
        account_id=account_id,
        reader=reader,
        session_factory=pg_sessions,
        config=WorkerConfig(flush_every=1),
    )
    asyncio.run(worker.cycle())

    assert reader.fetches[0][1] == 3, "resumed from the watermark the crash left behind"
    with pg_sessions() as db:
        scope_session_to_workspace(db, WORKSPACE_ID)
        assert progress.get(db, source_id).current_watermark == 6


# --- 5. ten accounts at once ----------------------------------------------


@pg_only
def test_ten_accounts_collect_concurrently_without_touching_each_other(pg_sessions):
    """The concurrency claim, measured rather than asserted.

    Ten accounts, three sources each, all running as concurrent tasks
    against one real connection pool. What is proved: every source's
    watermark reaches its own last message, every run is COMPLETED, and no
    account's progress lands on another's source. Ten is the number this
    test exercises — it is **not** a measured capacity ceiling, and
    ``SupervisorConfig.max_workers`` exists so a deployment that measures
    one can say so.
    """
    fleet = _seed(pg_sessions, accounts=10, sources_per_account=3)

    workers = []
    for index, (account_id, _) in enumerate(fleet):
        messages = {
            f"src{index}_{n}": [msg(m, f"https://e.example/a{index}s{n}m{m}") for m in range(1, 5)]
            for n in range(3)
        }
        workers.append(
            AccountWorker(
                workspace_id=WORKSPACE_ID,
                account_id=account_id,
                reader=FakeReader(messages),
                session_factory=pg_sessions,
                config=WorkerConfig(flush_every=2),
            )
        )

    async def run_all():
        return await asyncio.gather(*(w.cycle() for w in workers))

    reports = asyncio.run(run_all())

    assert sum(r.runs_completed for r in reports) == 30
    assert sum(r.runs_failed for r in reports) == 0
    assert sum(r.messages_seen for r in reports) == 120

    from app.rls import scope_session_to_workspace

    with pg_sessions() as db:
        scope_session_to_workspace(db, WORKSPACE_ID)
        watermarks = {
            row.source_id: row.current_watermark
            for row in db.query(SourceProgress).filter(SourceProgress.track == SourceProgress.LIVE).all()
        }
        states = {row.id: (row.state, row.account_id, row.source_id) for row in db.query(CollectionRun).all()}

    assert len(watermarks) == 30 and set(watermarks.values()) == {4}
    assert {s[0] for s in states.values()} == {CollectionRun.COMPLETED}

    # No run was recorded against an account that does not hold its source.
    owner_of = {source_id: account_id for account_id, sources in fleet for source_id in sources}
    for _, account_id, source_id in states.values():
        assert owner_of[source_id] == account_id


@pg_only
def test_one_account_failing_does_not_stop_the_others(pg_sessions):
    """Isolation is structural: the workers share no object to break."""
    fleet = _seed(pg_sessions, accounts=3, sources_per_account=1)

    workers = []
    for index, (account_id, _) in enumerate(fleet):
        reader = (
            FakeReader({f"src{index}_0": [msg(1)]}, error=ConnectionError("down"))
            if index == 1
            else FakeReader({f"src{index}_0": [msg(1), msg(2)]})
        )
        workers.append(
            AccountWorker(
                workspace_id=WORKSPACE_ID,
                account_id=account_id,
                reader=reader,
                session_factory=pg_sessions,
                config=WorkerConfig(),
            )
        )

    async def run_all():
        return await asyncio.gather(*(w.cycle() for w in workers))

    reports = asyncio.run(run_all())

    assert [r.runs_completed for r in reports] == [1, 0, 1]
    assert [r.runs_failed for r in reports] == [0, 1, 0]

    from app.rls import scope_session_to_workspace

    with pg_sessions() as db:
        scope_session_to_workspace(db, WORKSPACE_ID)
        healthy = [
            row.current_watermark for row in db.query(SourceProgress).order_by(SourceProgress.source_id).all()
        ]
    assert healthy == [2, 0, 2], "the broken account moved nothing, the others finished"


@pg_only
def test_the_supervisor_starts_a_worker_per_active_account_up_to_its_bound(pg_sessions):
    fleet = _seed(pg_sessions, accounts=10, sources_per_account=1)
    readers = {account_id: FakeReader() for account_id, _ in fleet}

    supervisor = Supervisor(
        workspace_id=WORKSPACE_ID,
        session_factory=pg_sessions,
        reader_factory=lambda account: readers[account.id],
        config=SupervisorConfig(max_workers=4, worker=WorkerConfig(cycle_pause=0.01)),
    )

    async def scenario():
        await supervisor.supervise_once()
        running = [s for s in supervisor._slots.values() if s.task is not None]
        assert len(running) == 4, "the bound is respected"

        # A retired account must free its place. Sabotage: put the bound
        # back on ``eligible_accounts``'s query and the eleventh healthy
        # account can never take over from a revoked first one.
        retired = running[0]
        retired.retired_reason = "revoked"
        retired.task.cancel()
        await asyncio.gather(retired.task, return_exceptions=True)
        retired.task = None

        await supervisor.supervise_once()
        started = {s.account_id for s in supervisor._slots.values() if s.task is not None}
        assert len(started) == 4
        assert retired.account_id not in started, "the retired account is not restarted"

        await supervisor.shutdown()
        assert all(s.task is None for s in supervisor._slots.values())

    asyncio.run(scenario())
