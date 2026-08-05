"""Common base class + types for LLM providers."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class QuotaInfo:
    """Quota info parsed from provider response headers."""
    limit: int | None = None
    remaining: int | None = None
    reset_at: datetime | None = None
    # Raw header dict for debugging
    raw_headers: dict = field(default_factory=dict)


@dataclass
class LLMResponse:
    """Normalized response from any LLM provider."""
    text: str
    provider: str
    model: str
    quota: QuotaInfo | None = None
    latency_ms: int | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    error: str | None = None


class BaseLLMProvider(ABC):
    """All providers implement this interface."""

    name: str = "base"
    is_async: bool = True

    @property
    @abstractmethod
    def is_available(self) -> bool:
        """True if this provider has credentials configured."""
        ...

    @abstractmethod
    async def chat(
        self,
        messages: list[dict],
        max_tokens: int = 500,
        temperature: float = 0.2,
    ) -> LLMResponse | None:
        """Send a chat completion request. Returns None on failure."""
        ...

    async def health_check(self) -> bool:
        """Lightweight connectivity test."""
        if not self.is_available:
            return False
        try:
            r = await self.chat(
                [{"role": "user", "content": "ping"}],
                max_tokens=5,
            )
            return r is not None and not r.error
        except Exception:
            return False
