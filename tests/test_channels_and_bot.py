from fastapi.testclient import TestClient

from tests.conftest import register_workspace


def test_add_duplicate_channel_rejected(client: TestClient):
    register_workspace(client, email="ch@example.com", workspace_name="Chans")
    first = client.post("/channels", json={"tg_channel_id": "42", "username": "c"})
    assert first.status_code == 201
    dup = client.post("/channels", json={"tg_channel_id": "42", "username": "c"})
    assert dup.status_code == 409


def test_deactivate_channel(client: TestClient):
    register_workspace(client, email="ch2@example.com", workspace_name="Chans2")
    channel = client.post("/channels", json={"tg_channel_id": "43", "username": "c2"}).json()

    delete = client.delete(f"/channels/{channel['id']}")
    assert delete.status_code == 204

    channels = client.get("/channels").json()
    assert channels[0]["is_active"] is False


def test_deactivate_missing_channel_404(client: TestClient):
    register_workspace(client, email="ch3@example.com", workspace_name="Chans3")
    resp = client.delete("/channels/999999")
    assert resp.status_code == 404


def test_bot_link_code_requires_auth(client: TestClient):
    assert client.post("/bot/link-code").status_code == 401


def test_bot_link_code_issued_when_authenticated(client: TestClient):
    register_workspace(client, email="bot@example.com", workspace_name="BotCo")
    resp = client.post("/bot/link-code")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["code"]) == 8
    assert body["code"] in body["instructions"]


def test_webhook_rejects_wrong_secret(client: TestClient):
    resp = client.post("/telegram/webhook/wrong-secret", json={})
    assert resp.status_code == 404


def test_dashboard_redirects_when_not_authenticated(client: TestClient):
    resp = client.get("/dashboard", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/login"


def test_index_redirects_to_login_when_anonymous(client: TestClient):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/login"


def test_healthz(client: TestClient):
    assert client.get("/healthz").json() == {"status": "ok"}
