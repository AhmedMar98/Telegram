"""Tests for production_secrets_check() and the lifespan check that uses it.

Both published defaults in app/config.py (SECRET_KEY, FIELD_ENCRYPTION_KEY)
are committed to this public repository. Running production with either one
is not weak security, it is no security at all: a session cookie anyone can
forge, or field encryption anyone can reverse. This used to be a comment
("MUST be overridden") that nothing enforced; this test file is what turns
it into a contract app/main.py's lifespan actually keeps.
"""

from __future__ import annotations

import pytest

from app.config import (
    _PUBLISHED_DEFAULTS,
    Settings,
    production_secrets_check,
    published_defaults_in_use,
    require_real_secrets,
)


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


# === Scheduled jobs: the guard that closes CR-01 =============================
#
# app/main.py's lifespan protects the web service. The collector was exempt,
# and it is the process that *writes* encrypted Telegram session strings —
# so a published FIELD_ENCRYPTION_KEY there is worse than on the web side.
# No workflow in .github/workflows sets ENVIRONMENT, so a check gated on
# "production" would have been permanently silent in exactly the place it
# was needed. These tests pin both halves of that reasoning.


def test_published_defaults_in_use_ignores_environment():
    """Ungated by design: a key printed in this repo protects nothing
    regardless of what ENVIRONMENT happens to say."""
    settings = Settings(
        environment="development",
        secret_key=_PUBLISHED_DEFAULTS["SECRET_KEY"],
        field_encryption_key=_PUBLISHED_DEFAULTS["FIELD_ENCRYPTION_KEY"],
    )

    assert set(published_defaults_in_use(settings)) == {"SECRET_KEY", "FIELD_ENCRYPTION_KEY"}


def test_published_defaults_in_use_honours_the_names_filter():
    """The collector signs no cookie, so it must not fail on SECRET_KEY."""
    settings = Settings(
        environment="development",
        secret_key=_PUBLISHED_DEFAULTS["SECRET_KEY"],
        field_encryption_key="a-real-fernet-key-not-the-default=",
    )

    assert published_defaults_in_use(settings, names=("FIELD_ENCRYPTION_KEY",)) == []


def test_require_real_secrets_raises_and_names_the_job():
    settings = Settings(
        environment="development",
        secret_key="a-real-long-random-secret-key",
        field_encryption_key=_PUBLISHED_DEFAULTS["FIELD_ENCRYPTION_KEY"],
    )

    with pytest.raises(RuntimeError) as excinfo:
        require_real_secrets(settings, names=("FIELD_ENCRYPTION_KEY",), job="collector")

    message = str(excinfo.value)
    assert "collector" in message
    assert "FIELD_ENCRYPTION_KEY" in message
    # The message must never echo the value itself, only the variable name.
    assert _PUBLISHED_DEFAULTS["FIELD_ENCRYPTION_KEY"] not in message


def test_require_real_secrets_is_silent_when_the_key_is_real():
    settings = Settings(
        environment="development",
        secret_key=_PUBLISHED_DEFAULTS["SECRET_KEY"],
        field_encryption_key="a-real-fernet-key-not-the-default=",
    )

    require_real_secrets(settings, names=("FIELD_ENCRYPTION_KEY",), job="collector")


def test_an_empty_actions_secret_does_not_slip_past_the_collector_guard():
    """The exact CR-01 mechanism: an unset GitHub Actions secret exports an
    empty string, config.py's validator turns that into the published
    default, and the collector used to encrypt with it silently."""
    settings = Settings(environment="development", field_encryption_key="")

    with pytest.raises(RuntimeError):
        require_real_secrets(settings, names=("FIELD_ENCRYPTION_KEY",), job="collector")


def test_the_collector_entrypoint_refuses_before_doing_any_work(monkeypatch):
    """main() must raise before asyncio.run(collect()) is reached — proven
    by making collect() explode if it is ever called."""
    from app.config import get_settings
    from scripts import collect as collector

    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", _PUBLISHED_DEFAULTS["FIELD_ENCRYPTION_KEY"])
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")

    def _must_not_run():
        raise AssertionError("collect() ran despite a published encryption key")

    monkeypatch.setattr(collector, "collect", _must_not_run)
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="collector"):
            collector.main()
    finally:
        get_settings.cache_clear()


# === Owner decision: collecting-account ceiling ==============================


def test_the_account_ceiling_is_ten():
    """Owner decision, pinned so a future edit cannot lower it silently.

    scripts/add_account.py refuses the 11th account by reading this value,
    so the number is the whole enforcement — there is nothing else to test.
    """
    assert Settings().max_accounts_per_workspace == 10


# === The guard that would have caught scripts/run_runtime.py =================


def _script_calls(path) -> set[str]:
    """Every function name called at any depth in one script."""
    import ast

    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def test_every_script_that_touches_a_session_string_refuses_published_defaults():
    """Enumerated from the code, not from a list somebody must remember.

    ``FIELD_ENCRYPTION_KEY`` has a working default published in this
    repository, so a job that encrypts or decrypts a Telegram session
    string under it is storing a bearer credential in plaintext as far as
    anyone holding the database is concerned.

    ``scripts/collect.py`` and ``scripts/add_account.py`` have refused to
    run on that default since they were written. ``scripts/run_runtime.py``
    was added in phase 3 and did not — nothing caught it, because the rule
    lived in two call sites and in prose. It lives here now: the check is
    derived from which scripts actually call the crypto helpers, so the
    next entrypoint that touches one is covered on the day it is written.
    """
    from pathlib import Path

    scripts = Path(__file__).resolve().parent.parent / "scripts"
    offenders = []
    for path in sorted(scripts.glob("*.py")):
        calls = _script_calls(path)
        if not calls & {"encrypt_field", "decrypt_field"}:
            continue
        if "require_real_secrets" not in calls:
            offenders.append(path.name)
    assert not offenders, (
        f"{offenders} encrypt or decrypt a Telegram session string without calling "
        "require_real_secrets(), so they would run happily under the published "
        "FIELD_ENCRYPTION_KEY and write credentials that are effectively plaintext"
    )


def test_the_guard_above_is_actually_looking_at_something():
    """Guards the guard: an empty sweep would pass over zero scripts."""
    from pathlib import Path

    scripts = Path(__file__).resolve().parent.parent / "scripts"
    touching = [
        p.name for p in sorted(scripts.glob("*.py")) if _script_calls(p) & {"encrypt_field", "decrypt_field"}
    ]
    assert len(touching) >= 3, f"expected the credential-touching scripts, found {touching}"
