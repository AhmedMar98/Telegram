"""Groq provider — fastest free inference (LPU), OpenAI-compatible API."""
from __future__ import annotations

import httpx
from loguru import logger

from ...config import get_settings
from .base import BaseLLMProvider, LLMResponse, QuotaInfo


class GroqProvider(BaseLLMProvider):
    name = "groq"
    BASE_URL = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self) -> None:
        s = get_settings()
        self.api_key = s.groq_api_key
        self.model = s.groq_model

    @property
    def is_available(self) -> bool:
        return bool(self.api_key)

    async def chat(
        self,
        messages: list[dict],
        max_tokens: int = 500,
        temperature: float = 0.2,
    ) -> LLMResponse | None:
        if not self.is_available:
            return None
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(self.BASE_URL, headers=headers, json=payload)
            quota = self._parse_quota(r.headers)
            if r.status_code == 429:
                # Rate limit — caller should failover
                logger.warning("[groq] rate limited")
                return LLMResponse(
                    text="", provider=self.name, model=self.model,
                    quota=quota, error="rate_limited",
                )
            if r.status_code != 200:
                logger.warning("[groq] HTTP {}: {}", r.status_code, r.text[:200])
                return None
            data = r.json()
            text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = data.get("usage", {})
            return LLMResponse(
                text=text or "",
                provider=self.name,
                model=self.model,
                quota=quota,
                tokens_in=usage.get("prompt_tokens"),
                tokens_out=usage.get("completion_tokens"),
            )
        except Exception as e:
            logger.warning("[groq] error: {}", e)
            return None

    @staticmethod
    def _parse_quota(headers) -> QuotaInfo | None:
        """Groq returns x-ratelimit-* headers."""
        try:
            return QuotaInfo(
                limit=_int(headers.get("x-ratelimit-limit-requests")),
                remaining=_int(headers.get("x-ratelimit-remaining-requests")),
                raw_headers=dict(headers),
            )
        except Exception:
            return None


def _int(v) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
