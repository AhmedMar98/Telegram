"""Regression guard for hosted-Postgres URL handling.

Getting this wrong is not a subtle bug: the service crashes on its first
boot after deploy, before serving a single request, with an error that
points at a missing driver rather than at the URL scheme.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import NoSuchModuleError

from app.config import Settings, normalize_database_url

RENDER_STYLE = "postgres://user:pass@dpg-abc123.oregon-postgres.render.com/mydb"


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        # Managed providers hand out the legacy scheme; SQLAlchemy 2 removed it.
        (RENDER_STYLE, "postgresql+psycopg://user:pass@dpg-abc123.oregon-postgres.render.com/mydb"),
        # Correct scheme, but the default driver would be psycopg2 (not installed).
        ("postgresql://user:pass@host/db", "postgresql+psycopg://user:pass@host/db"),
        # Query parameters such as sslmode must survive untouched.
        ("postgresql://u:p@host/db?sslmode=require", "postgresql+psycopg://u:p@host/db?sslmode=require"),
        # An explicit driver is already correct and must not be rewritten twice.
        ("postgresql+psycopg://u:p@host/db", "postgresql+psycopg://u:p@host/db"),
        # SQLite is untouched.
        ("sqlite:///./local.db", "sqlite:///./local.db"),
        ("sqlite:///:memory:", "sqlite:///:memory:"),
    ],
)
def test_urls_are_normalized_to_the_installed_driver(given: str, expected: str):
    assert normalize_database_url(given) == expected


def test_normalization_is_idempotent():
    once = normalize_database_url(RENDER_STYLE)
    assert normalize_database_url(once) == once


@pytest.mark.parametrize(
    ("given", "raw_error"),
    [
        # "postgres" is not a dialect SQLAlchemy 2 knows about at all.
        (RENDER_STYLE, NoSuchModuleError),
        # Valid dialect, but it resolves to psycopg2, which is not installed.
        ("postgresql://user:pass@host/db", ModuleNotFoundError),
    ],
)
def test_normalized_urls_resolve_to_an_installed_dialect(given: str, raw_error: type[Exception]):
    """The raw forms fail to load a driver; the normalized form must not."""
    with pytest.raises(raw_error):
        create_engine(given)
    create_engine(normalize_database_url(given))  # must not raise


def test_settings_normalizes_on_load(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", RENDER_STYLE)
    monkeypatch.setenv("SECRET_KEY", "test-secret-value-long-enough")
    assert Settings().database_url.startswith("postgresql+psycopg://")
