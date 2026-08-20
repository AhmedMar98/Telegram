"""Tests for removing, correcting and exporting links.

Without these a collection is write-only: a misclassified or unwanted link
stays forever, and the data cannot leave the (time-limited) free database.
"""

from __future__ import annotations

import csv
import io

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.models import AuditLog, Link
from tests.conftest import register_workspace


def _add(client: TestClient, text: str) -> None:
    assert client.post("/links", json={"text": text}).status_code == 201


def _first_link_id(client: TestClient) -> int:
    return client.get("/links").json()["items"][0]["id"]


def test_delete_removes_the_link(client: TestClient):
    register_workspace(client, email="del@example.com", workspace_name="Del Co")
    _add(client, "https://example.com/unwanted.apk")
    link_id = _first_link_id(client)

    assert client.delete(f"/links/{link_id}").status_code == 204
    assert client.get("/links").json()["total"] == 0


def test_delete_is_audited(client: TestClient):
    register_workspace(client, email="dela@example.com", workspace_name="DelA")
    _add(client, "https://example.com/gone.apk")
    client.delete(f"/links/{_first_link_id(client)}")

    db = SessionLocal()
    try:
        assert "link.delete" in {row.action for row in db.query(AuditLog).all()}
    finally:
        db.close()


def test_deleting_a_missing_link_is_404(client: TestClient):
    register_workspace(client, email="del404@example.com", workspace_name="Del404")
    assert client.delete("/links/999999").status_code == 404


def test_cannot_delete_another_workspaces_link(client: TestClient):
    """A foreign id must look identical to a nonexistent one."""
    register_workspace(client, email="owner@example.com", workspace_name="Owner")
    _add(client, "https://example.com/private.pdf")
    victim_id = _first_link_id(client)
    client.post("/auth/logout")

    register_workspace(client, email="attacker@example.com", workspace_name="Attacker")
    assert client.delete(f"/links/{victim_id}").status_code == 404

    client.post("/auth/logout")
    client.post("/auth/login", json={"email": "owner@example.com", "password": "j8Kd0-slwQ2x"})
    assert client.get("/links").json()["total"] == 1  # untouched


def test_recategorize_overrides_the_classifier(client: TestClient):
    register_workspace(client, email="cat@example.com", workspace_name="Cat Co")
    _add(client, "https://example.com/mystery.apk")
    link_id = _first_link_id(client)
    assert client.get("/links").json()["items"][0]["category"] == "software_apps"

    response = client.patch(f"/links/{link_id}", json={"category": "games"})

    assert response.status_code == 200
    body = response.json()
    assert body["category"] == "games"
    assert body["classified_by"] == "manual"
    assert body["confidence"] == 1.0


def test_recategorize_rejects_an_unknown_category(client: TestClient):
    register_workspace(client, email="badcat@example.com", workspace_name="BadCat")
    _add(client, "https://example.com/x.apk")
    resp = client.patch(f"/links/{_first_link_id(client)}", json={"category": "not-a-category"})
    assert resp.status_code == 422


def test_cannot_recategorize_another_workspaces_link(client: TestClient):
    register_workspace(client, email="o2@example.com", workspace_name="O2")
    _add(client, "https://example.com/theirs.apk")
    victim_id = _first_link_id(client)
    client.post("/auth/logout")

    register_workspace(client, email="a2@example.com", workspace_name="A2")
    assert client.patch(f"/links/{victim_id}", json={"category": "games"}).status_code == 404


def test_export_returns_csv_of_the_workspace(client: TestClient):
    register_workspace(client, email="exp@example.com", workspace_name="Exp Co")
    _add(client, "كتاب مفيد https://example.com/book.pdf وتطبيق https://example.com/app.apk")

    response = client.get("/links/export.csv")

    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "attachment" in response.headers["content-disposition"]

    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows[0] == [
        "url",
        "category",
        "confidence",
        "classified_by",
        "is_favorite",
        "domain",
        "posted_at",
        "collected_at",
        "is_alive",
        "http_status",
        "last_checked_at",
        "context",
    ]
    urls = {r[0] for r in rows[1:]}
    assert urls == {"https://example.com/book.pdf", "https://example.com/app.apk"}
    # A freshly-collected link has never been vitality-checked.
    assert all(r[8] == "" and r[9] == "" and r[10] == "" for r in rows[1:])


def test_export_can_be_filtered_by_category(client: TestClient):
    register_workspace(client, email="expf@example.com", workspace_name="ExpF")
    _add(client, "https://example.com/a.apk https://example.com/b.pdf")

    response = client.get("/links/export.csv", params={"category": "books_courses"})

    rows = list(csv.reader(io.StringIO(response.text)))[1:]
    assert [r[0] for r in rows] == ["https://example.com/b.pdf"]


def test_export_excludes_other_workspaces(client: TestClient):
    register_workspace(client, email="exp1@example.com", workspace_name="Exp1")
    _add(client, "https://secret.example/theirs.pdf")
    client.post("/auth/logout")

    register_workspace(client, email="exp2@example.com", workspace_name="Exp2")
    rows = list(csv.reader(io.StringIO(client.get("/links/export.csv").text)))
    assert len(rows) == 1  # header only


def test_export_requires_authentication(client: TestClient):
    assert client.get("/links/export.csv").status_code == 401


def test_link_management_requires_authentication(client: TestClient):
    assert client.delete("/links/1").status_code == 401
    assert client.patch("/links/1", json={"category": "games"}).status_code == 401


def test_deleted_link_can_be_added_again(client: TestClient):
    """Deletion prunes; it does not blocklist the URL."""
    register_workspace(client, email="re@example.com", workspace_name="Re Co")
    _add(client, "https://example.com/again.apk")
    client.delete(f"/links/{_first_link_id(client)}")

    _add(client, "https://example.com/again.apk")

    db = SessionLocal()
    try:
        assert db.query(Link).count() == 1
    finally:
        db.close()
