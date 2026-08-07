"""
SQLAlchemy database engine + session factory.

Uses SQLite by default (no Docker). sqlite-vec extension is loaded
for semantic vector search. FTS5 (full-text search) is bundled with
Python's sqlite3 module.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from .config import DATA_DIR, get_settings


def _sqlite_pragma_on_connect(dbapi_conn, _record):
    """Enable WAL mode, foreign keys, and sane busy timeout."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.execute("PRAGMA busy_timeout=5000;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.close()


def _load_sqlite_vec(dbapi_conn, _record):
    """Load sqlite-vec extension for vector search."""
    try:
        import sqlite_vec

        dbapi_conn.enable_load_extension(True)
        sqlite_vec.load(dbapi_conn)
        dbapi_conn.enable_load_extension(False)
    except ImportError:
        # sqlite-vec not installed — semantic search will gracefully degrade
        pass


def _make_engine():
    settings = get_settings()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    url = settings.database_url

    # Normalize relative SQLite paths to absolute (resolve against cwd, not DATA_DIR)
    if url.startswith("sqlite:///./"):
        rel = url[len("sqlite:///./") :]
        abs_path = (Path.cwd() / rel).resolve()
        # Ensure parent dir exists
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{abs_path}"
    elif url == "sqlite:///:memory:":
        pass  # in-memory, no path normalization

    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, echo=False, future=True, connect_args=connect_args)

    if url.startswith("sqlite"):
        event.listen(engine, "connect", _load_sqlite_vec)
        event.listen(engine, "connect", _sqlite_pragma_on_connect)

    return engine


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db():
    """FastAPI dependency: yields a session and ensures it's closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables (used by scripts/init_db.py)."""
    from sqlalchemy.orm import registry

    from . import models  # noqa: F401 — register models

    # Ensure all models are mapped
    registry().configure()
    models.Base.metadata.create_all(bind=engine)
