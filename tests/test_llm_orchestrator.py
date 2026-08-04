"""Tests for the LLM orchestrator failover logic."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.llm.orchestrator import LLMOrchestrator
from app.llm.providers import (
    BaseLLMProvider,
    LLMResponse,
    QuotaInfo,
)


class _FakeProvider(BaseLLMProvider):
    """Configurable fake provider for testing."""

    def __init__(self, name: str, responses: list, available: bool = True):
        self.name = name
        self._responses = responses
        self._available = available
        self.calls = 0

    @property
    def is_available(self) -> bool:
        return self._available

    async def chat(self, messages, max_tokens=500, temperature=0.2):
        if self.calls >= len(self._responses):
            return None
        resp = self._responses[self.calls]
        self.calls += 1
        if isinstance(resp, Exception):
            raise resp
        return resp


def _ok(text: str, provider: str = "fake") -> LLMResponse:
    return LLMResponse(text=text, provider=provider, model="fake-model")


def _rate_limited(provider: str = "fake") -> LLMResponse:
    return LLMResponse(text="", provider=provider, model="m", error="rate_limited")


@pytest.mark.asyncio
async def test_first_provider_success():
    p1 = _FakeProvider("groq", [_ok("answer-1", "groq")])
    orch = LLMOrchestrator(providers=[p1])
    r = await orch.chat([{"role": "user", "content": "hi"}])
    assert r is not None
    assert r.text == "answer-1"
    assert r.provider == "groq"


@pytest.mark.asyncio
async def test_failover_on_rate_limit():
    """If first provider rate-limits, fall through to second."""
    p1 = _FakeProvider("groq", [_rate_limited("groq")])
    p2 = _FakeProvider("gemini", [_ok("answer-2", "gemini")])
    orch = LLMOrchestrator(providers=[p1, p2])
    r = await orch.chat([{"role": "user", "content": "hi"}])
    assert r is not None
    assert r.text == "answer-2"
    assert r.provider == "gemini"
    assert p1.calls == 1
    assert p2.calls == 1


@pytest.mark.asyncio
async def test_failover_on_exception():
    p1 = _FakeProvider("groq", [RuntimeError("network error")])
    p2 = _FakeProvider("gemini", [_ok("fallback", "gemini")])
    orch = LLMOrchestrator(providers=[p1, p2])
    r = await orch.chat([{"role": "user", "content": "hi"}])
    assert r is not None
    assert r.provider == "gemini"


@pytest.mark.asyncio
async def test_all_providers_fail_returns_none():
    p1 = _FakeProvider("groq", [_rate_limited("groq")])
    p2 = _FakeProvider("gemini", [None])
    orch = LLMOrchestrator(providers=[p1, p2])
    r = await orch.chat([{"role": "user", "content": "hi"}])
    assert r is None


@pytest.mark.asyncio
async def test_unavailable_providers_skipped():
    """Providers without credentials are not tried."""
    p1 = _FakeProvider("groq", [], available=False)
    p2 = _FakeProvider("gemini", [_ok("ok", "gemini")])
    orch = LLMOrchestrator(providers=[p1, p2])
    r = await orch.chat([{"role": "user", "content": "hi"}])
    assert r is not None
    assert r.provider == "gemini"
    assert p1.calls == 0  # not called


def test_priority_order():
    p1 = _FakeProvider("groq", [])
    p2 = _FakeProvider("gemini", [])
    p3 = _FakeProvider("zai", [])
    orch = LLMOrchestrator(providers=[p3, p1, p2])
    # DEFAULT_PRIORITY is [groq, gemini, openrouter, huggingface, zai]
    assert orch.priority == ["groq", "gemini", "zai"]
