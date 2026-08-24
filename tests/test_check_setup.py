"""Tests for the setup diagnostic.

The diagnostic exists to be trusted when someone is stuck, so its verdicts
are worth testing: a false "all clear" would be worse than no check at all.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.classifier import llm
from scripts import check_setup


@pytest.fixture(autouse=True)
def _clear_results():
    check_setup.results.clear()
    yield
    check_setup.results.clear()


def _statuses() -> dict[str, str]:
    return {check: status for status, check, _ in check_setup.results}


def _details() -> dict[str, str]:
    return {check: detail for _, check, detail in check_setup.results}


def test_missing_core_env_is_a_failure(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)

    assert check_setup.check_core_env() is False
    assert _statuses()["DATABASE_URL"] == check_setup.FAIL
    assert _statuses()["SECRET_KEY"] == check_setup.FAIL


def test_default_secret_key_is_flagged(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("SECRET_KEY", "dev-secret")

    check_setup.check_core_env()
    assert _statuses()["SECRET_KEY"] == check_setup.WARN


def test_strong_secret_key_passes(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("SECRET_KEY", "S3rHqQ2v8xLpN4kZ7wYmB1tJ")

    assert check_setup.check_core_env() is True
    assert _statuses()["SECRET_KEY"] == check_setup.OK


def test_partial_telegram_credentials_fail(monkeypatch):
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.delenv("TG_API_HASH", raising=False)
    monkeypatch.delenv("TG_SESSION_STRING", raising=False)

    assert check_setup.check_telegram_credentials() is False
    assert _statuses()["telegram"] == check_setup.FAIL


def test_absent_telegram_credentials_are_only_a_warning(monkeypatch):
    for name in ("TG_API_ID", "TG_API_HASH", "TG_SESSION_STRING"):
        monkeypatch.delenv(name, raising=False)

    assert check_setup.check_telegram_credentials() is True
    assert _statuses()["telegram"] == check_setup.WARN


def test_truncated_session_string_is_rejected(monkeypatch):
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_SESSION_STRING", "too-short")

    assert check_setup.check_telegram_credentials() is False
    assert _statuses()["TG_SESSION_STRING"] == check_setup.FAIL


def test_non_numeric_api_id_is_rejected(monkeypatch):
    monkeypatch.setenv("TG_API_ID", "not-a-number")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_SESSION_STRING", "x" * 150)

    assert check_setup.check_telegram_credentials() is False
    assert _statuses()["TG_API_ID"] == check_setup.FAIL


def test_valid_telegram_credentials_pass(monkeypatch):
    monkeypatch.setenv("TG_API_ID", "12345")
    monkeypatch.setenv("TG_API_HASH", "abcdef0123456789")
    monkeypatch.setenv("TG_SESSION_STRING", "x" * 150)

    assert check_setup.check_telegram_credentials() is True
    assert _statuses()["telegram"] == check_setup.OK


def test_plain_http_webhook_url_is_rejected(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://insecure.example")
    monkeypatch.setenv("BOT_WEBHOOK_SECRET", "secret")

    check_setup.check_optional_features()
    assert _statuses()["bot"] == check_setup.FAIL


def test_bot_token_without_public_url_warns(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:ABC")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("BOT_WEBHOOK_SECRET", raising=False)

    check_setup.check_optional_features()
    assert _statuses()["bot"] == check_setup.WARN


def test_fully_configured_bot_passes(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.onrender.com")
    monkeypatch.setenv("BOT_WEBHOOK_SECRET", "a-webhook-secret-well-past-the-weak-length-floor")

    check_setup.check_optional_features()
    assert _statuses()["bot"] == check_setup.OK


def test_short_webhook_secret_is_warned_about_in_development(monkeypatch):
    """A short BOT_WEBHOOK_SECRET is a guessable bearer credential — this
    used to pass silently as "bot": OK regardless of length."""
    monkeypatch.setenv("BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.onrender.com")
    monkeypatch.setenv("BOT_WEBHOOK_SECRET", "secret")

    check_setup.check_optional_features()

    assert _statuses()["webhook secret"] == check_setup.WARN
    assert "bot" not in _statuses()


def test_short_webhook_secret_fails_in_production(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.onrender.com")
    monkeypatch.setenv("BOT_WEBHOOK_SECRET", "secret")

    check_setup.check_optional_features()

    assert _statuses()["webhook secret"] == check_setup.FAIL


def test_ungated_registration_is_warned_about(monkeypatch):
    monkeypatch.delenv("INVITE_CODE", raising=False)

    check_setup.check_optional_features()
    assert _statuses()["registration"] == check_setup.WARN


def test_guessable_invite_code_is_warned_about(monkeypatch):
    """INVITE_CODE="invite" gates nothing — anyone who tries the obvious
    value gets in, same as no code at all."""
    monkeypatch.setenv("INVITE_CODE", "invite")

    check_setup.check_optional_features()

    assert _statuses()["registration"] == check_setup.WARN
    assert "guessable" in _details()["registration"]


def test_short_invite_code_is_warned_about(monkeypatch):
    monkeypatch.setenv("INVITE_CODE", "abc123")

    check_setup.check_optional_features()

    assert _statuses()["registration"] == check_setup.WARN


def test_strong_invite_code_passes(monkeypatch):
    monkeypatch.setenv("INVITE_CODE", "a-long-random-invite-string-2026")

    check_setup.check_optional_features()

    assert _statuses()["registration"] == check_setup.OK


def test_missing_field_encryption_key_is_warned_about(monkeypatch):
    monkeypatch.delenv("FIELD_ENCRYPTION_KEY", raising=False)

    check_setup.check_optional_features()
    assert _statuses()["field encryption"] == check_setup.WARN


def test_default_field_encryption_key_fails_in_production(monkeypatch):
    """Upgraded from WARN to FAIL in production: app/main.py's lifespan
    will refuse to start on this value, so this diagnostic must not pass
    what the runtime rejects."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", check_setup._DEFAULT_FIELD_ENCRYPTION_KEY)

    check_setup.check_optional_features()

    assert _statuses()["field encryption"] == check_setup.FAIL
    assert "lifespan will refuse to start" in _details()["field encryption"]


def test_default_field_encryption_key_is_warned_about(monkeypatch):
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", check_setup._DEFAULT_FIELD_ENCRYPTION_KEY)

    check_setup.check_optional_features()
    assert _statuses()["field encryption"] == check_setup.WARN


def test_real_field_encryption_key_passes(monkeypatch):
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", "a-real-secret-generated-with-fernet-generate-key")

    check_setup.check_optional_features()
    assert _statuses()["field encryption"] == check_setup.OK


def test_a_revoked_groq_key_is_reported_rather_than_passed(monkeypatch):
    """The failure this check exists for. The key is present in the
    environment and completely dead — reporting "set" would hide that."""
    llm.reset_probe_cache()
    monkeypatch.setattr(llm, "get_settings", lambda: SimpleNamespace(groq_api_key="revoked"))
    monkeypatch.setattr(llm.httpx, "get", lambda *a, **k: httpx.Response(401))

    check_setup.check_optional_features()

    assert _statuses()["llm tier"] == check_setup.WARN
    assert "rejected by Groq" in _details()["llm tier"]


def test_a_working_groq_key_passes(monkeypatch):
    llm.reset_probe_cache()
    monkeypatch.setattr(llm, "get_settings", lambda: SimpleNamespace(groq_api_key="live"))
    monkeypatch.setattr(llm.httpx, "get", lambda *a, **k: httpx.Response(200))

    check_setup.check_optional_features()

    assert _statuses()["llm tier"] == check_setup.OK


def test_a_dead_llm_tier_never_fails_the_whole_check(monkeypatch):
    """The tier is optional by design, so an outage there must not make the
    script report the deployment as broken."""
    llm.reset_probe_cache()
    monkeypatch.setattr(llm, "get_settings", lambda: SimpleNamespace(groq_api_key="live"))
    monkeypatch.setattr(llm.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(llm.httpx.ConnectError("x")))

    check_setup.check_optional_features()

    assert _statuses()["llm tier"] != check_setup.FAIL


def test_https_without_production_environment_is_a_failure(monkeypatch):
    """The silent downgrade this check exists for: TLS in front, and a
    session cookie marked as safe to send over plain HTTP behind it."""
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://link-intel-web.onrender.com")

    check_setup.check_optional_features()

    assert _statuses()["session cookie"] == check_setup.FAIL
    assert "WITHOUT Secure" in _details()["session cookie"]


def test_production_environment_reports_the_real_flags(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")

    check_setup.check_optional_features()

    assert _statuses()["session cookie"] == check_setup.OK


def test_local_http_development_is_only_a_warning(monkeypatch):
    """Secure would break plain-http local development, so its absence
    there is correct rather than a finding."""
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000")

    check_setup.check_optional_features()

    assert _statuses()["session cookie"] == check_setup.WARN


# === DATABASE_URL scheme ====================================================


def test_sqlite_database_url_fails_in_production(monkeypatch):
    """The file lives on Render's ephemeral filesystem — a redeploy loses
    it entirely. In production that is data loss, not a deployment."""
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./local.db")
    monkeypatch.setenv("ENVIRONMENT", "production")

    check_setup.check_database_url_scheme()

    assert _statuses()["DATABASE_URL scheme"] == check_setup.FAIL


def test_sqlite_database_url_is_only_a_warning_in_development(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./local.db")
    monkeypatch.setenv("ENVIRONMENT", "development")

    check_setup.check_database_url_scheme()

    assert _statuses()["DATABASE_URL scheme"] == check_setup.WARN


def test_postgres_database_url_passes_in_production(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@host/db")
    monkeypatch.setenv("ENVIRONMENT", "production")

    check_setup.check_database_url_scheme()

    assert _statuses()["DATABASE_URL scheme"] == check_setup.OK


def test_missing_database_url_reports_nothing_here(monkeypatch):
    """Already reported by check_core_env — this check must not duplicate
    the finding under a different name."""
    monkeypatch.delenv("DATABASE_URL", raising=False)

    check_setup.check_database_url_scheme()

    assert "DATABASE_URL scheme" not in _statuses()


# === production_secrets_check integration ===================================


def test_production_secrets_check_passes_when_both_overridden(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", "a-real-long-random-secret-key-not-the-default")
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", "a-real-fernet-key-not-the-default=")

    check_setup.check_production_secrets()

    assert _statuses()["production secrets"] == check_setup.OK


def test_production_secrets_check_fails_with_default_secret_key(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SECRET_KEY", "dev-secret-key-change-me")
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", "a-real-fernet-key-not-the-default=")

    check_setup.check_production_secrets()

    assert _statuses()["production secrets"] == check_setup.FAIL
    assert "SECRET_KEY" in _details()["production secrets"]


def test_production_secrets_check_is_silent_in_development(monkeypatch):
    """Development legitimately runs on the published defaults; reporting
    OK for a check that is a no-op there would itself be a small lie."""
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("SECRET_KEY", "dev-secret-key-change-me")

    check_setup.check_production_secrets()

    assert "production secrets" not in _statuses()
