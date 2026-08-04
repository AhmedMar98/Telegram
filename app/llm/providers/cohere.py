"""Cohere provider — Command R / Command R+ via Cohere API v2."""
from __future__ import annotations

from typing import List, Optional

import httpx
from loguru import logger

from ...config import get_settings
from .base import BaseLLMProvider, LLMResponse, QuotaInfo


class CohereProvider(BaseLLMProvider):
    name = "cohere"
    BASE_URL = "https://api.cohere.com/v2/chat"

    def __init__(self) -> None:
        s = get_settings()
        self.api_key = s.cohere_api_key
        self.model = s.cohere_model

    @property
    def is_available(self) -> bool:
        return bool(self.api_key)

    async def chat(
        self,
        messages: List[dict],
        max_tokens: int = 500,
        temperature: float = 0.2,
    ) -> Optional[LLMResponse]:
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
                return LLMResponse(text="", provider=self.name, model=self.model,
                                   error="rate_limited")
            if r.status_code != 200:
                logger.warning("[cohere] HTTP {}: {}", r.status_code, r.text[:200])
                return None
            data = r.json()
            # Cohere v2 returns {message: {content: [{text: "..."}]}}
            content = data.get("message", {}).get("content", [])
            text = content[0].get("text", "") if content else ""
            usage = data.get("usage", {})
            return LLMResponse(
                text=text,
                provider=self.name,
                model=self.model,
                tokens_in=usage.get("tokens", {}).get("input_tokens"),
                tokens_out=usage.get("tokens", {}).get("output_tokens"),
            )
        except Exception as e:
            logger.warning("[cohere] error: {}", e)
            return None
