"""
NVIDIA Integrate API provider — OpenAI-compatible endpoint.

Supports NVIDIA-hosted open-source models including:
    - z-ai/glm-5.2 (Z.AI GLM 5.2 — streaming-friendly, 16K output)
    - deepseek-ai/deepseek-v4-pro (DeepSeek V4 Pro with thinking mode)
    - thinkingmachines/inkling
    - meta/llama-3.3-70b-instruct
    - nvidia/llama-3.1-nemotron-70b-instruct
    - mistralai/mistral-nemotron

Endpoint: https://integrate.api.nvidia.com/v1
Auth: Bearer nvapi-<key>

Get a key from: https://build.nvidia.com/ → "Get API Key"
"""
from __future__ import annotations

import httpx
from loguru import logger

from ...config import get_settings
from .base import BaseLLMProvider, LLMResponse


class NVIDIAProvider(BaseLLMProvider):
    name = "nvidia"
    BASE_URL = "https://integrate.api.nvidia.com/v1/chat/completions"

    def __init__(self) -> None:
        s = get_settings()
        self.api_key = s.nvidia_api_key
        self.model = s.nvidia_model

    @property
    def is_available(self) -> bool:
        return bool(self.api_key)

    async def chat(
        self,
        messages: list[dict],
        max_tokens: int = 1024,
        temperature: float = 0.6,
    ) -> LLMResponse | None:
        """
        Send a chat completion to NVIDIA Integrate API.

        Note: NVIDIA recommends temperature=0.6 to 1.0 for their hosted models
        (different from OpenAI's default 0.2).
        """
        if not self.is_available:
            return None
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": 0.95,
            "stream": False,
        }
        # Some NVIDIA models (e.g. deepseek-v4-pro) support a "thinking" toggle
        # via extra_body.chat_template_kwargs.thinking — disabled by default.
        # We expose this via the model name: if model contains "thinking" suffix
        # (e.g. "deepseek-ai/deepseek-v4-pro:thinking"), we enable it.
        if ":thinking" in self.model:
            payload["model"] = self.model.replace(":thinking", "")
            payload["chat_template_kwargs"] = {"thinking": True}

        try:
            # NVIDIA responses can be slow on cold-start — use 90s timeout
            async with httpx.AsyncClient(timeout=90) as client:
                r = await client.post(self.BASE_URL, headers=headers, json=payload)
            if r.status_code == 429:
                return LLMResponse(text="", provider=self.name, model=self.model,
                                   error="rate_limited")
            if r.status_code == 401:
                logger.warning("[nvidia] invalid API key")
                return None
            if r.status_code != 200:
                logger.warning("[nvidia] HTTP {}: {}", r.status_code, r.text[:200])
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
        except httpx.TimeoutException:
            logger.warning("[nvidia] request timed out (model may be cold-starting)")
            return None
        except Exception as e:
            logger.warning("[nvidia] error: {}", e)
            return None
