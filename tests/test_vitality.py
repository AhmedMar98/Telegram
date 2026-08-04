"""Tests for the vitality checker."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.vitality.checker import VitalityChecker, VitalityResult


@pytest.fixture
def checker():
    return VitalityChecker(delay_min=0, delay_max=0, timeout=2.0)


def _mock_response(status: int, url: str = "https://example.com"):
    r = MagicMock()
    r.status_code = status
    r.url = httpx.URL(url)
    r.headers = {}
    return r


@pytest.mark.asyncio
async def test_check_url_alive(checker):
    """200 status → alive=True."""
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.head = AsyncMock(return_value=_mock_response(200))
        mock_client_cls.return_value = mock_client

        result = await checker.check_url("https://example.com")
        assert result.alive is True
        assert result.http_status == 200


@pytest.mark.asyncio
async def test_check_url_dead(checker):
    """404 status → alive=False."""
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.head = AsyncMock(return_value=_mock_response(404))
        mock_client_cls.return_value = mock_client

        result = await checker.check_url("https://example.com/missing")
        assert result.alive is False
        assert result.http_status == 404


@pytest.mark.asyncio
async def test_check_url_timeout(checker):
    """Timeout → alive=False, error='timeout'."""
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.head = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
        mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
        mock_client_cls.return_value = mock_client

        result = await checker.check_url("https://slow.example.com")
        assert result.alive is False
        assert result.error == "timeout"


@pytest.mark.asyncio
async def test_check_url_empty():
    """Empty URL → immediate failure."""
    checker = VitalityChecker()
    result = await checker.check_url("")
    assert result.alive is False
    assert result.error == "empty_url"


@pytest.mark.asyncio
async def test_check_url_falls_back_to_get(checker):
    """HEAD with 405 should fall back to GET."""
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.head = AsyncMock(return_value=_mock_response(405))
        mock_client.get = AsyncMock(return_value=_mock_response(200))
        mock_client_cls.return_value = mock_client

        result = await checker.check_url("https://example.com")
        assert result.alive is True
        assert result.http_status == 200
        mock_client.get.assert_awaited()
