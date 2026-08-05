"""
Z.AI provider — uses the z-ai-web-dev-sdk when available, falls back to HTTP.

Tries (in order):
    1. Node CLI: z-ai-web-dev-sdk (if installed via npm)
    2. Direct HTTP API (if ZAI_API_KEY is set)
"""
from __future__ import annotations

import asyncio
import json
import shutil

from loguru import logger

from ...config import get_settings
from .base import BaseLLMProvider, LLMResponse


class ZAIProvider(BaseLLMProvider):
    name = "zai"

    def __init__(self) -> None:
        s = get_settings()
        self.api_key = s.zai_api_key
        self.model = s.zai_model
        self._cli_path = shutil.which("z-ai") or shutil.which("zai")

    @property
    def is_available(self) -> bool:
        return bool(self.api_key) or self._cli_path is not None

    async def chat(
        self,
        messages: list[dict],
        max_tokens: int = 500,
        temperature: float = 0.2,
    ) -> LLMResponse | None:
        # Prefer CLI (works in environments with z-ai-web-dev-sdk installed)
        if self._cli_path:
            return await self._chat_via_cli(messages, max_tokens, temperature)
        # Fallback to HTTP
        if self.api_key:
            return await self._chat_via_http(messages, max_tokens, temperature)
        return None

    async def _chat_via_cli(
        self, messages: list[dict], max_tokens: int, temperature: float
    ) -> LLMResponse | None:
        """Invoke the z-ai CLI with a JSON payload."""
        prompt = "\n".join(f"{m['role']}: {m['content']}" for m in messages)
        cmd = [self._cli_path, "llm", "--prompt", prompt, "--max-tokens", str(max_tokens)]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            if proc.returncode != 0:
                logger.warning("[zai] CLI failed: {}", stderr.decode()[:200])
                return None
            text = stdout.decode("utf-8").strip()
            # Strip any JSON wrapper
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict) and "text" in parsed:
                    text = parsed["text"]
            except json.JSONDecodeError:
                pass
            return LLMResponse(text=text, provider=self.name, model=self.model)
        except TimeoutError:
            logger.warning("[zai] CLI timeout")
            return None
        except Exception as e:
            logger.warning("[zai] CLI error: {}", e)
            return None

    async def _chat_via_http(
        self, messages: list[dict], max_tokens: int, temperature: float
    ) -> LLMResponse | None:
        """Direct HTTP fallback (assumes Z.AI OpenAI-compatible endpoint)."""
        import httpx
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    "https://api.z.ai/api/paas/v4/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": messages,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                    },
                )
            if r.status_code != 200:
                logger.warning("[zai] HTTP {}: {}", r.status_code, r.text[:200])
                return None
            data = r.json()
            text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return LLMResponse(
                text=text or "",
                provider=self.name,
                model=self.model,
            )
        except Exception as e:
            logger.warning("[zai] HTTP error: {}", e)
            return None
