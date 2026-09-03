"""The four separations the target model exists to enforce.

Each test here breaks if the invariant it names stops holding, which is
the only property that makes a test worth the line count. The invariants:

    Source     ≠ Resource        a dialog is not a link
    Resource   ≠ Occurrence      one link seen 100 times is 1 + 100
    Identity   ≠ State           a source stays itself when it goes private
    Assignment ≠ Collection      being responsible is not having collected

The migration backfill gets its own test at the bottom, run through real
Alembic against a real database file rather than through the models —
because what has to be proved is that *the migration* produces the right
rows, and asserting against the ORM would prove only that the ORM works.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.identity import canonical_id, source_identity_key
from app.models import (
    Channel,
    Evidence,
    Occurrence,
    Resource,
    SourceAccess,
    SourceAssignment,
    SourceEvent,
    TelegramAccount,
    Workspace,
)

REPO = Path(__file__).resolve().parent.parent


# --- identity -------------------------------------------------------------


def test_the_two_spellings_of_one_channel_share_an_identity():
    """Telethon writes -1001234567890; a person pastes 1234567890."""
    assert source_identity_key("-1001234567890") == source_identity_key("1234567890") == "1234567890"


def test_a_positive_id_beginning_with_100_is_left_alone():
    """The edge case that makes the naive version wrong.

    Stripping a leading ``100`` from positive ids too would send the
    dashboard's spelling and Telethon's spelling to different keys for a
    channel genuinely numbered 1001234 — and they would silently never
    match again.
    """
    assert source_identity_key("-1001001234") == source_identity_key("1001234") == "1001234"


def test_a_synthetic_row_keeps_its_own_identity():
    """ "manual" and "import:..." are identities too, just not Telegram's."""
    assert source_identity_key("manual") == "manual"
    assert source_identity_key("import:2026-01-02") == "import:2026-01-02"


def test_identity_is_absent_rather_than_invented_for_nothing():
    assert source_identity_key(None) is None
    assert source_identity_key("   ") is None


def test_the_migration_carries_the_same_rule_as_the_application():
    """The copy in migration 0028 is deliberate; drift in it is not.

    A migration that imports application code produces a different
    database when that code changes, so the rule is duplicated there on
    purpose. This is the test that keeps the duplicate honest.
    """
    module: dict = {}
    source = (REPO / "alembic" / "versions" / "0028_target_source_model.py").read_text(encoding="utf-8")
    exec(compile(source, "0026", "exec"), module)  # noqa: S102 - reading our own migration

    for value in ("-1001234567890", "1234567890", "-1001001234", "1001234", "-42", "42", "manual", "", None):
        assert module["_canonical_id"](value) == canonical_id(value), value
        assert module["_identity_key"](value) == source_identity_key(value), value


def test_identity_key_is_filled_without_anyone_remembering_to(client):
    """Six call sites create channels. None of them sets this column.

    The mapper hook is what makes that safe, so removing it has to fail a
    test rather than produce silently NULL identities.
    """
    with SessionLocal() as db:
        workspace = Workspace(name="identity")
        db.add(workspace)
        db.flush()
        channel = Channel(workspace_id=workspace.id, tg_channel_id="-1009876543210", title="t")
        db.add(channel)
        db.commit()
        assert channel.identity_key == "9876543210"

        channel.tg_channel_id = "-100111222333"
        db.commit()
        assert channel.identity_key == "111222333"


# --- resource / occurrence ------------------------------------------------


def _workspace(db: Session) -> Workspace:
    workspace = Workspace(name="ws")
    db.add(workspace)
    db.flush()
    return workspace


def _channel(db: Session, workspace: Workspace, tg_id: str) -> Channel:
    channel = Channel(workspace_id=workspace.id, tg_channel_id=tg_id, title=tg_id)
    db.add(channel)
    db.flush()
    return channel


def test_one_link_seen_a_hundred_times_is_one_resource(client):
    """The headline separation, stated as arithmetic.

    The legacy schema cannot express this: ``links`` is unique per
    ``(channel_id, url_hash)``, so the same URL twice in one channel is
    rejected outright and the repeat leaves no trace at all.
    """
    with SessionLocal() as db:
        workspace = _workspace(db)
        source_a = _channel(db, workspace, "-1001")
        source_b = _channel(db, workspace, "-1002")

        resource = Resource(
            workspace_id=workspace.id,
            fingerprint="f" * 64,
            representative_url="https://t.me/example",
            platform="telegram",
        )
        db.add(resource)
        db.flush()

        for index, source in enumerate([source_a] * 99 + [source_b]):
            db.add(
                Occurrence(
                    workspace_id=workspace.id,
                    resource_id=resource.id,
                    source_id=source.id,
                    tg_message_id=index + 1,
                    extraction_method="text",
                )
            )
        db.commit()

        assert db.query(Resource).count() == 1
        assert db.query(Occurrence).filter(Occurrence.resource_id == resource.id).count() == 100
        assert db.query(Occurrence).filter(Occurrence.source_id == source_b.id).count() == 1


def test_two_resources_cannot_share_one_fingerprint(client):
    with SessionLocal() as db:
        workspace = _workspace(db)
        for _ in range(2):
            db.add(
                Resource(
                    workspace_id=workspace.id,
                    fingerprint="a" * 64,
                    representative_url="https://t.me/dup",
                    platform="telegram",
                )
            )
        with pytest.raises(IntegrityError):
            db.commit()


def test_the_same_link_in_the_same_message_is_recorded_once(client):
    with SessionLocal() as db:
        workspace = _workspace(db)
        source = _channel(db, workspace, "-1003")
        resource = Resource(
            workspace_id=workspace.id,
            fingerprint="b" * 64,
            representative_url="https://t.me/x",
            platform="telegram",
        )
        db.add(resource)
        db.flush()
        for _ in range(2):
            db.add(
                Occurrence(
                    workspace_id=workspace.id,
                    resource_id=resource.id,
                    source_id=source.id,
                    tg_message_id=7,
                    extraction_method="text",
                )
            )
        with pytest.raises(IntegrityError):
            db.commit()


def test_hand_added_links_are_not_collapsed_into_one_occurrence(client):
    """Manual and imported rows all carry message id 0.

    An unrestricted uniqueness key would make every hand-added link from
    one source the same occurrence, which is why the index is partial.
    """
    with SessionLocal() as db:
        workspace = _workspace(db)
        source = _channel(db, workspace, "manual")
        resource = Resource(
            workspace_id=workspace.id,
            fingerprint="c" * 64,
            representative_url="https://t.me/y",
            platform="telegram",
        )
        db.add(resource)
        db.flush()
        for _ in range(3):
            db.add(
                Occurrence(
                    workspace_id=workspace.id,
                    resource_id=resource.id,
                    source_id=source.id,
                    tg_message_id=0,
                    extraction_method="text",
                )
            )
        db.commit()
        assert db.query(Occurrence).count() == 3


# --- access ---------------------------------------------------------------


def test_one_source_can_be_readable_by_one_account_and_not_another(client):
    """Access is a property of the relationship, not of the source."""
    with SessionLocal() as db:
        workspace = _workspace(db)
        source = _channel(db, workspace, "-1004")
        accounts = []
        for label in ("one", "two"):
            account = TelegramAccount(workspace_id=workspace.id, label=label, session_string="x")
            db.add(account)
            db.flush()
            accounts.append(account)

        db.add(
            SourceAccess(
                workspace_id=workspace.id,
                source_id=source.id,
                account_id=accounts[0].id,
                path_kind="userbot",
                state=SourceAccess.ACCESSIBLE,
            )
        )
        db.add(
            SourceAccess(
                workspace_id=workspace.id,
                source_id=source.id,
                account_id=accounts[1].id,
                path_kind="userbot",
                state=SourceAccess.NEEDS_ACCESS,
            )
        )
        db.commit()

        states = {row.account_id: row.state for row in db.query(SourceAccess).all()}
        assert states == {accounts[0].id: "ACCESSIBLE", accounts[1].id: "NEEDS_ACCESS"}


def test_the_public_path_is_recorded_once_even_though_it_has_no_account(client):
    """NULLs do not compare equal, which a plain constraint would allow."""
    with SessionLocal() as db:
        workspace = _workspace(db)
        source = _channel(db, workspace, "-1005")
        for _ in range(2):
            db.add(
                SourceAccess(
                    workspace_id=workspace.id,
                    source_id=source.id,
                    account_id=None,
                    path_kind="public",
                    state=SourceAccess.ACCESSIBLE,
                )
            )
        with pytest.raises(IntegrityError):
            db.commit()


# --- assignment -----------------------------------------------------------


def test_a_source_has_at_most_one_open_assignment(client):
    """Two open assignments mean two writers on one watermark."""
    with SessionLocal() as db:
        workspace = _workspace(db)
        source = _channel(db, workspace, "-1006")
        accounts = []
        for label in ("one", "two"):
            account = TelegramAccount(workspace_id=workspace.id, label=label, session_string="x")
            db.add(account)
            db.flush()
            accounts.append(account)
        for account in accounts:
            db.add(SourceAssignment(workspace_id=workspace.id, source_id=source.id, account_id=account.id))
        with pytest.raises(IntegrityError):
            db.commit()


def test_a_released_assignment_leaves_room_for_the_next_one(client):
    """History accumulates; only the open row is exclusive."""
    with SessionLocal() as db:
        workspace = _workspace(db)
        source = _channel(db, workspace, "-1007")
        first = TelegramAccount(workspace_id=workspace.id, label="one", session_string="x")
        second = TelegramAccount(workspace_id=workspace.id, label="two", session_string="x")
        db.add_all([first, second])
        db.flush()

        db.add(
            SourceAssignment(
                workspace_id=workspace.id,
                source_id=source.id,
                account_id=first.id,
                released_at=datetime(2026, 1, 1),
                reason="account disabled",
            )
        )
        db.add(SourceAssignment(workspace_id=workspace.id, source_id=source.id, account_id=second.id))
        db.commit()

        assert db.query(SourceAssignment).count() == 2
        open_rows = db.query(SourceAssignment).filter(SourceAssignment.released_at.is_(None)).all()
        assert [row.account_id for row in open_rows] == [second.id]


def test_assignment_does_not_claim_collection(client):
    """The three states §11 requires, all expressible at once.

    Assigned-and-never-collected, assigned-and-access-lost, and
    assigned-and-failing are different facts. A model that cannot tell
    them apart reports a fleet as healthy while it collects nothing.
    """
    with SessionLocal() as db:
        workspace = _workspace(db)
        account = TelegramAccount(workspace_id=workspace.id, label="a", session_string="x")
        db.add(account)
        db.flush()

        never, lost, failing = (_channel(db, workspace, f"-100{n}") for n in (10, 11, 12))
        for source in (never, lost, failing):
            db.add(SourceAssignment(workspace_id=workspace.id, source_id=source.id, account_id=account.id))

        # Access lost: the assignment stands, the access row says otherwise.
        db.add(
            SourceAccess(
                workspace_id=workspace.id,
                source_id=lost.id,
                account_id=account.id,
                path_kind="userbot",
                state=SourceAccess.INACCESSIBLE,
                observed_at=datetime(2026, 2, 1),
            )
        )
        # Failing: it was reachable when last seen, and nothing has been
        # collected since.
        db.add(
            SourceAccess(
                workspace_id=workspace.id,
                source_id=failing.id,
                account_id=account.id,
                path_kind="userbot",
                state=SourceAccess.ACCESSIBLE,
                observed_at=datetime(2026, 2, 1),
            )
        )
        db.commit()

        assigned = {row.source_id for row in db.query(SourceAssignment).all()}
        assert assigned == {never.id, lost.id, failing.id}
        # None of the three has a collection to its name, and the model
        # says so without contradicting the assignment.
        assert db.query(Channel).filter(Channel.last_collected_at.isnot(None)).count() == 0
        assert db.query(SourceAccess).filter(SourceAccess.source_id == never.id).count() == 0


# --- state history --------------------------------------------------------


def test_a_source_keeps_its_identity_across_a_public_to_private_move(client):
    """Identity ≠ state, demonstrated rather than asserted in prose."""
    with SessionLocal() as db:
        workspace = _workspace(db)
        source = _channel(db, workspace, "-1008")
        identity = source.identity_key

        db.add(
            SourceEvent(
                workspace_id=workspace.id,
                source_id=source.id,
                dimension="access_state",
                previous_state="ACCESSIBLE",
                new_state="NEEDS_ACCESS",
                occurred_at=datetime(2026, 3, 1),
                reason="channel became private",
                actor="collector",
            )
        )
        db.add(
            SourceEvent(
                workspace_id=workspace.id,
                source_id=source.id,
                dimension="access_state",
                previous_state="NEEDS_ACCESS",
                new_state="ACCESSIBLE",
                occurred_at=datetime(2026, 3, 9),
                reason="joined with account 3",
                actor="operator",
            )
        )
        db.commit()

        db.refresh(source)
        assert source.identity_key == identity, "identity moved when the state did"
        history = db.query(SourceEvent).order_by(SourceEvent.occurred_at).all()
        assert [(row.previous_state, row.new_state) for row in history] == [
            ("ACCESSIBLE", "NEEDS_ACCESS"),
            ("NEEDS_ACCESS", "ACCESSIBLE"),
        ]


def test_evidence_and_audit_are_not_the_same_record(client):
    """Evidence says what the system saw; audit says what a person did.

    Kept apart so "the operator paused it" and "the account could not read
    it" never collapse into one indistinguishable row.
    """
    with SessionLocal() as db:
        workspace = _workspace(db)
        evidence = Evidence(
            workspace_id=workspace.id,
            kind="access_probe",
            observed_at=datetime(2026, 4, 1),
            summary="account 3 read 12 messages",
        )
        db.add(evidence)
        db.commit()
        assert evidence.id is not None
        assert not hasattr(evidence, "user_id"), "evidence must not carry an actor; that is audit's column"


# --- the migration itself -------------------------------------------------


# Migrations in this project run on PostgreSQL only — the chain uses ALTER
# forms SQLite does not support, which is why the test suite builds its
# schema with ``create_all`` instead. So the backfill has to be proved
# where it will actually run, and these tests skip loudly without a DSN
# rather than passing vacuously somewhere else.
MIGRATION_DSN = os.environ.get("MIGRATION_TEST_DSN", "")

pg_migration = pytest.mark.skipif(
    not MIGRATION_DSN,
    reason="MIGRATION_TEST_DSN not set — the backfill can only be proved on real Postgres",
)


def _tenant(conn, workspace_id: int = 1) -> None:
    """Name the tenant before touching a protected table.

    RLS is in force from migration 0020 and it fails closed in both
    directions: without this, an INSERT is refused and a SELECT returns
    nothing at all. Session-scoped rather than transaction-local so it
    survives the implicit commits between statements here.
    """
    conn.execute(text("SELECT set_config('app.workspace_id', :w, false)"), {"w": str(workspace_id)})


def _alembic(env: dict[str, str], *args: str) -> None:
    subprocess.run([sys.executable, "-m", "alembic", *args], cwd=REPO, env=env, check=True, capture_output=True)


@pytest.fixture
def legacy_db():
    """A database at revision 0025, seeded the way the old model wrote.

    Rewound to base first so each test starts from the same schema no
    matter what the previous one left behind — the alternative is tests
    that pass in isolation and fail in a suite.
    """
    env = {**os.environ, "DATABASE_URL": MIGRATION_DSN, "SECRET_KEY": "test-secret-key", "ENVIRONMENT": "test"}
    _alembic(env, "downgrade", "base")
    _alembic(env, "upgrade", "0025_message_identity_rules_v2")
    yield MIGRATION_DSN, env
    _alembic(env, "downgrade", "base")


def _upgrade(env: dict[str, str]) -> None:
    _alembic(env, "upgrade", "head")


@pg_migration
def test_the_migration_splits_legacy_links_into_resources_and_occurrences(legacy_db):
    """The same URL in three channels becomes one resource, three sightings.

    This is the whole reason the phase exists, so it is checked against
    what the migration actually wrote, not against the models.
    """
    url, env = legacy_db
    engine = create_engine(url)
    seen = datetime(2026, 5, 1)
    with engine.begin() as conn:
        _tenant(conn)
        conn.execute(text("INSERT INTO workspaces (id, name, created_at) VALUES (1, 'w', :t)"), {"t": seen})
        for channel_id, tg in ((1, "-1001111"), (2, "-1002222"), (3, "manual")):
            conn.execute(
                text(
                    "INSERT INTO channels (id, workspace_id, tg_channel_id, kind, source, last_message_id, "
                    "is_active, created_at) VALUES (:i, 1, :tg, 'channel', 'userbot', 0, true, :t)"
                ),
                {"i": channel_id, "tg": tg, "t": seen},
            )
        # One URL in three channels, plus a second URL in one of them.
        rows = [
            (1, 1, 10, "https://t.me/shared", "hash-shared", "text"),
            (2, 2, 20, "https://t.me/shared", "hash-shared", "hyperlink"),
            (3, 3, 0, "https://t.me/shared", "hash-shared", "text"),
            (4, 1, 11, "https://chat.whatsapp.com/abc", "hash-wa", "button"),
        ]
        for link_id, channel_id, message_id, link_url, url_hash, source_type in rows:
            conn.execute(
                text(
                    "INSERT INTO links (id, workspace_id, channel_id, message_id, url, url_hash, domain, "
                    "platform, category, confidence, classified_by, source_type, created_at, posted_at, "
                    "is_favorite, is_pinned, click_count, consecutive_failures, is_archived) "
                    "VALUES (:i, 1, :c, :m, :u, :h, 'd', 'telegram', 'other', 0.5, 'rules-v2', :s, :t, :t, "
                    "false, false, 0, 0, false)"
                ),
                {
                    "i": link_id,
                    "c": channel_id,
                    "m": message_id,
                    "u": link_url,
                    "h": url_hash,
                    "s": source_type,
                    "t": seen,
                },
            )
    engine.dispose()

    _upgrade(env)

    engine = create_engine(url)
    with engine.connect() as conn:
        _tenant(conn)
        resources = conn.execute(text("SELECT id, fingerprint, representative_url FROM resources")).fetchall()
        assert len(resources) == 2, f"expected one resource per distinct URL, got {resources}"

        shared = next(r for r in resources if r.fingerprint == "hash-shared")
        assert shared.representative_url == "https://t.me/shared"

        occurrences = conn.execute(
            text(
                "SELECT source_id, tg_message_id, extraction_method, legacy_link_id FROM occurrences "
                "WHERE resource_id = :r ORDER BY legacy_link_id"
            ),
            {"r": shared.id},
        ).fetchall()
        assert len(occurrences) == 3, "three sightings of one link became something other than three occurrences"
        assert [row.source_id for row in occurrences] == [1, 2, 3]
        assert [row.extraction_method for row in occurrences] == ["text", "hyperlink", "text"]
        assert [row.legacy_link_id for row in occurrences] == [1, 2, 3]

        # Nothing was dropped: every legacy link has exactly one occurrence.
        total = conn.execute(text("SELECT count(*) FROM occurrences")).scalar()
        legacy = conn.execute(text("SELECT count(*) FROM links")).scalar()
        assert total == legacy == 4
    engine.dispose()


@pg_migration
def test_the_migration_backfills_identity_and_acquisition_without_inventing(legacy_db):
    url, env = legacy_db
    engine = create_engine(url)
    seen = datetime(2026, 5, 1)
    with engine.begin() as conn:
        _tenant(conn)
        conn.execute(text("INSERT INTO workspaces (id, name, created_at) VALUES (1, 'w', :t)"), {"t": seen})
        conn.execute(
            text(
                "INSERT INTO telegram_accounts (id, workspace_id, label, session_string, is_active, "
                "consecutive_failures, links_collected, created_at) VALUES (1, 1, 'a', 'x', true, 0, 0, :t)"
            ),
            {"t": seen},
        )
        # A collected userbot source, an uncollected one, a public one, and
        # a synthetic bucket.
        for channel_id, tg, src, account, collected in (
            (1, "-1001111", "userbot", 1, seen),
            (2, "-1002222", "userbot", 1, None),
            (3, "-1003333", "public", None, seen),
            (4, "manual", "userbot", None, None),
        ):
            conn.execute(
                text(
                    "INSERT INTO channels (id, workspace_id, tg_channel_id, kind, source, account_id, "
                    "last_message_id, last_collected_at, is_active, created_at) "
                    "VALUES (:i, 1, :tg, 'channel', :s, :a, 0, :c, true, :t)"
                ),
                {"i": channel_id, "tg": tg, "s": src, "a": account, "c": collected, "t": seen},
            )
    engine.dispose()

    _upgrade(env)

    engine = create_engine(url)
    with engine.connect() as conn:
        _tenant(conn)
        rows = {
            r.tg_channel_id: r
            for r in conn.execute(
                text("SELECT tg_channel_id, identity_key, acquisition_method FROM channels")
            ).fetchall()
        }
        assert rows["-1001111"].identity_key == "1111"
        assert rows["manual"].identity_key == "manual"
        # The target vocabulary, normalised by 0028: 0026 wrote
        # AUTHORIZED_ACCOUNT and PUBLIC_ACQUISITION, and two names for one
        # concept is the thing worth removing.
        assert rows["-1001111"].acquisition_method == "AUTHORIZED_USER"
        assert rows["-1003333"].acquisition_method == "PUBLIC"
        assert rows["manual"].acquisition_method is None, "a synthetic bucket is not acquired from anywhere"

        # Access is recorded only where a collection actually happened.
        access = conn.execute(
            text(
                "SELECT source_id, account_id, path_kind, state, observed_at FROM source_access ORDER BY source_id"
            )
        ).fetchall()
        assert [row.source_id for row in access] == [1, 3], (
            "access was invented for a source nobody has ever collected"
        )
        assert access[0].account_id == 1 and access[0].path_kind == "userbot"
        assert access[1].account_id is None and access[1].path_kind == "public"
        assert all(row.state == "ACCESSIBLE" for row in access)

        # Every access row cites the observation it was inferred from.
        cited = conn.execute(
            text(
                "SELECT count(*) FROM source_access sa JOIN evidence e ON e.id = sa.evidence_id "
                "WHERE e.kind = 'collection_history'"
            )
        ).scalar()
        assert cited == 2

        # Assignments come from the scalar column, with no invented time.
        assignments = conn.execute(
            text("SELECT source_id, account_id, assigned_at, released_at FROM source_assignments")
        ).fetchall()
        assert {row.source_id for row in assignments} == {1, 2}
        assert all(row.assigned_at is None for row in assignments), "a timestamp was invented for a migrated row"
        assert all(row.released_at is None for row in assignments)

        # History is empty, because none of it was ever recorded.
        assert conn.execute(text("SELECT count(*) FROM source_events")).scalar() == 0
    engine.dispose()


@pg_migration
def test_the_migration_is_reversible_without_touching_legacy_rows(legacy_db):
    """Downgrade drops only what upgrade created."""
    url, env = legacy_db
    engine = create_engine(url)
    seen = datetime(2026, 5, 1) - timedelta(days=1)
    with engine.begin() as conn:
        _tenant(conn)
        conn.execute(text("INSERT INTO workspaces (id, name, created_at) VALUES (1, 'w', :t)"), {"t": seen})
        conn.execute(
            text(
                "INSERT INTO channels (id, workspace_id, tg_channel_id, kind, source, last_message_id, "
                "is_active, created_at) VALUES (1, 1, '-1001111', 'channel', 'userbot', 0, true, :t)"
            ),
            {"t": seen},
        )
        conn.execute(
            text(
                "INSERT INTO links (id, workspace_id, channel_id, message_id, url, url_hash, domain, platform, "
                "category, confidence, classified_by, source_type, created_at, is_favorite, is_pinned, "
                "click_count, consecutive_failures, is_archived) "
                "VALUES (1, 1, 1, 5, 'https://t.me/a', 'h1', 'd', 'telegram', 'other', 0.5, 'rules-v2', "
                "'text', :t, false, false, 0, 0, false)"
            ),
            {"t": seen},
        )
    engine.dispose()

    _upgrade(env)
    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0025_message_identity_rules_v2"],
        cwd=REPO,
        env=env,
        check=True,
        capture_output=True,
    )

    engine = create_engine(url)
    with engine.connect() as conn:
        _tenant(conn)
        assert conn.execute(text("SELECT count(*) FROM links")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM channels")).scalar() == 1
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
            ).fetchall()
        }
        assert not tables & {"resources", "occurrences", "source_access", "source_assignments", "source_events"}
    engine.dispose()


# --- the collision report that gates the unique constraint ----------------


def test_the_identity_report_finds_a_dialog_stored_twice(client):
    """Two spellings, two rows, one dialog — which is the whole problem.

    ``get_or_create_channel`` compares the raw string, so this state is
    reachable today through manual entry and both importers. The constraint
    that would prevent it cannot be added until existing deployments are
    known to be clean, and this is what knows that.
    """
    from scripts.check_source_identity import find_collisions

    with SessionLocal() as db:
        workspace = _workspace(db)
        db.add(Channel(workspace_id=workspace.id, tg_channel_id="-1001234567890", title="as telethon wrote it"))
        db.add(Channel(workspace_id=workspace.id, tg_channel_id="1234567890", title="as a person typed it"))
        db.add(Channel(workspace_id=workspace.id, tg_channel_id="-1009999999999", title="unrelated"))
        db.commit()

        collisions = find_collisions(db, workspace.id)
        assert collisions == [("1234567890", 2)], collisions


def test_the_identity_report_is_quiet_when_there_is_nothing_to_say(client):
    from scripts.check_source_identity import find_collisions

    with SessionLocal() as db:
        workspace = _workspace(db)
        db.add(Channel(workspace_id=workspace.id, tg_channel_id="-1001111111111", title="a"))
        db.add(Channel(workspace_id=workspace.id, tg_channel_id="-1002222222222", title="b"))
        db.commit()
        assert find_collisions(db, workspace.id) == []
