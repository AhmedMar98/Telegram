"""SQLAlchemy engine/session wiring.

Supports Postgres (production, Render free tier) and SQLite (local dev and
the test suite) through the same code path. An in-memory SQLite URL uses a
``StaticPool`` so every connection — including the ones FastAPI's threaded
test client opens from a worker thread — shares the same in-memory
database instead of each getting its own empty one.
"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
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


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
