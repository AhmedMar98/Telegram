"""Self-service export and workspace deletion.

The load-bearing test here is
``test_every_workspace_scoped_table_is_covered_by_delete``: it reads
SQLAlchemy's own metadata rather than a hand-written list, so adding a new
``workspace_id`` table without wiring it into deletion fails the suite
instead of quietly leaving orphaned rows behind forever.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.account_data import WORKSPACE_TABLES
from app.database import Base, SessionLocal
from app.models import (
    ActionEvent,
    AuditLog,
    AuthSession,
    Channel,
    Link,
    LoginAttempt,
    TelegramAccount,
    User,
    Workspace,
)
from tests.conftest import register_workspace

PASSWORD = "j8Kd0-slwQ2x"


def _seed(client: TestClient, email: str, name: str) -> int:
    register_workspace(client, email=email, workspace_name=name)
    client.post("/links", json={"text": "كتاب https://example.com/book.pdf"})
    client.post("/channels", json={"tg_channel_id": "1", "username": "chan"})
    return client.get("/auth/me").json()["workspace_id"]


# --- the standing invariant ------------------------------------------------


def test_every_workspace_scoped_table_is_covered_by_delete():
    """A new workspace_id table must be added to WORKSPACE_TABLES.

    Reads the mapper registry, not a duplicate list, so this cannot drift.
    """
    scoped = {
        mapper.class_.__tablename__
        for mapper in Base.registry.mappers
        if "workspace_id" in mapper.class_.__table__.columns
    }
    covered = {model.__tablename__ for model in WORKSPACE_TABLES}

    assert scoped == covered, f"not covered by delete_workspace: {sorted(scoped - covered)}"


def test_delete_order_puts_children_before_their_parents():
    """WORKSPACE_TABLES is order-sensitive: a parent deleted first would
    trip a foreign key on engines that enforce them."""
    order = [model.__tablename__ for model in WORKSPACE_TABLES]

    assert order.index("links") < order.index("channels")
    assert order.index("channels") < order.index("telegram_accounts")
    assert order.index("audit_log") < order.index("users")


# --- export ----------------------------------------------------------------


def test_export_contains_the_workspaces_own_data(client: TestClient):
    _seed(client, "exp@example.com", "Export Co")

    body = client.get("/auth/me/export").json()

    assert body["workspace"]["name"] == "Export Co"
    assert [u["email"] for u in body["users"]] == ["exp@example.com"]
    assert [link["url"] for link in body["links"]] == ["https://example.com/book.pdf"]
    assert any(c["username"] == "chan" for c in body["channels"])


def test_export_never_includes_credentials(client: TestClient):
    workspace_id = _seed(client, "cred@example.com", "Cred Co")
    db = SessionLocal()
    try:
        db.add(TelegramAccount(workspace_id=workspace_id, label="primary", session_string="encrypted-blob"))
        db.commit()
    finally:
        db.close()

    raw = client.get("/auth/me/export").text

    assert "password_hash" not in raw
    assert "session_string" not in raw
    assert "encrypted-blob" not in raw
    assert json.loads(raw)["telegram_accounts"][0]["label"] == "primary"


def test_export_is_offered_as_a_download(client: TestClient):
    _seed(client, "dl@example.com", "DL Co")

    resp = client.get("/auth/me/export")

    assert "attachment" in resp.headers["content-disposition"]


def test_export_excludes_other_workspaces(client: TestClient):
    _seed(client, "one@example.com", "One")
    client.post("/auth/logout")
    _seed(client, "two@example.com", "Two")

    body = client.get("/auth/me/export").json()

    assert [u["email"] for u in body["users"]] == ["two@example.com"]
    assert body["workspace"]["name"] == "Two"


def test_export_requires_authentication(client: TestClient):
    assert client.get("/auth/me/export").status_code == 401


# --- delete ----------------------------------------------------------------


def _counts(workspace_id: int) -> dict[str, int]:
    db = SessionLocal()
    try:
        return {
            "workspace": db.query(Workspace).filter(Workspace.id == workspace_id).count(),
            "users": db.query(User).filter(User.workspace_id == workspace_id).count(),
            "links": db.query(Link).filter(Link.workspace_id == workspace_id).count(),
            "channels": db.query(Channel).filter(Channel.workspace_id == workspace_id).count(),
            "audit": db.query(AuditLog).filter(AuditLog.workspace_id == workspace_id).count(),
            "accounts": db.query(TelegramAccount).filter(TelegramAccount.workspace_id == workspace_id).count(),
        }
    finally:
        db.close()


def test_delete_removes_every_trace_of_the_workspace(client: TestClient):
    workspace_id = _seed(client, "del@example.com", "Delete Co")
    assert _counts(workspace_id)["links"] == 1

    resp = client.post("/auth/me/delete", json={"current_password": PASSWORD, "confirm": "DELETE"})

    assert resp.status_code == 200
    assert resp.json()["deleted"]["links"] == 1
    assert _counts(workspace_id) == {
        "workspace": 0,
        "users": 0,
        "links": 0,
        "channels": 0,
        "audit": 0,
        "accounts": 0,
    }


def test_delete_also_clears_rows_keyed_by_something_other_than_workspace_id(client: TestClient):
    """Sessions, login attempts and rate-limit markers key on user id, email
    and an opaque identifier — none of them on workspace_id."""
    workspace_id = _seed(client, "keys@example.com", "Keys Co")
    db = SessionLocal()
    try:
        db.add(ActionEvent(scope="link_add", identifier=str(workspace_id)))
        db.add(LoginAttempt(identifier="keys@example.com", successful=False))
        db.commit()
    finally:
        db.close()

    client.post("/auth/me/delete", json={"current_password": PASSWORD, "confirm": "DELETE"})

    db = SessionLocal()
    try:
        assert db.query(ActionEvent).filter(ActionEvent.identifier == str(workspace_id)).count() == 0
        assert db.query(LoginAttempt).filter(LoginAttempt.identifier == "keys@example.com").count() == 0
        assert db.query(AuthSession).count() == 0
    finally:
        db.close()


def test_delete_leaves_other_workspaces_untouched(client: TestClient):
    survivor = _seed(client, "keep@example.com", "Keep Co")
    client.post("/auth/logout")
    doomed = _seed(client, "gone@example.com", "Gone Co")

    client.post("/auth/me/delete", json={"current_password": PASSWORD, "confirm": "DELETE"})

    surviving = _counts(survivor)
    assert _counts(doomed)["workspace"] == 0
    assert surviving["workspace"] == 1
    assert surviving["users"] == 1
    assert surviving["links"] == 1
    assert surviving["channels"] == 2  # the manual-entry bucket plus the one _seed adds
    assert surviving["audit"] > 0  # its own history, untouched


def test_delete_requires_the_correct_password(client: TestClient):
    workspace_id = _seed(client, "wrong@example.com", "Wrong Co")

    resp = client.post("/auth/me/delete", json={"current_password": "not-my-password", "confirm": "DELETE"})

    assert resp.status_code == 403
    assert _counts(workspace_id)["workspace"] == 1


def test_delete_requires_the_literal_confirmation(client: TestClient):
    workspace_id = _seed(client, "confirm@example.com", "Confirm Co")

    resp = client.post("/auth/me/delete", json={"current_password": PASSWORD, "confirm": "delete"})

    assert resp.status_code == 422
    assert _counts(workspace_id)["workspace"] == 1


def test_session_stops_working_after_deletion(client: TestClient):
    _seed(client, "session@example.com", "Session Co")

    client.post("/auth/me/delete", json={"current_password": PASSWORD, "confirm": "DELETE"})

    assert client.get("/auth/me").status_code == 401


def test_delete_requires_authentication(client: TestClient):
    assert client.post("/auth/me/delete", json={"current_password": "x", "confirm": "DELETE"}).status_code == 401
