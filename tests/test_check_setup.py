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
    monkeypatch.setenv("BOT_WEBHOOK_SECRET", "secret")

    check_setup.check_optional_features()
    assert _statuses()["bot"] == check_setup.OK


def test_ungated_registration_is_warned_about(monkeypatch):
    monkeypatch.delenv("INVITE_CODE", raising=False)

    check_setup.check_optional_features()
    assert _statuses()["registration"] == check_setup.WARN


def test_missing_field_encryption_key_is_warned_about(monkeypatch):
    monkeypatch.delenv("FIELD_ENCRYPTION_KEY", raising=False)

    check_setup.check_optional_features()
    assert _statuses()["field encryption"] == check_setup.WARN


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
