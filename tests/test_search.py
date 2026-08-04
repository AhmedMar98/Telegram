"""Tests for the search module (text + semantic)."""
import asyncio
import json

import pytest

from app.database import SessionLocal, engine
from app.models import Base, Link, LinkEmbedding, LinkStatus
from app.search.hybrid import HybridSearch


@pytest.fixture(autouse=True)
def setup_db():
    """Fresh DB per test."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


def _seed_links():
    with SessionLocal() as db:
        db.add_all([
            Link(
                url="https://t.me/joinchat/python_devs",
                url_hash="h1", title="Python developers group",
                description="Telegram group for Python devs",
                category="telegram", status=LinkStatus.ALIVE.value,
                alive=True, click_count=10,
            ),
            Link(
                url="https://example.com/python-tutorial.pdf",
                url_hash="h2", title="Python tutorial PDF",
                description="Learn Python from scratch",
                category="file", status=LinkStatus.ALIVE.value,
                alive=True, click_count=5,
            ),
            Link(
                url="https://github.com/user/python-repo",
                url_hash="h3", title="Awesome Python repo",
                description="Curated list of Python packages",
                category="tool", status=LinkStatus.ALIVE.value,
                alive=True, click_count=20,
            ),
            Link(
                url="https://example.com/unrelated-news",
                url_hash="h4", title="Breaking news today",
                description="Latest headlines from around the world",
                category="news", status=LinkStatus.ALIVE.value,
                alive=True,
            ),
        ])
        db.commit()


def _run_async(coro):
    """Run a coroutine with a fresh event loop (Python 3.12 compatible)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_fts_search_finds_python_links():
    _seed_links()
    s = HybridSearch(llm_orchestrator=None)
    s.init_fts()
    results = _run_async(s.search("python", limit=10))
    titles = [r.title for r in results]
    assert any("Python" in t for t in titles), f"Expected Python in results, got {titles}"


def test_search_empty_query_returns_empty():
    s = HybridSearch(llm_orchestrator=None)
    results = _run_async(s.search("", limit=10))
    assert results == []


def test_search_with_category_filter():
    _seed_links()
    s = HybridSearch(llm_orchestrator=None)
    s.init_fts()
    results = _run_async(s.search("python", category="telegram", limit=10))
    for r in results:
        assert r.category == "telegram"


def test_search_alive_only():
    _seed_links()
    # Mark one link as dead
    with SessionLocal() as db:
        link = db.query(Link).filter(Link.url_hash == "h1").first()
        link.alive = False
        db.commit()

    s = HybridSearch(llm_orchestrator=None)
    s.init_fts()
    results = _run_async(s.search("python", alive_only=True, limit=10))
    for r in results:
        assert r.alive is True


def test_search_result_has_score():
    _seed_links()
    s = HybridSearch(llm_orchestrator=None)
    s.init_fts()
    results = _run_async(s.search("python", limit=5))
    assert len(results) > 0
    for r in results:
        assert 0.0 <= r.score <= 1.0
        assert "fts" in r.score_breakdown
