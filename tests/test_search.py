from fastapi.testclient import TestClient

from app.classifier import hash_url
from app.database import SessionLocal
from app.models import Link
from tests.conftest import register_workspace


def _seed_link(workspace_id: int, channel_id: int, message_id: int, url: str, category: str, raw_text: str):
    db = SessionLocal()
    try:
        db.add(
            Link(
                workspace_id=workspace_id,
                channel_id=channel_id,
                message_id=message_id,
                url=url,
                url_hash=hash_url(url),
                domain="example.com",
                category=category,
                confidence=0.9,
                classified_by="rules",
                raw_text=raw_text,
            )
        )
        db.commit()
    finally:
        db.close()


def test_search_and_category_filter(client: TestClient):
    register_workspace(client, email="s@example.com", workspace_name="Search Co")
    workspace_id = client.get("/auth/me").json()["workspace_id"]
    channel = client.post("/channels", json={"tg_channel_id": "1", "username": "c1"}).json()

    _seed_link(
        channel_id=channel["id"],
        workspace_id=workspace_id,
        message_id=1,
        url="https://example.com/movie.mp4",
        category="movies_series",
        raw_text="فيلم اكشن رائع",
    )
    _seed_link(
        channel_id=channel["id"],
        workspace_id=workspace_id,
        message_id=2,
        url="https://example.com/book.pdf",
        category="books_courses",
        raw_text="كتاب برمجة",
    )

    all_results = client.get("/links").json()
    assert all_results["total"] == 2

    filtered = client.get("/links", params={"category": "movies_series"}).json()
    assert filtered["total"] == 1
    assert filtered["items"][0]["category"] == "movies_series"

    text_search = client.get("/links", params={"q": "برمجة"}).json()
    assert text_search["total"] == 1
    assert "book.pdf" in text_search["items"][0]["url"]


def test_stats_counts_by_category(client: TestClient):
    register_workspace(client, email="stats@example.com", workspace_name="Stats Co")
    workspace_id = client.get("/auth/me").json()["workspace_id"]
    channel = client.post("/channels", json={"tg_channel_id": "2", "username": "c2"}).json()

    _seed_link(
        channel_id=channel["id"],
        workspace_id=workspace_id,
        message_id=1,
        url="https://example.com/a.mp4",
        category="movies_series",
        raw_text="x",
    )
    _seed_link(
        channel_id=channel["id"],
        workspace_id=workspace_id,
        message_id=2,
        url="https://example.com/b.mp4",
        category="movies_series",
        raw_text="y",
    )
    _seed_link(
        channel_id=channel["id"],
        workspace_id=workspace_id,
        message_id=3,
        url="https://example.com/c.apk",
        category="software_apps",
        raw_text="z",
    )

    stats = client.get("/links/stats").json()
    assert stats["total_links"] == 3
    assert stats["by_category"]["movies_series"] == 2
    assert stats["by_category"]["software_apps"] == 1


def test_pagination(client: TestClient):
    register_workspace(client, email="page@example.com", workspace_name="Page Co")
    workspace_id = client.get("/auth/me").json()["workspace_id"]
    channel = client.post("/channels", json={"tg_channel_id": "3", "username": "c3"}).json()

    for i in range(5):
        _seed_link(
            channel_id=channel["id"],
            workspace_id=workspace_id,
            message_id=i,
            url=f"https://example.com/{i}.apk",
            category="software_apps",
            raw_text="x",
        )

    page1 = client.get("/links", params={"page": 1, "page_size": 2}).json()
    page2 = client.get("/links", params={"page": 2, "page_size": 2}).json()
    assert page1["total"] == 5
    assert len(page1["items"]) == 2
    assert len(page2["items"]) == 2
    assert page1["items"] != page2["items"]
