"""
Tests for the FastAPI web layer (app/web/routes.py).

Uses FastAPI's ``dependency_overrides`` to swap ``get_db`` for the in-memory
test session, so every route shares one SQLite connection and sees the same
data. The TestClient is built WITHOUT a ``with`` block on purpose: that skips
the app lifespan (log broadcaster, daily-summary worker, Telegram bot), keeping
each test hermetic and fast.

Endpoints that reach outside the DB — ``/api/v1/search`` (LLM orchestrator) and
``/api/v1/links/{id}/check`` (live HTTP) — are covered by their own dedicated
test modules and are intentionally not exercised here.
"""

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import create_app
from app.models import Link, LinkStatus


@pytest.fixture
def client(db_session):
    """A TestClient whose get_db dependency yields the test session."""
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    test_client = TestClient(app)
    yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def sample_links(db_session):
    """Three links spanning categories and alive/dead/new states."""
    db_session.add_all(
        [
            Link(
                url="https://t.me/alpha",
                url_hash="h_alpha",
                category="telegram",
                status=LinkStatus.ALIVE.value,
                alive=True,
                confidence=0.9,
            ),
            Link(
                url="https://example.com/file.pdf",
                url_hash="h_file",
                category="file",
                status=LinkStatus.DEAD.value,
                alive=False,
                confidence=0.95,
            ),
            Link(
                url="https://chat.whatsapp.com/xyz",
                url_hash="h_wa",
                category="whatsapp",
                status=LinkStatus.NEW.value,
                confidence=0.0,
            ),
        ]
    )
    db_session.commit()
    return db_session


# ============================================================
# Health
# ============================================================


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "semantic_memory" in body["features"]


# ============================================================
# REST API — /api/v1/stats
# ============================================================


def test_stats_empty(client):
    r = client.get("/api/v1/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["links"] == {"total": 0, "alive": 0, "dead": 0, "new": 0}
    assert body["by_category"] == {}


def test_stats_with_data(client, sample_links):
    r = client.get("/api/v1/stats")
    body = r.json()
    assert body["links"]["total"] == 3
    assert body["links"]["alive"] == 1
    assert body["links"]["dead"] == 1
    assert body["by_category"]["telegram"] == 1
    assert body["by_category"]["file"] == 1


# ============================================================
# REST API — /api/v1/links
# ============================================================


def test_list_links_all(client, sample_links):
    r = client.get("/api/v1/links")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert len(body["items"]) == 3


def test_list_links_category_filter(client, sample_links):
    r = client.get("/api/v1/links", params={"category": "file"})
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["category"] == "file"


def test_list_links_alive_filter(client, sample_links):
    r = client.get("/api/v1/links", params={"alive": "true"})
    body = r.json()
    assert body["total"] == 1
    assert all(item["alive"] for item in body["items"])


def test_list_links_pagination(client, sample_links):
    r = client.get("/api/v1/links", params={"limit": 2, "offset": 0})
    body = r.json()
    assert body["limit"] == 2
    assert body["total"] == 3
    assert len(body["items"]) == 2


def test_get_link_found(client, sample_links):
    link_id = client.get("/api/v1/links").json()["items"][0]["id"]
    r = client.get(f"/api/v1/links/{link_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == link_id
    assert "classification_logs" in body
    assert "vitality_checks" in body


def test_get_link_not_found(client):
    r = client.get("/api/v1/links/999999")
    assert r.status_code == 404


# ============================================================
# REST API — click counter
# ============================================================


def test_click_increments(client, sample_links):
    link_id = client.get("/api/v1/links").json()["items"][0]["id"]
    first = client.post(f"/api/v1/links/{link_id}/click")
    assert first.status_code == 200
    assert first.json()["click_count"] == 1
    second = client.post(f"/api/v1/links/{link_id}/click")
    assert second.json()["click_count"] == 2


def test_click_not_found(client):
    r = client.post("/api/v1/links/999999/click")
    assert r.status_code == 404


# ============================================================
# HTML panel pages render (status 200 + HTML content-type)
# ============================================================


@pytest.mark.parametrize("path", ["/", "/links", "/accounts", "/tenants", "/logs"])
def test_panel_pages_render(client, sample_links, path):
    r = client.get(path)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
