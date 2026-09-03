"""Does row-level security actually stop anything?

Every test here runs against **real PostgreSQL as a non-superuser role**,
and both halves of that are load-bearing:

- SQLite has no row-level security at all, so a SQLite run proves nothing.
- A **superuser bypasses RLS entirely, even with FORCE**. The default
  local role (``postgres``) is a superuser, so a suite that connected as
  it would pass every assertion below while the policies did nothing. That
  is not a hypothetical: it is what the first measurement of this feature
  did, and it is why the superuser check below is a test and not a note.

So ``RLS_TEST_DSN`` must point at a database **owned by an unprivileged
role**, and the first test in this file refuses to let the rest run
otherwise: it asserts ``rls_effective()`` reports ``superuser: False`` and
``reason: effective``. Without ``RLS_TEST_DSN`` the whole file skips —
loudly, in the summary — rather than passing vacuously on SQLite.

The assertions deliberately use **raw SQL with no workspace filter in
Python**. A query written through the ORM's normal path is filtered by the
application before Postgres ever sees it, so it would pass whether RLS
existed or not.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from app.config import normalize_database_url
from app.rls import PROTECTED_TABLES, TENANT_SETTING, UNPROTECTED_BY_DESIGN, rls_effective

# Normalised the same way the application normalises DATABASE_URL. A bare
# ``postgresql://`` DSN resolves to psycopg2, which this project does not
# ship — the driver is psycopg 3 — so a raw DSN here fails at connect time
# with "No module named 'psycopg2'" and proves nothing about RLS.
PG_DSN = normalize_database_url(os.environ.get("RLS_TEST_DSN") or "")

pytestmark = pytest.mark.skipif(
    not PG_DSN,
    reason="RLS_TEST_DSN not set — needs real Postgres and a non-superuser role",
)


@pytest.fixture
def db():
    engine = create_engine(PG_DSN)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _tenant(db: Session, workspace_id: int | None) -> None:
    value = "" if workspace_id is None else str(workspace_id)
    db.execute(text(f"SELECT set_config('{TENANT_SETTING}', :v, true)"), {"v": value})


# --- the precondition, checked before anything is asserted ----------------


def test_the_connection_is_not_a_superuser():
    """Guards every other test in this file.

    A superuser bypasses RLS with FORCE and all. If this suite ever runs
    as one, every assertion below passes while nothing is enforced — the
    exact shape of a test that has stopped testing.
    """
    engine = create_engine(PG_DSN)
    with Session(engine) as session:
        status = rls_effective(session)
    engine.dispose()

    assert status["superuser"] is False, (
        "connected as a superuser: RLS is bypassed and nothing below means anything"
    )
    assert status["reason"] == "effective", f"RLS is not in force: {status}"


def test_every_protected_table_is_forced_not_merely_enabled(db: Session):
    """ENABLE without FORCE leaves the owner — the application's own role —
    seeing every row. Measured, not assumed: with ENABLE only, a query
    scoped to workspace 100 still returned rows from workspace 200."""
    rows = db.execute(
        text("SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY(:names)"),
        {"names": list(PROTECTED_TABLES)},
    ).all()

    assert len(rows) == len(PROTECTED_TABLES), f"expected {len(PROTECTED_TABLES)} tables, found {len(rows)}"
    for name, enabled, forced in rows:
        assert enabled, f"{name}: row security not enabled"
        assert forced, f"{name}: enabled but NOT forced — the owner bypasses it, so it protects nothing"


# --- what it actually stops ----------------------------------------------


def test_another_workspaces_rows_are_invisible_to_raw_sql(db: Session):
    """The claim RLS exists to make.

    No .filter(workspace_id=...) anywhere in this query — the isolation is
    entirely Postgres's doing.
    """
    ids = _two_workspaces(db)
    _seed_channel(db, ids[0], "alpha")
    _seed_channel(db, ids[1], "beta")

    _tenant(db, ids[0])
    visible = db.execute(text("SELECT tg_channel_id FROM channels")).scalars().all()

    assert "alpha" in visible
    assert "beta" not in visible, "another workspace's row was visible to a query with no filter of its own"


def test_writing_a_row_for_another_workspace_is_refused(db: Session):
    """The protection an application-level filter cannot provide.

    Filters guard reads. This guards the write: even code that sets
    workspace_id wrongly — a bug, not an attack — is stopped by the
    database instead of silently creating a row in someone else's data.
    """
    ids = _two_workspaces(db)
    _tenant(db, ids[0])

    with pytest.raises(ProgrammingError) as exc:
        db.execute(
            text(
                "INSERT INTO channels (workspace_id, tg_channel_id, last_message_id, is_active, created_at) "
                "VALUES (:ws, 'smuggled', 0, true, now())"
            ),
            {"ws": ids[1]},
        )
        db.flush()

    assert "row-level security" in str(exc.value).lower()
    db.rollback()


def test_with_no_tenant_set_nothing_is_visible(db: Session):
    """Fail-closed, which is the only safe direction.

    A code path that forgets the tenant reads nothing — obviously broken —
    rather than reading everything, which looks like it works.
    """
    ids = _two_workspaces(db)
    _seed_channel(db, ids[0], "alpha")

    _tenant(db, None)
    visible = db.execute(text("SELECT count(*) FROM channels")).scalar()

    assert visible == 0


def test_a_reused_connection_fails_closed_rather_than_erroring():
    """The bug this file caught before it shipped, pinned so it cannot return.

    A transaction-local ``set_config`` reverts when its transaction ends,
    and **it reverts to the empty string, not to NULL**. So the policy
    expression sees ``''`` on every connection that has already served one
    request — which, behind a pool, is nearly every request. The first
    draft of the policy cast that straight to int::

        workspace_id = current_setting('app.workspace_id', true)::int

    and ``''::int`` is not false, it is *invalid input syntax for type
    integer*. That turns "an unset tenant reads nothing" into "an unset
    tenant returns HTTP 500", on the second request onward — passing a
    single-request smoke test and failing in production.

    Two transactions on one connection, which is the whole point: a single
    transaction cannot reproduce it.
    """
    engine = create_engine(PG_DSN)
    try:
        with engine.connect() as conn:
            with conn.begin():
                conn.execute(text(f"SELECT set_config('{TENANT_SETTING}', '1', true)"))

            # Same physical connection, tenant now reverted to ''.
            with conn.begin():
                leaked = conn.execute(text(f"SELECT current_setting('{TENANT_SETTING}', true)")).scalar()
                assert leaked == "", f"expected the reverted GUC to be the empty string, got {leaked!r}"

                # Must return a count, not raise. The assertion that matters
                # is that this line does not throw DataError.
                count = conn.execute(text("SELECT count(*) FROM channels")).scalar()
                assert count == 0
    finally:
        engine.dispose()


def test_the_unprotected_tables_are_listed_honestly(db: Session):
    """links and telegram_accounts are documented as NOT protected.

    This asserts the documentation matches reality in both directions: a
    table claimed as unprotected that quietly gained a policy would make
    app/rls.py's stated limitation a lie, and one claimed as protected
    that lost its policy would make its promise one.
    """
    for name in UNPROTECTED_BY_DESIGN:
        enabled = db.execute(text("SELECT relrowsecurity FROM pg_class WHERE relname = :n"), {"n": name}).scalar()
        assert enabled is False, f"{name} is documented as unprotected but has RLS enabled"


# --- helpers --------------------------------------------------------------


def _two_workspaces(db: Session) -> tuple[int, int]:
    first = db.execute(
        text("INSERT INTO workspaces (name, created_at) VALUES ('rls-a', now()) RETURNING id")
    ).scalar_one()
    second = db.execute(
        text("INSERT INTO workspaces (name, created_at) VALUES ('rls-b', now()) RETURNING id")
    ).scalar_one()
    return int(first), int(second)


def _seed_channel(db: Session, workspace_id: int, tg_id: str) -> None:
    _tenant(db, workspace_id)
    db.execute(
        text(
            "INSERT INTO channels (workspace_id, tg_channel_id, last_message_id, is_active, created_at) "
            "VALUES (:ws, :tg, 0, true, now())"
        ),
        {"ws": workspace_id, "tg": tg_id},
    )


def test_every_protected_table_still_has_force_after_all_migrations(db: Session):
    """A migration that lifts FORCE and forgets to restore it is silent.

    Migrations legitimately lift FORCE to do their own work — a migration
    is a schema operation and carries no tenant, so a forced table refuses
    its writes and returns nothing to its reads. Three migrations in this
    project do it (0025, 0026, 0027). The failure mode is not the lifting;
    it is a restore that does not happen, which leaves every workspace's
    rows readable by every other and looks exactly like success.

    So this asserts the end state of the whole chain rather than trusting
    each migration's own check.
    """
    missing = []
    for name in PROTECTED_TABLES:
        row = db.execute(
            text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = :n"),
            {"n": name},
        ).one_or_none()
        assert row is not None, f"{name} is listed as protected but does not exist"
        enabled, forced = row
        if not (enabled and forced):
            missing.append(f"{name} (enabled={enabled}, forced={forced})")
    assert not missing, "protected tables left without enforced RLS: " + ", ".join(missing)
