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
import time
from collections.abc import Mapping
from dataclasses import dataclass

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
        # Before raise_for_status on purpose: a 429 is the single most
        # informative response about quota, and raising first would throw
        # its headers away.
        record_quota_headers(response.headers)
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


_PROBE_URL = "https://api.groq.com/openai/v1/models"
_PROBE_TIMEOUT = 3.0
_PROBE_CACHE_SECONDS = 60.0
# Last probe result and the monotonic clock reading when it was taken.
_probe_cache: tuple[float, dict[str, object]] | None = None


def probe(*, now: float | None = None) -> dict[str, object]:
    """Diagnose whether the optional Groq tier can actually be reached.

    Diagnostic only. Nothing here decides whether the service is ready:
    Groq is optional by construction, so an outage there is not an outage
    here, and a readiness probe that says otherwise would have Render
    restart a perfectly healthy service because a third party was down.

    Three deliberate limits, because this is reachable without a session:

    - ``GET /models`` rather than a completion — it validates the key and
      the route to Groq without spending any of the free quota.
    - A 3-second timeout, well under the 8 seconds classification allows,
      because a diagnostic that hangs a worker is worse than no diagnostic.
    - A 60-second cache, so repeated calls cannot be turned into outbound
      request amplification through this service.

    Never raises, and never returns any part of the key.
    """
    global _probe_cache

    settings = get_settings()
    if not settings.groq_api_key:
        # Not an error: the platform is designed to run without it.
        return {"configured": False, "status": "not_configured", "detail": None}

    clock = now if now is not None else time.monotonic()
    if _probe_cache is not None and clock - _probe_cache[0] < _PROBE_CACHE_SECONDS:
        return dict(_probe_cache[1], cached=True)

    result: dict[str, object]
    try:
        response = httpx.get(
            _PROBE_URL,
            headers={"Authorization": f"Bearer {settings.groq_api_key}"},
            timeout=_PROBE_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        # The message can name a host but never the key — it is only ever
        # sent in a header, and httpx does not echo headers in errors.
        result = {"configured": True, "status": "unreachable", "detail": type(exc).__name__}
    else:
        if response.status_code == 200:
            result = {"configured": True, "status": "ok", "detail": None}
        elif response.status_code in (401, 403):
            # Worth separating: an expired key is fixed by pasting a new
            # one, an outage is fixed by waiting.
            result = {"configured": True, "status": "unauthorized", "detail": str(response.status_code)}
        else:
            result = {"configured": True, "status": "error", "detail": str(response.status_code)}

    _probe_cache = (clock, result)
    return dict(result, cached=False)


def reset_probe_cache() -> None:
    """Drop the cached probe result. For tests and for key rotation."""
    global _probe_cache
    _probe_cache = None


# --- what is left of the free quota, if the response says (idea 160) ------

# The idea is explicitly conditional: *"if the response headers expose
# it"*. This environment cannot check what Groq actually sends —
# ``console.groq.com`` is blocked from it by the egress proxy, the same
# class of gap ``docs/02-core-idea.md`` §5 records for the database's
# lifetime — so nothing here asserts that any particular header exists.
#
# Instead of a hardcoded list of dimension names, any
# ``x-ratelimit-remaining-<dimension>`` paired with its matching
# ``x-ratelimit-limit-<dimension>`` is read. That handles requests, tokens
# and any dimension not thought of here, and it degrades to *nothing*: if
# the response carries no such pair, the snapshot stays empty, and an
# empty snapshot cannot raise an alert. A wrong guess about header names
# therefore costs a missing warning, never a false one.
_REMAINING_PREFIX = "x-ratelimit-remaining-"
_LIMIT_PREFIX = "x-ratelimit-limit-"


@dataclass(frozen=True)
class QuotaReading:
    """One rate-limit dimension as the last response reported it."""

    dimension: str
    remaining: float
    limit: float

    @property
    def fraction_left(self) -> float:
        return self.remaining / self.limit if self.limit > 0 else 0.0


# Last reading per dimension. Process-local by design: it describes the
# quota this process has been spending, and the collector — which is where
# bulk classification happens — is a one-shot process that reads it at the
# end of its own run.
_quota: dict[str, QuotaReading] = {}


def _as_number(raw: str) -> float | None:
    """A header value as a number, or None if it is not one.

    Reset headers in the same family carry durations like ``7.66s``, and a
    dimension whose value cannot be read as a plain number is skipped
    rather than guessed at.
    """
    try:
        return float(raw.strip())
    except (TypeError, ValueError):
        return None


def record_quota_headers(headers: Mapping[str, str]) -> None:
    """Note whatever the response said about remaining quota. Never raises."""
    lowered = {key.lower(): value for key, value in headers.items()}
    for key, raw_remaining in lowered.items():
        if not key.startswith(_REMAINING_PREFIX):
            continue
        dimension = key[len(_REMAINING_PREFIX) :]
        raw_limit = lowered.get(f"{_LIMIT_PREFIX}{dimension}")
        if raw_limit is None:
            # A remaining with no limit is a number with no denominator;
            # "8000 left" says nothing about whether that is a lot.
            continue
        remaining, limit = _as_number(raw_remaining), _as_number(raw_limit)
        if remaining is None or limit is None or limit <= 0:
            continue
        _quota[dimension] = QuotaReading(dimension=dimension, remaining=remaining, limit=limit)


def quota_readings() -> list[QuotaReading]:
    """Every dimension seen so far, scarcest first."""
    return sorted(_quota.values(), key=lambda reading: reading.fraction_left)


def lowest_quota() -> QuotaReading | None:
    """The dimension closest to running out, or None if none was reported."""
    readings = quota_readings()
    return readings[0] if readings else None


def reset_quota() -> None:
    """Forget the recorded readings. For tests."""
    _quota.clear()
