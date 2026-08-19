"""Tests for change-password and logout-all-sessions."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.models import AuditLog
from tests.conftest import register_workspace


def test_change_password_requires_correct_current_password(client: TestClient):
    register_workspace(client, email="cp@example.com", workspace_name="CP")
    resp = client.post(
        "/auth/change-password", json={"current_password": "wrong", "new_password": "newpassword123"}
    )
    assert resp.status_code == 401


def test_change_password_succeeds_and_new_password_works(client: TestClient):
    register_workspace(client, email="cp2@example.com", workspace_name="CP2")
    resp = client.post(
        "/auth/change-password",
        json={"current_password": "password123", "new_password": "brandnewpassword"},
    )
    assert resp.status_code == 200

    client.post("/auth/logout")
    old = client.post("/auth/login", json={"email": "cp2@example.com", "password": "password123"})
    assert old.status_code == 401
    new = client.post("/auth/login", json={"email": "cp2@example.com", "password": "brandnewpassword"})
    assert new.status_code == 200


def test_change_password_revokes_other_sessions_but_not_current(client: TestClient):
    register_workspace(client, email="cp3@example.com", workspace_name="CP3")

    other = TestClient(client.app)
    other.post("/auth/login", json={"email": "cp3@example.com", "password": "password123"})
    assert other.get("/auth/me").status_code == 200

    resp = client.post(
        "/auth/change-password", json={"current_password": "password123", "new_password": "anotherpassword1"}
    )
    assert resp.json()["other_sessions_revoked"] == 1

    assert client.get("/auth/me").status_code == 200  # current session survives
    assert other.get("/auth/me").status_code == 401  # the other session does not


def test_change_password_is_audited(client: TestClient):
    register_workspace(client, email="cp4@example.com", workspace_name="CP4")
    client.post(
        "/auth/change-password", json={"current_password": "password123", "new_password": "yetanotherpass1"}
    )

    db = SessionLocal()
    try:
        assert "user.change_password" in {row.action for row in db.query(AuditLog).all()}
    finally:
        db.close()


def test_logout_all_revokes_every_session_including_current(client: TestClient):
    register_workspace(client, email="la@example.com", workspace_name="LA")

    other = TestClient(client.app)
    other.post("/auth/login", json={"email": "la@example.com", "password": "password123"})

    resp = client.post("/auth/logout-all")
    assert resp.json()["sessions_revoked"] == 2

    assert client.get("/auth/me").status_code == 401
    assert other.get("/auth/me").status_code == 401


def test_account_management_requires_authentication(client: TestClient):
    assert (
        client.post(
            "/auth/change-password", json={"current_password": "a", "new_password": "bbbbbbbb"}
        ).status_code
        == 401
    )
    assert client.post("/auth/logout-all").status_code == 401
