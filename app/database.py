"""SQLAlchemy engine/session wiring.

Supports Postgres (production, Render free tier) and SQLite (local dev and
the test suite) through the same code path. An in-memory SQLite URL uses a
``StaticPool`` so every connection — including the ones FastAPI's threaded
test client opens from a worker thread — shares the same in-memory
database instead of each getting its own empty one.
"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine(url: str):
    engine_kwargs: dict = {"pool_pre_ping": True}
    connect_args: dict = {}

    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        if url == "sqlite:///:memory:":
            from sqlalchemy.pool import StaticPool

            engine_kwargs["poolclass"] = StaticPool

    return create_engine(url, connect_args=connect_args, **engine_kwargs)


engine = _make_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


@event.listens_for(Session, "after_begin")
def _apply_tenant_scope(session: Session, transaction, connection) -> None:
    """Re-issue the row-level-security tenant on every new transaction.

    ``SET LOCAL`` lives and dies with its transaction — which is the
    property that makes it safe on a pooled connection, and the reason it
    cannot simply be issued once when the session opens. Several request
    paths commit half way through and keep querying afterwards; that
    commit ends the transaction, and everything after it would run with no
    tenant set. Under RLS that means reads silently returning nothing and
    writes erroring outright.

    Hooking ``after_begin`` covers every transaction the session opens,
    including the one that starts after a mid-request commit, without
    auditing each path by hand.

    Sessions with no ``workspace_id`` in ``info`` are left alone. That is
    not an oversight: login, API-key resolution and the bot's chat lookup
    all read *before* any tenant is known, which is exactly why those
    tables are excluded from RLS (see app/rls.py).
    """
    workspace_id = session.info.get("workspace_id")
    if workspace_id is None or connection.dialect.name != "postgresql":
        return
    from sqlalchemy import text as _text

    from app.rls import TENANT_SETTING

    # set_config(..., true) is the parameterisable form of SET LOCAL —
    # SET itself rejects bind parameters outright ("syntax error at or
    # near $1"), so this is the only form that keeps the value out of the
    # SQL string.
    connection.execute(
        _text(f"SELECT set_config('{TENANT_SETTING}', :value, true)"), {"value": str(int(workspace_id))}
    )


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
