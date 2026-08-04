"""Shared pytest fixtures."""
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Force test mode: mock collector, no real Telegram/LLM
os.environ.setdefault("TG_MOCK_MODE", "true")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_DEBUG", "true")
# Use in-memory SQLite for tests
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SQLITE_PATH", ":memory:")

import pytest


@pytest.fixture(scope="session")
def settings():
    from app.config import get_settings
    return get_settings()


@pytest.fixture
def db_session():
    """Fresh in-memory SQLite session per test."""
    from app.database import SessionLocal, engine, init_db
    from app.models import Base
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
