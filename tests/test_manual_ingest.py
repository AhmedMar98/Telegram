"""Tests for the credential-free ingestion paths.

These matter more than usual: manual entry and export import are what make
the platform usable when the Telegram API collector is not (or cannot be)
configured, so they are the difference between a working product and one
that is merely deployed.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.models import AuditLog, Channel, Link
from tests.conftest import register_workspace


def test_paste_extracts_classifies_and_stores(client: TestClient):
    register_workspace(client, email="paste@example.com", workspace_name="Paste Co")

    response = client.post(
        "/links",
        json={
            "text": (
                "تحميل التطبيق https://example.com/app.apk\n"
                "وكتاب مفيد https://example.com/book.pdf، "
                "وفيلم على https://www.youtube.com/watch?v=xyz."
            )
        },
    )
    assert response.status_code == 201
    assert response.json() == {"found": 3, "stored": 3, "duplicates": 0}

    results = client.get("/links").json()
    urls = {item["url"] for item in results["items"]}
    # Trailing Arabic and Latin punctuation must not survive into storage.
    assert urls == {
        "https://example.com/app.apk",
        "https://example.com/book.pdf",
        "https://www.youtube.com/watch?v=xyz",
    }
    categories = {item["url"]: item["category"] for item in results["items"]}
    assert categories["https://example.com/app.apk"] == "software_apps"
    assert categories["https://example.com/book.pdf"] == "books_courses"
    assert categories["https://www.youtube.com/watch?v=xyz"] == "movies_series"


def test_repeated_paste_is_deduplicated(client: TestClient):
    register_workspace(client, email="dedup@example.com", workspace_name="Dedup Co")

    first = client.post("/links", json={"text": "https://example.com/a.apk"}).json()
    second = client.post("/links", json={"text": "https://example.com/a.apk"}).json()

    assert first == {"found": 1, "stored": 1, "duplicates": 0}
    assert second == {"found": 1, "stored": 0, "duplicates": 1}
    assert client.get("/links").json()["total"] == 1


def test_text_without_links_is_reported_not_an_error(client: TestClient):
    register_workspace(client, email="nolinks@example.com", workspace_name="No Links")

    response = client.post("/links", json={"text": "نص عادي بدون أي روابط إطلاقاً"})

    assert response.status_code == 201
    assert response.json() == {"found": 0, "stored": 0, "duplicates": 0}


def test_manual_links_land_in_a_dedicated_channel(client: TestClient):
    register_workspace(client, email="chan@example.com", workspace_name="Chan Co")
    client.post("/links", json={"text": "https://example.com/x.apk"})

    channels = client.get("/channels").json()
    assert [c["tg_channel_id"] for c in channels] == ["manual"]

    # A second paste reuses that channel rather than creating another.
    client.post("/links", json={"text": "https://example.com/y.apk"})
    assert len(client.get("/channels").json()) == 1


def test_manual_add_is_audited(client: TestClient):
    register_workspace(client, email="audit@example.com", workspace_name="Audit Co")
    client.post("/links", json={"text": "https://example.com/z.apk"})

    db = SessionLocal()
    try:
        actions = [row.action for row in db.query(AuditLog).all()]
        assert "link.manual_add" in actions
    finally:
        db.close()


def test_manual_add_requires_authentication(client: TestClient):
    assert client.post("/links", json={"text": "https://example.com/a.apk"}).status_code == 401


def test_manual_links_are_workspace_isolated(client: TestClient):
    register_workspace(client, email="iso-a@example.com", workspace_name="Iso A")
    client.post("/links", json={"text": "https://secret.example/private.pdf"})
    client.post("/auth/logout")

    register_workspace(client, email="iso-b@example.com", workspace_name="Iso B")
    assert client.get("/links").json()["total"] == 0
    assert client.get("/links", params={"q": "private"}).json()["total"] == 0
    assert client.get("/channels").json() == []


def test_empty_paste_is_rejected_by_validation(client: TestClient):
    register_workspace(client, email="empty@example.com", workspace_name="Empty Co")
    assert client.post("/links", json={"text": ""}).status_code == 422


def test_duplicate_across_paste_and_collector_channels_are_independent(client: TestClient):
    """The same URL from two different sources is two rows, by design.

    Dedup is scoped per channel, so a link seen both in a channel and added
    by hand is kept twice rather than one silently masking the other.
    """
    register_workspace(client, email="cross@example.com", workspace_name="Cross Co")
    client.post("/channels", json={"tg_channel_id": "555", "username": "src"})
    client.post("/links", json={"text": "https://example.com/shared.apk"})

    db = SessionLocal()
    try:
        workspace_id = db.query(Channel).first().workspace_id
        other = db.query(Channel).filter(Channel.tg_channel_id == "555").one()
        from app.ingest import store_link

        assert store_link(
            db,
            workspace_id=workspace_id,
            channel_id=other.id,
            message_id=1,
            url="https://example.com/shared.apk",
        )
        db.commit()
        assert db.query(Link).count() == 2
    finally:
        db.close()
