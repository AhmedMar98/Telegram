"""Tests for session listing/revocation, favorites, sort options, top domains."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import register_workspace


def _add(client: TestClient, text: str) -> None:
    assert client.post("/links", json={"text": text}).status_code == 201


# --- sessions -----------------------------------------------------------


def test_list_sessions_shows_current_session_marked(client: TestClient):
    register_workspace(client, email="s1@example.com", workspace_name="S1")
    sessions = client.get("/auth/sessions").json()
    assert len(sessions) == 1
    assert sessions[0]["is_current"] is True


def test_list_sessions_shows_all_devices(client: TestClient):
    register_workspace(client, email="s2@example.com", workspace_name="S2")
    other = TestClient(client.app)
    other.post("/auth/login", json={"email": "s2@example.com", "password": "j8Kd0-slwQ2x"})

    sessions = client.get("/auth/sessions").json()
    assert len(sessions) == 2
    assert sum(1 for s in sessions if s["is_current"]) == 1


def test_revoke_specific_session_by_id(client: TestClient):
    register_workspace(client, email="s3@example.com", workspace_name="S3")
    other = TestClient(client.app)
    other.post("/auth/login", json={"email": "s3@example.com", "password": "j8Kd0-slwQ2x"})

    other_session_id = next(s["id"] for s in client.get("/auth/sessions").json() if not s["is_current"])
    resp = client.delete(f"/auth/sessions/{other_session_id}")
    assert resp.status_code == 204

    assert other.get("/auth/me").status_code == 401
    assert client.get("/auth/me").status_code == 200  # current session untouched


def test_revoke_missing_session_is_404(client: TestClient):
    register_workspace(client, email="s4@example.com", workspace_name="S4")
    assert client.delete("/auth/sessions/999999").status_code == 404


def test_cannot_revoke_another_users_session(client: TestClient):
    register_workspace(client, email="victim@example.com", workspace_name="Victim")
    victim_session_id = client.get("/auth/sessions").json()[0]["id"]
    client.post("/auth/logout")

    register_workspace(client, email="attacker@example.com", workspace_name="Attacker")
    assert client.delete(f"/auth/sessions/{victim_session_id}").status_code == 404


def test_sessions_require_authentication(client: TestClient):
    assert client.get("/auth/sessions").status_code == 401
    assert client.delete("/auth/sessions/1").status_code == 401


# --- favorites ------------------------------------------------------------


def test_mark_and_unmark_favorite(client: TestClient):
    register_workspace(client, email="f1@example.com", workspace_name="F1")
    _add(client, "https://example.com/a.apk")
    link_id = client.get("/links").json()["items"][0]["id"]

    marked = client.post(f"/links/{link_id}/favorite")
    assert marked.json()["is_favorite"] is True

    unmarked = client.post(f"/links/{link_id}/favorite", params={"is_favorite": "false"})
    assert unmarked.json()["is_favorite"] is False


def test_filter_search_by_favorite(client: TestClient):
    register_workspace(client, email="f2@example.com", workspace_name="F2")
    _add(client, "https://example.com/a.apk")
    _add(client, "https://example.com/b.apk")
    items = client.get("/links").json()["items"]
    client.post(f"/links/{items[0]['id']}/favorite")

    favorites = client.get("/links", params={"favorite": "true"}).json()
    assert favorites["total"] == 1
    assert favorites["items"][0]["is_favorite"] is True


def test_favorite_requires_authentication(client: TestClient):
    assert client.post("/links/1/favorite").status_code == 401


def test_favorite_is_scoped_to_workspace(client: TestClient):
    register_workspace(client, email="f3@example.com", workspace_name="F3")
    _add(client, "https://example.com/theirs.apk")
    victim_id = client.get("/links").json()["items"][0]["id"]
    client.post("/auth/logout")

    register_workspace(client, email="f4@example.com", workspace_name="F4")
    assert client.post(f"/links/{victim_id}/favorite").status_code == 404


# --- sort -------------------------------------------------------------------


def test_sort_by_domain(client: TestClient):
    register_workspace(client, email="sort1@example.com", workspace_name="Sort1")
    _add(client, "https://zebra.example/a.apk")
    _add(client, "https://alpha.example/b.apk")

    items = client.get("/links", params={"sort": "domain"}).json()["items"]
    assert [i["domain"] for i in items] == ["alpha.example", "zebra.example"]


def test_sort_by_confidence(client: TestClient):
    register_workspace(client, email="sort2@example.com", workspace_name="Sort2")
    _add(client, "https://example.com/a.apk")  # extension match, confidence 0.9
    _add(client, "https://example.com/totally-unrecognized-thing")  # unmatched, confidence 0.0

    items = client.get("/links", params={"sort": "confidence"}).json()["items"]
    assert items[0]["confidence"] >= items[1]["confidence"]


def test_invalid_sort_is_rejected(client: TestClient):
    register_workspace(client, email="sort3@example.com", workspace_name="Sort3")
    resp = client.get("/links", params={"sort": "not-a-real-option"})
    assert resp.status_code == 422


# --- stats: top domains -----------------------------------------------------


def test_stats_reports_top_domains(client: TestClient):
    register_workspace(client, email="dom1@example.com", workspace_name="Dom1")
    _add(client, "https://popular.example/a.apk")
    _add(client, "https://popular.example/b.apk")
    _add(client, "https://rare.example/c.apk")

    domains = dict(client.get("/links/stats").json()["top_domains"])
    assert domains["popular.example"] == 2
    assert domains["rare.example"] == 1


def test_stats_top_domains_is_scoped_to_workspace(client: TestClient):
    register_workspace(client, email="dom2@example.com", workspace_name="Dom2")
    _add(client, "https://mine.example/a.apk")
    client.post("/auth/logout")

    register_workspace(client, email="dom3@example.com", workspace_name="Dom3")
    assert client.get("/links/stats").json()["top_domains"] == []
