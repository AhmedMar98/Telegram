from fastapi.testclient import TestClient

from app.classifier import hash_url
from app.database import SessionLocal
from app.models import Channel, Link
from tests.conftest import register_workspace


def test_register_login_logout_flow(client: TestClient):
    register_workspace(client, email="owner@example.com", workspace_name="Alpha")

    me = client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "owner@example.com"

    logout = client.post("/auth/logout")
    assert logout.status_code == 200

    me_after_logout = client.get("/auth/me")
    assert me_after_logout.status_code == 401


def test_duplicate_email_registration_rejected(client: TestClient):
    register_workspace(client, email="dup@example.com", workspace_name="A")
    second = client.post(
        "/auth/register",
        json={"email": "dup@example.com", "password": "j8Kd0-slwQ2x", "workspace_name": "B"},
    )
    assert second.status_code == 409


def test_wrong_password_rejected(client: TestClient):
    register_workspace(client, email="pw@example.com", workspace_name="A")
    client.post("/auth/logout")
    bad_login = client.post("/auth/login", json={"email": "pw@example.com", "password": "wrong-password"})
    assert bad_login.status_code == 401


def test_unauthenticated_requests_are_rejected(client: TestClient):
    assert client.get("/channels").status_code == 401
    assert client.get("/links").status_code == 401
    assert client.get("/links/stats").status_code == 401


def test_workspace_isolation_across_two_tenants(client: TestClient):
    # Workspace A creates a channel and (directly, simulating the collector) a link.
    register_workspace(client, email="a@example.com", workspace_name="Workspace A")
    channel_res = client.post("/channels", json={"tg_channel_id": "100", "username": "chan_a"})
    assert channel_res.status_code == 201
    channel_a_id = channel_res.json()["id"]

    db = SessionLocal()
    try:
        channel = db.get(Channel, channel_a_id)
        db.add(
            Link(
                workspace_id=channel.workspace_id,
                channel_id=channel.id,
                message_id=1,
                url="https://a-only.example/secret.pdf",
                url_hash=hash_url("https://a-only.example/secret.pdf"),
                domain="a-only.example",
                category="books_courses",
                confidence=0.9,
                classified_by="rules",
                raw_text="سري لمساحة العمل A فقط",
            )
        )
        db.commit()
    finally:
        db.close()

    client.post("/auth/logout")

    # Workspace B must never see workspace A's channel or link.
    register_workspace(client, email="b@example.com", workspace_name="Workspace B")
    channels_b = client.get("/channels").json()
    assert channels_b == []

    search_b = client.get("/links", params={"q": "secret"}).json()
    assert search_b["total"] == 0
    assert search_b["items"] == []

    stats_b = client.get("/links/stats").json()
    assert stats_b["total_links"] == 0
    assert stats_b["total_channels"] == 0
