"""Mistral AI provider — OpenAI-compatible API."""

from __future__ import annotations

import httpx
from loguru import logger

from ...config import get_settings
from .base import BaseLLMProvider, LLMResponse


class MistralProvider(BaseLLMProvider):
    name = "mistral"
    BASE_URL = "https://api.mistral.ai/v1/chat/completions"

    def __init__(self) -> None:
        s = get_settings()
        self.api_key = s.mistral_api_key
        self.model = s.mistral_model

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
            if r.status_code == 429:
                return LLMResponse(
                    text="", provider=self.name, model=self.model, error="rate_limited"
                )
            if r.status_code != 200:
                logger.warning("[mistral] HTTP {}: {}", r.status_code, r.text[:200])
                return None
            data = r.json()
            text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = data.get("usage", {})
            return LLMResponse(
                text=text or "",
                provider=self.name,
                model=self.model,
                tokens_in=usage.get("prompt_tokens"),
                tokens_out=usage.get("completion_tokens"),
            )
        except Exception as e:
            logger.warning("[mistral] error: {}", e)
            return None
