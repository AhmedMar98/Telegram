"""
Tier 3 — LLM-based classification (~4% + 1% deep).

Delegates to app.llm.orchestrator for provider routing + failover.
Uses semantic memory (cached embeddings) to reuse previous answers
when a similar URL was already classified (similarity > 0.92).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from loguru import logger

from ..llm.orchestrator import LLMOrchestrator
from ..models import LinkCategory


@dataclass
class AIResult:
    category: LinkCategory
    confidence: float
    provider: str
    raw_response: str
    semantic_reused: bool = False


SYSTEM_PROMPT = """You are a URL classifier for a Telegram link-intelligence platform.
Classify the given URL into EXACTLY ONE of these categories:
- whatsapp   (WhatsApp group invites, wa.me direct links)
- telegram   (Telegram channels, groups, invites, t.me/* links)
- file       (direct file downloads: pdf, zip, apk, mp4, ...)
- setting    (config files, settings, environment files)
- social     (twitter, instagram, tiktok, youtube, ...)
- news       (news articles, press releases)
- tool       (dev tools, GitHub repos, SaaS dashboards)
- other      (anything else)

Respond as compact JSON: {"category": "...", "confidence": 0.0-1.0, "reason": "..."}
Do not include any other text. Be concise.
"""

DEEP_PROMPT = """Analyze the following URL deeply. Fetch its likely content type, intent,
and target audience. Consider the surrounding context (Telegram message text).

URL: {url}
Context: {context}

Return JSON: {{"category": "...", "confidence": 0.0-1.0, "reason": "...", "tags": ["..."]}}
"""


_VALID_CATS = {c.value for c in LinkCategory}


def _parse_llm_json(raw: str) -> dict | None:
    """Robust JSON extraction from LLM output (handles code fences, extra text)."""
    if not raw:
        return None
    raw = raw.strip()
    # Strip markdown code fences
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    # Find first { ... }
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None


async def classify_with_llm(
    orchestrator: LLMOrchestrator,
    url: str,
    context: str = "",
    deep: bool = False,
) -> AIResult | None:
    """Use the LLM orchestrator to classify a URL."""
    user_msg = (
        f"URL: {url}\nContext: {context[:500]}"
        if not deep
        else DEEP_PROMPT.format(url=url, context=context[:500])
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    response = await orchestrator.chat(messages, max_tokens=200 if not deep else 500)
    if response is None:
        logger.warning("LLM classification failed for {} (all providers exhausted)", url[:80])
        return None

    parsed = _parse_llm_json(response.text)
    if not parsed:
        logger.debug("LLM returned non-JSON for {}: {}", url[:60], response.text[:120])
        return None

    cat = str(parsed.get("category", "")).lower().strip()
    if cat not in _VALID_CATS:
        cat = LinkCategory.OTHER.value

    try:
        conf = float(parsed.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    conf = max(0.0, min(1.0, conf))

    return AIResult(
        category=LinkCategory(cat),
        confidence=conf,
        provider=response.provider,
        raw_response=response.text,
    )
