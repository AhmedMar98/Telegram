"""Tests for the NVIDIA Integrate API provider (v5.1)."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.llm.providers import NVIDIAProvider
from app.llm.providers.nvidia import NVIDIAProvider as NVIDIADirect


def _mock_response(status: int, json_data: dict):
    r = MagicMock()
    r.status_code = status
    r.headers = {}
    r.json.return_value = json_data
    return r


@pytest.mark.asyncio
async def test_nvidia_not_available_without_key():
    p = NVIDIAProvider()
    p.api_key = ""
    assert p.is_available is False
    r = await p.chat([{"role": "user", "content": "hi"}])
    assert r is None


@pytest.mark.asyncio
async def test_nvidia_success():
    p = NVIDIAProvider()
    p.api_key = "nvapi-test-key"
    p.model = "mistralai/mistral-nemotron"
    with patch("httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post = AsyncMock(
            return_value=_mock_response(
                200,
                {
                    "choices": [{"message": {"content": "telegram"}}],
                    "usage": {"prompt_tokens": 15, "completion_tokens": 1},
                    "model": "mistralai/mistral-nemotron",
                },
            )
        )
        mock_cls.return_value = client
        r = await p.chat([{"role": "user", "content": "classify"}])
    assert r is not None
    assert r.text == "telegram"
    assert r.provider == "nvidia"
    assert r.model == "mistralai/mistral-nemotron"
    assert r.tokens_in == 15
    assert r.tokens_out == 1


@pytest.mark.asyncio
async def test_nvidia_rate_limited_returns_rate_limited_error():
    p = NVIDIAProvider()
    p.api_key = "k"
    with patch("httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post = AsyncMock(return_value=_mock_response(429, {}))
        mock_cls.return_value = client
        r = await p.chat([{"role": "user", "content": "hi"}])
    assert r is not None
    assert r.error == "rate_limited"


@pytest.mark.asyncio
async def test_nvidia_401_returns_none():
    p = NVIDIAProvider()
    p.api_key = "invalid-key"
    with patch("httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post = AsyncMock(return_value=_mock_response(401, {"detail": "Invalid API key"}))
        mock_cls.return_value = client
        r = await p.chat([{"role": "user", "content": "hi"}])
    assert r is None


@pytest.mark.asyncio
async def test_nvidia_500_returns_none():
    p = NVIDIAProvider()
    p.api_key = "k"
    with patch("httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post = AsyncMock(return_value=_mock_response(500, {"error": "server"}))
        mock_cls.return_value = client
        r = await p.chat([{"role": "user", "content": "hi"}])
    assert r is None


@pytest.mark.asyncio
async def test_nvidia_timeout_returns_none():
    p = NVIDIAProvider()
    p.api_key = "k"
    with patch("httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
        mock_cls.return_value = client
        r = await p.chat([{"role": "user", "content": "hi"}])
    assert r is None


@pytest.mark.asyncio
async def test_nvidia_thinking_mode_appends_chat_template_kwargs():
    """When model ends with ':thinking', the payload should include chat_template_kwargs."""
    p = NVIDIAProvider()
    p.api_key = "k"
    p.model = "deepseek-ai/deepseek-v4-pro:thinking"

    captured_payload = {}

    async def fake_post(url, headers=None, json=None):
        captured_payload.update(json)
        return _mock_response(
            200,
            {
                "choices": [{"message": {"content": "answer"}}],
            },
        )

    with patch("httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post = fake_post
        mock_cls.return_value = client
        r = await p.chat([{"role": "user", "content": "hi"}])

    assert r is not None
    # The model name should have the :thinking suffix stripped
    assert captured_payload["model"] == "deepseek-ai/deepseek-v4-pro"
    # And chat_template_kwargs.thinking should be True
    assert captured_payload.get("chat_template_kwargs") == {"thinking": True}


def test_orchestrator_priority_includes_nvidia():
    """v5.1 orchestrator should have nvidia in DEFAULT_PRIORITY."""
    from app.llm.orchestrator import DEFAULT_PRIORITY

    assert "nvidia" in DEFAULT_PRIORITY
    # Total should be 11 providers now
    assert len(DEFAULT_PRIORITY) == 11


def test_orchestrator_instantiates_nvidia():
    from app.llm.orchestrator import LLMOrchestrator

    orch = LLMOrchestrator()
    assert "nvidia" in orch.providers
    assert isinstance(orch.providers["nvidia"], NVIDIAProvider)


def test_nvidia_provider_class_imported_from_providers_package():
    """NVIDIAProvider should be importable from the providers package."""
    from app.llm.providers import NVIDIAProvider as ImportedNVIDIA

    assert ImportedNVIDIA is NVIDIADirect
