"""HuggingFace Inference API provider."""
from __future__ import annotations

import httpx
from loguru import logger

from ...config import get_settings
from .base import BaseLLMProvider, LLMResponse


class HuggingFaceProvider(BaseLLMProvider):
    name = "huggingface"
    BASE_URL = "https://api-inference.huggingface.co/models/{model}"

    def __init__(self) -> None:
        s = get_settings()
        self.api_key = s.hf_api_key
        self.model = s.hf_model

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
        url = self.BASE_URL.format(model=self.model)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        # Convert chat → single prompt (HF chat-template applied server-side)
        prompt_parts = []
        for m in messages:
            role = m["role"].upper()
            prompt_parts.append(f"{role}: {m['content']}")
        prompt = "\n".join(prompt_parts) + "\nASSISTANT:"

        payload = {
            "inputs": prompt,
            "parameters": {
                "max_new_tokens": max_tokens,
                "temperature": temperature,
                "return_full_text": False,
            },
            "options": {"wait_for_model": True},
        }
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(url, headers=headers, json=payload)
            if r.status_code == 503:
                logger.debug("[hf] model loading, will retry later")
                return None
            if r.status_code != 200:
                logger.warning("[hf] HTTP {}: {}", r.status_code, r.text[:200])
                return None
            data = r.json()
            # HF returns [{generated_text: "..."}]
            if isinstance(data, list) and data:
                text = data[0].get("generated_text", "")
            else:
                text = str(data)
            return LLMResponse(
                text=text.strip(),
                provider=self.name,
                model=self.model,
            )
        except Exception as e:
            logger.warning("[hf] error: {}", e)
            return None
