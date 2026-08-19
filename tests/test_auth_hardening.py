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
    login = client.post("/auth/login", json={"email": "MIXED.CASE@example.com", "password": "password123"})
    assert login.status_code == 200


def test_duplicate_registration_is_case_insensitive(client: TestClient):
    register_workspace(client, email="dup@example.com", workspace_name="A")
    clash = client.post(
        "/auth/register",
        json={"email": "DUP@Example.com", "password": "password123", "workspace_name": "B"},
    )
    assert clash.status_code == 409


def test_repeated_failures_lock_the_account_out(client: TestClient):
    register_workspace(client, email="target@example.com", workspace_name="Target")
    client.post("/auth/logout")

    for _ in range(LOGIN_MAX_FAILURES):
        rejected = client.post("/auth/login", json={"email": "target@example.com", "password": "wrong-password"})
        assert rejected.status_code == 401

    # Even the *correct* password is refused once the threshold is crossed.
    locked = client.post("/auth/login", json={"email": "target@example.com", "password": "password123"})
    assert locked.status_code == 429


def test_successful_login_clears_prior_failures(client: TestClient):
    register_workspace(client, email="clears@example.com", workspace_name="Clears")
    client.post("/auth/logout")

    for _ in range(LOGIN_MAX_FAILURES - 1):
        client.post("/auth/login", json={"email": "clears@example.com", "password": "nope"})

    good = client.post("/auth/login", json={"email": "clears@example.com", "password": "password123"})
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
        json={"email": "nocode@example.com", "password": "password123", "workspace_name": "X"},
    )
    assert without.status_code == 403

    wrong = client.post(
        "/auth/register",
        json={
            "email": "badcode@example.com",
            "password": "password123",
            "workspace_name": "X",
            "invite_code": "guess",
        },
    )
    assert wrong.status_code == 403

    correct = client.post(
        "/auth/register",
        json={
            "email": "goodcode@example.com",
            "password": "password123",
            "workspace_name": "X",
            "invite_code": "secret-invite",
        },
    )
    assert correct.status_code == 201


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
