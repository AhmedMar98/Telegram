"""Tests for the 5 new LLM providers (v5.0)."""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.llm.providers import (
    AnyscaleProvider,
    CloudflareProvider,
    CohereProvider,
    MistralProvider,
    TogetherProvider,
)


def _mock_response(status: int, json_data: dict):
    r = MagicMock()
    r.status_code = status
    r.headers = {}
    r.json.return_value = json_data
    return r


# ============================================================
# Cohere
# ============================================================

@pytest.mark.asyncio
async def test_cohere_not_available_without_key():
    p = CohereProvider()
    p.api_key = ""
    assert p.is_available is False
    r = await p.chat([{"role": "user", "content": "hi"}])
    assert r is None


@pytest.mark.asyncio
async def test_cohere_success():
    p = CohereProvider()
    p.api_key = "test-key"
    p.model = "command-r-08-2024"
    with patch("httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post = AsyncMock(return_value=_mock_response(200, {
            "message": {"content": [{"text": "cohere-response"}]},
            "usage": {"tokens": {"input_tokens": 10, "output_tokens": 5}},
        }))
        mock_cls.return_value = client
        r = await p.chat([{"role": "user", "content": "hi"}])
    assert r is not None
    assert r.text == "cohere-response"
    assert r.provider == "cohere"
    assert r.tokens_in == 10


@pytest.mark.asyncio
async def test_cohere_rate_limited():
    p = CohereProvider()
    p.api_key = "k"
    with patch("httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post = AsyncMock(return_value=_mock_response(429, {}))
        mock_cls.return_value = client
        r = await p.chat([{"role": "user", "content": "hi"}])
    assert r is not None
    assert r.error == "rate_limited"


# ============================================================
# Mistral
# ============================================================

@pytest.mark.asyncio
async def test_mistral_success():
    p = MistralProvider()
    p.api_key = "k"
    with patch("httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post = AsyncMock(return_value=_mock_response(200, {
            "choices": [{"message": {"content": "mistral-resp"}}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 3},
        }))
        mock_cls.return_value = client
        r = await p.chat([{"role": "user", "content": "hi"}])
    assert r is not None
    assert r.text == "mistral-resp"
    assert r.provider == "mistral"


# ============================================================
# Together
# ============================================================

@pytest.mark.asyncio
async def test_together_success():
    p = TogetherProvider()
    p.api_key = "k"
    with patch("httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post = AsyncMock(return_value=_mock_response(200, {
            "choices": [{"message": {"content": "together-resp"}}],
        }))
        mock_cls.return_value = client
        r = await p.chat([{"role": "user", "content": "hi"}])
    assert r is not None
    assert r.text == "together-resp"
    assert r.provider == "together"


# ============================================================
# Anyscale
# ============================================================

@pytest.mark.asyncio
async def test_anyscale_success():
    p = AnyscaleProvider()
    p.api_key = "k"
    with patch("httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post = AsyncMock(return_value=_mock_response(200, {
            "choices": [{"message": {"content": "anyscale-resp"}}],
        }))
        mock_cls.return_value = client
        r = await p.chat([{"role": "user", "content": "hi"}])
    assert r is not None
    assert r.text == "anyscale-resp"


# ============================================================
# Cloudflare (requires both API key + account ID)
# ============================================================

@pytest.mark.asyncio
async def test_cloudflare_requires_account_id():
    p = CloudflareProvider()
    p.api_key = "k"
    p.account_id = ""
    assert p.is_available is False


@pytest.mark.asyncio
async def test_cloudflare_success():
    p = CloudflareProvider()
    p.api_key = "k"
    p.account_id = "abc123"
    p.model = "@cf/meta/llama-3.1-8b-instruct"
    with patch("httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post = AsyncMock(return_value=_mock_response(200, {
            "success": True,
            "result": {"response": "cf-resp", "usage": {"prompt_tokens": 5}},
        }))
        mock_cls.return_value = client
        r = await p.chat([{"role": "user", "content": "hi"}])
    assert r is not None
    assert r.text == "cf-resp"
    assert r.provider == "cloudflare"


@pytest.mark.asyncio
async def test_cloudflare_api_error_returns_none():
    p = CloudflareProvider()
    p.api_key = "k"
    p.account_id = "abc"
    with patch("httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post = AsyncMock(return_value=_mock_response(200, {
            "success": False,
            "errors": [{"message": "model not found"}],
        }))
        mock_cls.return_value = client
        r = await p.chat([{"role": "user", "content": "hi"}])
    assert r is None


# ============================================================
# Orchestrator integration — all 11 providers (v5.1)
# ============================================================

def test_orchestrator_default_priority_has_11_providers():
    """v5.1 orchestrator should know about all 11 providers in priority order."""
    from app.llm.orchestrator import DEFAULT_PRIORITY
    assert len(DEFAULT_PRIORITY) == 11
    # v5.0 providers
    assert "cohere" in DEFAULT_PRIORITY
    assert "mistral" in DEFAULT_PRIORITY
    assert "together" in DEFAULT_PRIORITY
    assert "anyscale" in DEFAULT_PRIORITY
    assert "cloudflare" in DEFAULT_PRIORITY
    # v5.1 addition
    assert "nvidia" in DEFAULT_PRIORITY


def test_orchestrator_instantiates_all_providers():
    from app.llm.orchestrator import LLMOrchestrator
    orch = LLMOrchestrator()
    # All 11 provider classes should be instantiated (even if not configured)
    assert len(orch.providers) == 11
    for name in ["groq", "gemini", "cloudflare", "nvidia", "openrouter",
                 "mistral", "together", "anyscale", "cohere",
                 "huggingface", "zai"]:
        assert name in orch.providers
