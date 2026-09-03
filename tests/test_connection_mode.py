"""Connecting through a pooler, and the one setting that survives it.

A transaction-mode pooler hands each transaction a different backend, so
a prepared statement made on one is gone on the next. psycopg 3 prepares
automatically after a query has been seen a few times, which means the
failure does not show up on the first request — it shows up later, under
load, as ``prepared statement does not exist``, and it reads like a
database fault rather than a connection-mode choice.

The right answer is a session-mode endpoint for a runtime that holds
long-lived connections. ``db_disable_prepared_statements`` exists for the
deployment that has no such endpoint, and these tests pin what it does
so it cannot quietly stop doing it.
"""

from __future__ import annotations

import os

import pytest

from app import database
from app.config import Settings, get_settings
from app.database import _make_engine

POSTGRES_URL = "postgresql+psycopg://user:pw@example.invalid:5432/db"


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _connect_args(monkeypatch, url: str = POSTGRES_URL, **env) -> dict:
    """What ``_make_engine`` actually hands to the driver.

    Captured at the ``create_engine`` boundary rather than read back off
    the built engine: SQLAlchemy merges ``connect_args`` inside its own
    connect closure, so an assembled engine does not expose them and a
    test that inspected ``create_connect_args`` would be asserting only
    the parts parsed out of the URL — which is how this test first passed
    while proving nothing.
    """
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()

    captured: dict = {}

    def _spy(*args, **kwargs):
        captured.update(kwargs.get("connect_args", {}))
        raise _Captured

    class _Captured(Exception):
        pass

    monkeypatch.setattr(database, "create_engine", _spy)
    with pytest.raises(_Captured):
        _make_engine(url)
    return captured


def test_prepared_statements_are_left_alone_by_default(monkeypatch):
    """The default must not change for every existing deployment."""
    assert Settings().db_disable_prepared_statements is False
    args = _connect_args(monkeypatch)
    assert "prepare_threshold" not in args


def test_the_switch_disables_them_with_none_not_zero(monkeypatch):
    """``0`` means "prepare immediately"; ``None`` means "never".

    Getting this backwards would make the setting prepare *more*
    aggressively while appearing to solve the problem — the failure would
    look identical and arrive sooner.
    """
    args = _connect_args(monkeypatch, DB_DISABLE_PREPARED_STATEMENTS="true")
    assert "prepare_threshold" in args
    assert args["prepare_threshold"] is None


def test_sqlite_is_untouched_by_the_setting(monkeypatch):
    """The knob is a psycopg concept and must not reach the dev database."""
    args = _connect_args(monkeypatch, url="sqlite:///./x.db", DB_DISABLE_PREPARED_STATEMENTS="true")
    assert "prepare_threshold" not in args


PG_DSN = os.environ.get("PG_TEST_DSN")


@pytest.mark.skipif(not PG_DSN, reason="PG_TEST_DSN not set — this measures a real server's prepared statements")
def test_against_a_real_server_the_switch_actually_removes_them():
    """The measurement, not the intention.

    Without the setting psycopg leaves a prepared statement behind after
    it has seen the query enough times; with it, none. That difference is
    the entire reason the setting exists, and asserting the connect_args
    alone would not have caught psycopg changing what the value means.
    """
    from sqlalchemy import create_engine, text

    def prepared_after_repeats(**connect_args) -> int:
        engine = create_engine(PG_DSN, connect_args=connect_args)
        try:
            with engine.connect() as conn:
                for _ in range(7):
                    conn.execute(text("SELECT 1 WHERE :x = 1"), {"x": 1})
                return conn.execute(text("SELECT count(*) FROM pg_prepared_statements")).scalar()
        finally:
            engine.dispose()

    assert prepared_after_repeats() > 0, "psycopg no longer prepares; the setting may be obsolete"
    assert prepared_after_repeats(prepare_threshold=None) == 0
