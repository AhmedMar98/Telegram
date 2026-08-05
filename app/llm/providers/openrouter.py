"""OpenRouter provider — unified gateway for many free models."""
from __future__ import annotations

import httpx
from loguru import logger

from ...config import get_settings
from .base import BaseLLMProvider, LLMResponse, QuotaInfo


class OpenRouterProvider(BaseLLMProvider):
    name = "openrouter"
    BASE_URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self) -> None:
        s = get_settings()
        self.api_key = s.openrouter_api_key
        self.model = s.openrouter_model

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
            # OpenRouter requires these for ranking
            "HTTP-Referer": "https://github.com/AhmedMar98/Telegram",
            "X-Title": "Link Intelligence Platform",
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
            if r.status_code != 200:
                logger.warning("[openrouter] HTTP {}: {}",
                               r.status_code, r.text[:200])
                return LLMResponse(
                    text="", provider=self.name, model=self.model,
                    quota=quota, error=f"HTTP {r.status_code}: {r.text[:200]}",
                )
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
            logger.warning("[openrouter] error: {}", e)
            return None

    @staticmethod
    def _parse_quota(headers) -> QuotaInfo | None:
        """OpenRouter returns rate-limit headers."""
        try:
            return QuotaInfo(
                limit=_int_or_none(headers.get("x-or-rate-limit-limit")),
                remaining=_int_or_none(headers.get("x-or-rate-limit-remaining")),
                raw_headers=dict(headers),
            )
        except Exception:
            return None


def _int_or_none(v) -> int | None:
    if v is None:
        return None
    try:
        return int(str(v).split("/")[0])
    except (ValueError, IndexError):
        return None
