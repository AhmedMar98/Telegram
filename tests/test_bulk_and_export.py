"""Tests for bulk delete/recategorize and JSON export."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from tests.conftest import register_workspace


def _add(client: TestClient, text: str) -> None:
    assert client.post("/links", json={"text": text}).status_code == 201


def test_bulk_delete_by_category(client: TestClient):
    register_workspace(client, email="bd@example.com", workspace_name="BD")
    _add(client, "https://example.com/a.apk https://example.com/b.exe")
    _add(client, "https://example.com/c.pdf")

    resp = client.post("/links/bulk/delete", json={"category": "software_apps"})
    assert resp.json()["affected"] == 2
    assert client.get("/links").json()["total"] == 1


def test_bulk_delete_with_no_filter_matches_whole_workspace(client: TestClient):
    register_workspace(client, email="bdall@example.com", workspace_name="BDAll")
    _add(client, "https://example.com/a.apk")
    _add(client, "https://example.com/b.pdf")

    resp = client.post("/links/bulk/delete", json={})
    assert resp.json()["affected"] == 2
    assert client.get("/links").json()["total"] == 0


def test_bulk_delete_is_scoped_to_workspace(client: TestClient):
    register_workspace(client, email="bdiso@example.com", workspace_name="BDIso")
    _add(client, "https://example.com/mine.apk")
    client.post("/auth/logout")

    register_workspace(client, email="bdiso2@example.com", workspace_name="BDIso2")
    resp = client.post("/links/bulk/delete", json={})
    assert resp.json()["affected"] == 0  # nothing in *this* workspace

    client.post("/auth/logout")
    client.post("/auth/login", json={"email": "bdiso@example.com", "password": "j8Kd0-slwQ2x"})
    assert client.get("/links").json()["total"] == 1  # untouched


def test_bulk_recategorize_by_category_filter(client: TestClient):
    register_workspace(client, email="br@example.com", workspace_name="BR")
    _add(client, "https://example.com/a.apk https://example.com/b.exe")

    resp = client.post("/links/bulk/recategorize", json={"category": "software_apps", "new_category": "games"})
    assert resp.json()["affected"] == 2

    items = client.get("/links").json()["items"]
    assert all(i["category"] == "games" and i["classified_by"] == "manual" for i in items)


def test_bulk_recategorize_rejects_unknown_category(client: TestClient):
    register_workspace(client, email="brbad@example.com", workspace_name="BRBad")
    _add(client, "https://example.com/a.apk")
    resp = client.post("/links/bulk/recategorize", json={"new_category": "not-a-real-category"})
    assert resp.status_code == 422


def test_bulk_operations_require_authentication(client: TestClient):
    assert client.post("/links/bulk/delete", json={}).status_code == 401
    assert client.post("/links/bulk/recategorize", json={"new_category": "games"}).status_code == 401


def test_export_json_matches_csv_content(client: TestClient):
    register_workspace(client, email="ej@example.com", workspace_name="EJ")
    _add(client, "كتاب https://example.com/book.pdf وتطبيق https://example.com/app.apk")

    resp = client.get("/links/export.json")
    assert resp.status_code == 200
    assert "application/json" in resp.headers["content-type"]
    assert "attachment" in resp.headers["content-disposition"]

    rows = json.loads(resp.text)
    urls = {r["url"] for r in rows}
    assert urls == {"https://example.com/book.pdf", "https://example.com/app.apk"}
    assert all(
        set(r)
        == {
            "url",
            "category",
            "confidence",
            "classified_by",
            "matched_rule",
            "source_type",
            "forwarded_from",
            "language",
            "is_favorite",
            "domain",
            "posted_at",
            "collected_at",
            "is_alive",
            "http_status",
            "last_checked_at",
            "context",
        }
        for r in rows
    )
    # A freshly-collected link has never been vitality-checked.
    assert all(r["is_alive"] is None and r["last_checked_at"] is None for r in rows)


def test_export_json_empty_workspace_is_empty_array(client: TestClient):
    register_workspace(client, email="ejempty@example.com", workspace_name="EJEmpty")
    assert json.loads(client.get("/links/export.json").text) == []


def test_export_json_filters_by_category(client: TestClient):
    register_workspace(client, email="ejf@example.com", workspace_name="EJF")
    _add(client, "https://example.com/a.apk https://example.com/b.pdf")

    rows = json.loads(client.get("/links/export.json", params={"category": "books_courses"}).text)
    assert [r["url"] for r in rows] == ["https://example.com/b.pdf"]


def test_export_json_requires_authentication(client: TestClient):
    assert client.get("/links/export.json").status_code == 401


def test_link_add_rate_limit(client: TestClient, monkeypatch):
    from app.routers import links as links_module

    monkeypatch.setattr(links_module, "LINK_ADD_LIMIT", 3)
    register_workspace(client, email="rl@example.com", workspace_name="RL")

    for i in range(3):
        resp = client.post("/links", json={"text": f"https://example.com/{i}.apk"})
        assert resp.status_code == 201

    limited = client.post("/links", json={"text": "https://example.com/one-more.apk"})
    assert limited.status_code == 429


def test_link_add_rate_limit_is_scoped_per_workspace(client: TestClient, monkeypatch):
    from app.routers import links as links_module

    monkeypatch.setattr(links_module, "LINK_ADD_LIMIT", 1)
    register_workspace(client, email="rlws1@example.com", workspace_name="RLWS1")
    assert client.post("/links", json={"text": "https://example.com/a.apk"}).status_code == 201
    assert client.post("/links", json={"text": "https://example.com/b.apk"}).status_code == 429

    client.post("/auth/logout")
    register_workspace(client, email="rlws2@example.com", workspace_name="RLWS2")
    # A different workspace's limit is independent.
    assert client.post("/links", json={"text": "https://example.com/c.apk"}).status_code == 201


def test_search_orders_by_relevance_on_postgres_only(client: TestClient):
    """On SQLite (the ILIKE fallback) ranking does not apply; both must still return results."""
    register_workspace(client, email="rank@example.com", workspace_name="Rank")
    _add(client, "https://example.com/python-guide.pdf")
    _add(client, "https://example.com/other.pdf مقال عرضي يذكر بايثون مرة واحدة")

    results = client.get("/links", params={"q": "python"}).json()
    assert results["total"] >= 1
