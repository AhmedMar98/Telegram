"""Tests for the Daily Summary worker (v4.1)."""
from datetime import datetime

import pytest

from app.models import DailySummary, Link, LinkStatus
from app.workers.summary_worker import DailySummaryWorker


@pytest.fixture(autouse=True)
def patch_session_local(db_session):
    """Patch SessionLocal in summary_worker to use the test session."""
    import app.workers.summary_worker as sw
    original = sw.SessionLocal
    sw.SessionLocal = lambda: _Ctx(db_session)
    yield
    sw.SessionLocal = original


class _Ctx:
    def __init__(self, session):
        self.session = session
    def __enter__(self):
        return self.session
    def __exit__(self, *args):
        return False


@pytest.mark.asyncio
async def test_generate_summary_for_empty_day(db_session):
    worker = DailySummaryWorker(run_hour=0, run_minute=0)
    summary = await worker.generate_and_store(date_str="2026-01-01")
    assert summary.total_new == 0
    assert summary.total_alive == 0
    assert summary.total_dead == 0
    assert summary.top_links_json == "[]"
    assert summary.category_breakdown_json == "{}"


@pytest.mark.asyncio
async def test_generate_summary_counts_new_links(db_session):
    """Summary should count links discovered on the given date."""
    target_date = datetime(2026, 1, 1, 12, 0, 0)
    # Add 3 links on 2026-01-01
    db_session.add_all([
        Link(url="https://a.com", url_hash="h1", category="telegram",
             status=LinkStatus.ALIVE.value, alive=True,
             discovered_at=target_date, click_count=5),
        Link(url="https://b.com", url_hash="h2", category="file",
             status=LinkStatus.DEAD.value, alive=False,
             discovered_at=target_date, click_count=2),
        Link(url="https://c.com", url_hash="h3", category="telegram",
             status=LinkStatus.NEW.value, alive=None,
             discovered_at=target_date, click_count=10),
    ])
    # And one link on a different day (should NOT be counted)
    db_session.add(Link(
        url="https://other.com", url_hash="h4", category="news",
        status=LinkStatus.NEW.value, discovered_at=datetime(2026, 2, 1),
    ))
    db_session.commit()

    worker = DailySummaryWorker()
    summary = await worker.generate_and_store(date_str="2026-01-01")

    assert summary.total_new == 3
    assert summary.total_alive == 1
    assert summary.total_dead == 1


@pytest.mark.asyncio
async def test_generate_summary_top_links_sorted_by_engagement(db_session):
    """Top links should be ordered by clicks + search_hits desc."""
    target_date = datetime(2026, 1, 1, 12, 0, 0)
    db_session.add_all([
        Link(url="https://low.com", url_hash="hl", category="other",
             status=LinkStatus.NEW.value, discovered_at=target_date,
             click_count=1, search_hit_count=0),
        Link(url="https://high.com", url_hash="hh", category="telegram",
             status=LinkStatus.NEW.value, discovered_at=target_date,
             click_count=50, search_hit_count=10),
        Link(url="https://mid.com", url_hash="hm", category="file",
             status=LinkStatus.NEW.value, discovered_at=target_date,
             click_count=5, search_hit_count=5),
    ])
    db_session.commit()

    import json
    worker = DailySummaryWorker()
    summary = await worker.generate_and_store(date_str="2026-01-01")
    top = json.loads(summary.top_links_json)
    assert len(top) == 3
    assert top[0]["url"] == "https://high.com"  # highest engagement first
    assert top[1]["url"] == "https://mid.com"
    assert top[2]["url"] == "https://low.com"


@pytest.mark.asyncio
async def test_generate_summary_upserts_existing(db_session):
    """Running twice for the same date should update, not duplicate."""
    worker = DailySummaryWorker()
    s1 = await worker.generate_and_store(date_str="2026-01-01")
    s2 = await worker.generate_and_store(date_str="2026-01-01")

    rows = db_session.query(DailySummary).filter_by(summary_date="2026-01-01").all()
    assert len(rows) == 1
    assert s1.id == s2.id


@pytest.mark.asyncio
async def test_format_for_bot_renders_html(db_session):
    worker = DailySummaryWorker()
    summary = await worker.generate_and_store(date_str="2026-01-01")
    text = worker.format_for_bot(summary)
    assert "Daily Summary" in text
    assert "2026-01-01" in text


def test_seconds_until_next_run_returns_positive_value():
    worker = DailySummaryWorker(run_hour=0, run_minute=0)
    sec = worker._seconds_until_next_run()
    assert sec > 0
    assert sec <= 86400  # at most 24 hours
