"""
Cloudflare Workers AI provider — runs LLMs on Cloudflare's edge network.

Uses Cloudflare's REST API for Workers AI. Free tier includes 10,000
neurons/day. Supports models like llama-3.1-8b-instruct, mistral-7b-instruct.
"""

from __future__ import annotations

import httpx
from loguru import logger

from ...config import get_settings
from .base import BaseLLMProvider, LLMResponse


class CloudflareProvider(BaseLLMProvider):
    name = "cloudflare"
    # URL pattern: https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}
    BASE_URL = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"

    def __init__(self) -> None:
        s = get_settings()
        self.api_key = s.cloudflare_api_key  # CF API token
        self.account_id = s.cloudflare_account_id
        self.model = s.cloudflare_model

    @property
    def is_available(self) -> bool:
        return bool(self.api_key) and bool(self.account_id)

    async def chat(
        self,
        messages: list[dict],
        max_tokens: int = 500,
        temperature: float = 0.2,
    ) -> LLMResponse | None:
        if not self.is_available:
            return None
        url = self.BASE_URL.format(account_id=self.account_id, model=self.model)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        # CF Workers AI expects a "messages" list directly (no model field)
        payload = {
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        try:
            async with httpx.AsyncClient(timeout=45) as client:
                r = await client.post(url, headers=headers, json=payload)
            if r.status_code == 429:
                return LLMResponse(
                    text="", provider=self.name, model=self.model, error="rate_limited"
                )
            if r.status_code != 200:
                logger.warning("[cloudflare] HTTP {}: {}", r.status_code, r.text[:200])
                return None
            data = r.json()
            if not data.get("success"):
                logger.warning("[cloudflare] API error: {}", data.get("errors"))
                return None
            # Response shape: {result: {response: "the text"}}
            text = data.get("result", {}).get("response", "")
            usage = data.get("result", {}).get("usage", {})
            return LLMResponse(
                text=text or "",
                provider=self.name,
                model=self.model,
                tokens_in=usage.get("prompt_tokens"),
                tokens_out=usage.get("completion_tokens"),
            )
        except Exception as e:
            logger.warning("[cloudflare] error: {}", e)
            return None
