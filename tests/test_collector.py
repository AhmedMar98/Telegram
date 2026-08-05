"""Tests for the mock collector + URL extraction."""

import pytest

from app.collectors.base import BaseCollector
from app.collectors.mock_collector import MockCollector


def test_extract_urls_basic():
    urls = list(BaseCollector.extract_urls(
        "Check https://example.com and https://t.me/joinchat/abc now"
    ))
    assert "https://example.com" in urls
    assert "https://t.me/joinchat/abc" in urls
    assert len(urls) == 2


def test_extract_urls_dedup():
    urls = list(BaseCollector.extract_urls(
        "https://example.com https://example.com"
    ))
    assert urls == ["https://example.com"]


def test_extract_urls_bare_tme():
    """t.me/xxx without protocol is normalized to https://t.me/xxx."""
    urls = list(BaseCollector.extract_urls("join t.me/mychannel now"))
    assert "https://t.me/mychannel" in urls


def test_extract_urls_strips_trailing_punctuation():
    urls = list(BaseCollector.extract_urls("see (https://example.com/path),"))
    assert urls == ["https://example.com/path"]


def test_extract_urls_empty():
    assert list(BaseCollector.extract_urls("")) == []
    assert list(BaseCollector.extract_urls(None)) == []


@pytest.mark.asyncio
async def test_mock_collector_yields_links():
    """MockCollector should produce LinkExtraction objects."""
    collector = MockCollector()
    links = []
    async for link in collector.collect_links("test_channel", last_message_id=0):
        links.append(link)
        if len(links) >= 3:
            collector.stop()
            break
    assert len(links) >= 1
    assert all(link.url.startswith("http") for link in links)
    assert all(link.source_channel == "test_channel" for link in links)


@pytest.mark.asyncio
async def test_mock_collector_respects_stop():
    """Stop signal halts iteration."""
    collector = MockCollector()
    collector.stop()
    links = []
    async for link in collector.collect_links("test"):
        links.append(link)
    # Should yield nothing or very few before stopping
    assert len(links) <= 5


def test_make_collector_returns_mock_when_no_credentials():
    """With TG_MOCK_MODE=true, factory returns MockCollector."""
    from app.collectors.base import make_collector
    c = make_collector()
    from app.collectors.mock_collector import MockCollector
    assert isinstance(c, MockCollector)
