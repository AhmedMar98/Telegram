"""Tests for Semantic Memory (v4.1)."""

import pytest

from app.classifier.semantic_memory import SemanticMemory, _cosine
from app.models import SemanticCache


@pytest.fixture(autouse=True)
def setup_db(db_session):
    """Use the shared in-memory DB session."""
    # Patch SessionLocal to return db_session for the duration of the test
    import app.classifier.semantic_memory as sm

    original = sm.SessionLocal
    sm.SessionLocal = lambda: _CtxMgr(db_session)
    yield
    sm.SessionLocal = original


class _CtxMgr:
    """Wrap a session so it can be used as a context manager."""

    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, *args):
        return False


def test_cosine_identical_vectors():
    a = [1.0, 0.0, 0.0]
    assert _cosine(a, a) == pytest.approx(1.0)


def test_cosine_orthogonal_vectors():
    a = [1.0, 0.0]
    b = [0.0, 1.0]
    assert _cosine(a, b) == pytest.approx(0.0)


def test_cosine_opposite_vectors():
    a = [1.0, 0.0]
    b = [-1.0, 0.0]
    assert _cosine(a, b) == pytest.approx(-1.0)


def test_cosine_different_lengths_returns_zero():
    assert _cosine([1.0, 2.0], [1.0]) == 0.0


def test_cosine_empty_returns_zero():
    assert _cosine([], []) == 0.0


def test_find_similar_returns_none_when_cache_empty():
    mem = SemanticMemory(threshold=0.5)
    hit = mem.find_similar([1.0, 0.0, 0.0])
    assert hit is None


def test_store_and_find_similar_hits_above_threshold(db_session):
    mem = SemanticMemory(threshold=0.5)
    # Store a vector
    vec = [1.0, 0.0, 0.0]
    mem.store(
        url="https://t.me/joinchat/abc",
        url_hash="hash_abc",
        embedding=vec,
        category="telegram",
        tier="rules",
        confidence=0.98,
    )
    # Query with an identical vector → should hit
    hit = mem.find_similar(vec)
    assert hit is not None
    assert hit.category == "telegram"
    assert hit.tier == "rules"
    assert hit.confidence == 0.98
    assert hit.similarity == pytest.approx(1.0, abs=1e-6)


def test_find_similar_misses_below_threshold(db_session):
    mem = SemanticMemory(threshold=0.99)  # very high bar
    vec = [1.0, 0.0, 0.0]
    mem.store(
        url="https://example.com",
        url_hash="h1",
        embedding=vec,
        category="other",
        tier="rules",
        confidence=0.5,
    )
    # Slightly different vector → sim < 0.99
    query = [0.95, 0.31, 0.0]
    hit = mem.find_similar(query)
    assert hit is None


def test_find_similar_increments_used_count(db_session):
    mem = SemanticMemory(threshold=0.5)
    vec = [1.0, 0.0, 0.0]
    mem.store(
        url="https://a.com",
        url_hash="ha",
        embedding=vec,
        category="other",
        tier="rules",
        confidence=0.5,
    )
    mem.find_similar(vec)
    mem.find_similar(vec)
    # Cache row should have used_count=2
    row = db_session.query(SemanticCache).filter_by(url_hash="ha").first()
    assert row is not None
    assert row.used_count == 2


def test_store_updates_existing_url(db_session):
    mem = SemanticMemory()
    vec1 = [1.0, 0.0]
    vec2 = [0.5, 0.5]
    mem.store(
        url="https://x.com",
        url_hash="hx",
        embedding=vec1,
        category="other",
        tier="rules",
        confidence=0.5,
    )
    mem.store(
        url="https://x.com",
        url_hash="hx",
        embedding=vec2,
        category="tool",
        tier="llm",
        confidence=0.9,
    )
    rows = db_session.query(SemanticCache).filter_by(url_hash="hx").all()
    assert len(rows) == 1  # updated, not duplicated
    assert rows[0].category == "tool"
    assert rows[0].confidence == 0.9


def test_stats_empty_cache():
    mem = SemanticMemory()
    s = mem.stats()
    assert s["cache_entries"] == 0
    assert s["total_reuses"] == 0
    assert s["avg_reuse_per_entry"] == 0.0
