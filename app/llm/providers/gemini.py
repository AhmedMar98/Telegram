"""Google Gemini provider via the google-generativeai SDK."""

from __future__ import annotations

from loguru import logger

from ...config import get_settings
from .base import BaseLLMProvider, LLMResponse


class GeminiProvider(BaseLLMProvider):
    name = "gemini"

    def __init__(self) -> None:
        s = get_settings()
        self.api_key = s.gemini_api_key
        self.model = s.gemini_model
        self._client = None

    @property
    def is_available(self) -> bool:
        return bool(self.api_key)

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        try:
            import google.generativeai as genai

            genai.configure(api_key=self.api_key)
            self._client = genai.GenerativeModel(self.model)
        except ImportError:
            logger.warning("[gemini] google-generativeai not installed")
            return None
        except Exception as e:
            logger.warning("[gemini] init failed: {}", e)
            return None
        return self._client

    async def chat(
        self,
        messages: list[dict],
        max_tokens: int = 500,
        temperature: float = 0.2,
    ) -> LLMResponse | None:
        if not self.is_available:
            return None
        client = self._ensure_client()
        if client is None:
            return None

        # Convert OpenAI-style messages to a single prompt
        system_parts = [m["content"] for m in messages if m["role"] == "system"]
        user_parts = [m["content"] for m in messages if m["role"] == "user"]
        prompt = "\n\n".join(system_parts + user_parts)

        try:
            # SDK is synchronous — run in thread pool
            import anyio

            response = await anyio.to_thread.run_sync(
                lambda: client.generate_content(
                    prompt,
                    generation_config={
                        "max_output_tokens": max_tokens,
                        "temperature": temperature,
                    },
                )
            )
            text = response.text or ""
            usage = getattr(response, "usage_metadata", None)
            tokens_in = getattr(usage, "prompt_token_count", None) if usage else None
            tokens_out = getattr(usage, "candidates_token_count", None) if usage else None
            return LLMResponse(
                text=text,
                provider=self.name,
                model=self.model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )
        except Exception as e:
            logger.warning("[gemini] error: {}", e)
            return None
