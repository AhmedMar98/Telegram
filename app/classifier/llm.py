"""Optional second classification tier using Groq's free API tier.

This module is called only when the rules tier (``app.classifier.rules``)
returns low confidence, and only when ``GROQ_API_KEY`` is configured. It
is designed to *never* be a hard dependency:

- Any network error, timeout, non-2xx response, or malformed reply is
  caught and treated as "no improvement" — the rules-tier result is kept.
- Groq's free tier (https://console.groq.com) has no cost; there is no
  code path in this module that can incur a paid API charge, because
  there is no billing information involved in a free-tier API key.

This keeps the "strongest possible performance" requirement satisfiable
without breaking the "100% free" requirement: the extra accuracy is a
bonus layered on top of a fully working, zero-cost baseline.
"""

from __future__ import annotations

import json
import logging

import httpx

from app.classifier.rules import CATEGORIES, ClassificationResult
from app.config import get_settings

logger = logging.getLogger(__name__)

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_SYSTEM_PROMPT = (
    "You classify a single URL (with optional surrounding message text) into "
    "exactly one of these categories: " + ", ".join(CATEGORIES) + ". "
    'Reply with strict JSON only: {"category": "...", "confidence": 0.0-1.0}.'
)


def try_improve(url: str, raw_text: str | None, fallback: ClassificationResult) -> ClassificationResult:
    """Attempt to improve a low-confidence rules-tier result. Never raises."""
    settings = get_settings()
    if not settings.groq_api_key:
        return fallback

    user_content = f"URL: {url}\nContext: {(raw_text or '')[:500]}"
    payload = {
        "model": settings.groq_model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0,
        "max_tokens": 60,
    }
    headers = {"Authorization": f"Bearer {settings.groq_api_key}"}

    try:
        response = httpx.post(_GROQ_URL, json=payload, headers=headers, timeout=8.0)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        category = parsed.get("category")
        confidence = float(parsed.get("confidence", 0))
        if category in CATEGORIES and confidence > fallback.confidence:
            return ClassificationResult(category, min(confidence, 0.99), "llm:groq")
    except (httpx.HTTPError, KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("groq classification skipped: %s", exc)

    return fallback
