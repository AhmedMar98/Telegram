"""Tests for brute-force throttling, enumeration resistance, and invite gating."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal
from app.models import LoginAttempt, User
from app.security import LOGIN_MAX_FAILURES, constant_time_equals, normalize_email
from tests.conftest import register_workspace


def test_email_is_normalized_on_register_and_login(client: TestClient):
    register_workspace(client, email="Mixed.Case@Example.COM", workspace_name="Case Co")

    db = SessionLocal()
    try:
        assert db.query(User).one().email == "mixed.case@example.com"
    finally:
        db.close()

    client.post("/auth/logout")
    # Logging in with different casing must reach the same account.
    login = client.post("/auth/login", json={"email": "MIXED.CASE@example.com", "password": "j8Kd0-slwQ2x"})
    assert login.status_code == 200


def test_duplicate_registration_is_case_insensitive(client: TestClient):
    register_workspace(client, email="dup@example.com", workspace_name="A")
    clash = client.post(
        "/auth/register",
        json={"email": "DUP@Example.com", "password": "j8Kd0-slwQ2x", "workspace_name": "B"},
    )
    assert clash.status_code == 409


def test_repeated_failures_lock_the_account_out(client: TestClient):
    register_workspace(client, email="target@example.com", workspace_name="Target")
    client.post("/auth/logout")

    for _ in range(LOGIN_MAX_FAILURES):
        rejected = client.post("/auth/login", json={"email": "target@example.com", "password": "wrong-password"})
        assert rejected.status_code == 401

    # Even the *correct* password is refused once the threshold is crossed.
    locked = client.post("/auth/login", json={"email": "target@example.com", "password": "j8Kd0-slwQ2x"})
    assert locked.status_code == 429


def test_successful_login_clears_prior_failures(client: TestClient):
    register_workspace(client, email="clears@example.com", workspace_name="Clears")
    client.post("/auth/logout")

    for _ in range(LOGIN_MAX_FAILURES - 1):
        client.post("/auth/login", json={"email": "clears@example.com", "password": "nope"})

    good = client.post("/auth/login", json={"email": "clears@example.com", "password": "j8Kd0-slwQ2x"})
    assert good.status_code == 200

    db = SessionLocal()
    try:
        remaining = (
            db.query(LoginAttempt)
            .filter(LoginAttempt.identifier == "clears@example.com", LoginAttempt.successful.is_(False))
            .count()
        )
        assert remaining == 0
    finally:
        db.close()


def test_unknown_account_is_throttled_too(client: TestClient):
    """Guessing against a non-existent address must not be unlimited."""
    for _ in range(LOGIN_MAX_FAILURES):
        miss = client.post("/auth/login", json={"email": "ghost@example.com", "password": "x"})
        assert miss.status_code == 401

    assert client.post("/auth/login", json={"email": "ghost@example.com", "password": "x"}).status_code == 429


def test_unknown_and_wrong_password_are_indistinguishable(client: TestClient):
    register_workspace(client, email="real@example.com", workspace_name="Real")
    client.post("/auth/logout")

    wrong_password = client.post("/auth/login", json={"email": "real@example.com", "password": "bad"})
    no_such_user = client.post("/auth/login", json={"email": "absent@example.com", "password": "bad"})

    assert wrong_password.status_code == no_such_user.status_code == 401
    assert wrong_password.json() == no_such_user.json()


def test_invite_code_is_enforced_when_configured(client: TestClient, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "invite_code", "secret-invite")

    without = client.post(
        "/auth/register",
        json={"email": "nocode@example.com", "password": "j8Kd0-slwQ2x", "workspace_name": "X"},
    )
    assert without.status_code == 403

    wrong = client.post(
        "/auth/register",
        json={
            "email": "badcode@example.com",
            "password": "j8Kd0-slwQ2x",
            "workspace_name": "X",
            "invite_code": "guess",
        },
    )
    assert wrong.status_code == 403

    correct = client.post(
        "/auth/register",
        json={
            "email": "goodcode@example.com",
            "password": "j8Kd0-slwQ2x",
            "workspace_name": "X",
            "invite_code": "secret-invite",
        },
    )
    assert correct.status_code == 201


def test_the_invite_gate_answers_before_the_email_conflict(client: TestClient, monkeypatch):
    """Otherwise the gate becomes an oracle for which accounts exist.

    Registration answers 409 for an address already taken. If that check
    ran first, anyone could ask the open endpoint about any address and
    read the answer off the status code — without holding an invite at
    all. Closing signup would then have *added* an enumeration channel.
    The gate has to answer 403 for a known address exactly as it does for
    an unknown one.
    """
    register_workspace(client, email="known@example.com", workspace_name="Known")
    client.post("/auth/logout")

    settings = get_settings()
    monkeypatch.setattr(settings, "invite_code", "secret-invite")

    known = client.post(
        "/auth/register",
        json={"email": "known@example.com", "password": "j8Kd0-slwQ2x", "workspace_name": "X"},
    )
    unknown = client.post(
        "/auth/register",
        json={"email": "unknown@example.com", "password": "j8Kd0-slwQ2x", "workspace_name": "X"},
    )
    assert known.status_code == 403
    assert unknown.status_code == 403
    assert known.status_code == unknown.status_code, "the gate distinguishes a known address from an unknown one"


def test_an_empty_invite_code_is_not_a_bypass(client: TestClient, monkeypatch):
    """`""` is falsy, and the guard reads the *configured* value's truth."""
    settings = get_settings()
    monkeypatch.setattr(settings, "invite_code", "secret-invite")

    blank = client.post(
        "/auth/register",
        json={
            "email": "blank@example.com",
            "password": "j8Kd0-slwQ2x",
            "workspace_name": "X",
            "invite_code": "",
        },
    )
    assert blank.status_code == 403


def test_closing_registration_does_not_close_the_door_on_existing_users(client: TestClient, monkeypatch):
    """The gate belongs to /register alone.

    Switching to invite-only is a change nobody should have to reverse at
    2am because it locked out the accounts that already existed, so the
    property is asserted rather than assumed: an account created before
    the gate went up still authenticates after it, and a wrong password
    is still refused.
    """
    register_workspace(client, email="already@example.com", workspace_name="Already")
    client.post("/auth/logout")

    settings = get_settings()
    monkeypatch.setattr(settings, "invite_code", "secret-invite")

    good = client.post("/auth/login", json={"email": "already@example.com", "password": "j8Kd0-slwQ2x"})
    assert good.status_code == 200

    client.post("/auth/logout")
    bad = client.post("/auth/login", json={"email": "already@example.com", "password": "wrong-password-9999"})
    assert bad.status_code >= 400


def test_the_invite_code_is_configured_by_its_environment_variable(monkeypatch):
    """The gate is only as real as the variable that switches it on.

    Every other test here sets ``settings.invite_code`` directly, which
    proves the handler and skips the question of whether the deployment's
    ``INVITE_CODE`` reaches it at all. A policy configured in a dashboard
    and never read would look identical to one that works.
    """
    from app.config import Settings

    monkeypatch.setenv("INVITE_CODE", "an-invite-from-the-environment")
    assert Settings().invite_code == "an-invite-from-the-environment"

    monkeypatch.delenv("INVITE_CODE", raising=False)
    assert Settings().invite_code is None


def test_constant_time_equals_handles_missing_values():
    assert constant_time_equals("abc", "abc") is True
    assert constant_time_equals("abc", "abd") is False
    assert constant_time_equals(None, "abc") is False
    assert constant_time_equals("abc", None) is False


def test_normalize_email_trims_and_lowercases():
    assert normalize_email("  User@Example.COM  ") == "user@example.com"


def test_session_is_revoked_on_logout(client: TestClient):
    register_workspace(client, email="revoke@example.com", workspace_name="Revoke")
    cookie = client.cookies.get("session")
    assert cookie

    client.post("/auth/logout")

    # Replaying the old cookie must not resurrect the session.
    client.cookies.set("session", cookie)
    assert client.get("/auth/me").status_code == 401
