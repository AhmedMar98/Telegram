"""Tests for production_secrets_check() and the lifespan check that uses it.

Both published defaults in app/config.py (SECRET_KEY, FIELD_ENCRYPTION_KEY)
are committed to this public repository. Running production with either one
is not weak security, it is no security at all: a session cookie anyone can
forge, or field encryption anyone can reverse. This used to be a comment
("MUST be overridden") that nothing enforced; this test file is what turns
it into a contract app/main.py's lifespan actually keeps.
"""

from __future__ import annotations

from app.config import _PUBLISHED_DEFAULTS, Settings, production_secrets_check


def test_production_with_both_defaults_reports_both_names():
    settings = Settings(
        environment="production",
        secret_key=_PUBLISHED_DEFAULTS["SECRET_KEY"],
        field_encryption_key=_PUBLISHED_DEFAULTS["FIELD_ENCRYPTION_KEY"],
    )

    assert set(production_secrets_check(settings)) == {"SECRET_KEY", "FIELD_ENCRYPTION_KEY"}


def test_production_with_only_secret_key_default_reports_just_that():
    settings = Settings(
        environment="production",
        secret_key=_PUBLISHED_DEFAULTS["SECRET_KEY"],
        field_encryption_key="a-real-fernet-key-not-the-default=",
    )

    assert production_secrets_check(settings) == ["SECRET_KEY"]


def test_production_with_only_field_encryption_default_reports_just_that():
    settings = Settings(
        environment="production",
        secret_key="a-real-long-random-secret-key",
        field_encryption_key=_PUBLISHED_DEFAULTS["FIELD_ENCRYPTION_KEY"],
    )

    assert production_secrets_check(settings) == ["FIELD_ENCRYPTION_KEY"]


def test_production_with_both_overridden_reports_nothing():
    settings = Settings(
        environment="production",
        secret_key="a-real-long-random-secret-key",
        field_encryption_key="a-real-fernet-key-not-the-default=",
    )

    assert production_secrets_check(settings) == []


def test_development_never_reports_anything():
    """Development legitimately runs on the published defaults — that is
    what makes `uvicorn app.main:app` work with no .env file on a fresh
    checkout, and the whole test suite depends on it. Without this
    environment gate, every test in the repo would fail this check."""
    settings = Settings(
        environment="development",
        secret_key=_PUBLISHED_DEFAULTS["SECRET_KEY"],
        field_encryption_key=_PUBLISHED_DEFAULTS["FIELD_ENCRYPTION_KEY"],
    )

    assert production_secrets_check(settings) == []


def test_empty_field_encryption_key_still_resolves_to_the_default():
    """field_encryption_key's own validator maps "" to the published
    default (an unset GitHub Actions secret exports an empty string), so
    an empty override is exactly as unsafe as an explicit default and must
    be caught the same way."""
    settings = Settings(
        environment="production", secret_key="a-real-long-random-secret-key", field_encryption_key=""
    )

    assert "FIELD_ENCRYPTION_KEY" in production_secrets_check(settings)


def test_lifespan_refuses_to_start_with_published_defaults_in_production(monkeypatch):
    """The behaviour app/main.py actually has, not just what the pure
    function returns: importing and running the lifespan body with both
    secrets left at their defaults must raise, not log and continue."""
    import asyncio

    from fastapi import FastAPI

    from app.config import get_settings
    from app.main import lifespan

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", _PUBLISHED_DEFAULTS["SECRET_KEY"])
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", _PUBLISHED_DEFAULTS["FIELD_ENCRYPTION_KEY"])
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    get_settings.cache_clear()
    try:
        dummy = FastAPI()

        async def _enter() -> None:
            async with lifespan(dummy):
                pass

        try:
            asyncio.run(_enter())
            raise AssertionError("lifespan should have refused to start")
        except RuntimeError as exc:
            assert "SECRET_KEY" in str(exc)
            assert "FIELD_ENCRYPTION_KEY" in str(exc)
    finally:
        get_settings.cache_clear()
