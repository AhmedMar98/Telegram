"""Two-tier link classifier: free rules always, free-tier LLM as a bonus."""

from __future__ import annotations

from app.classifier import llm, rules
from app.classifier.rules import CATEGORIES, ClassificationResult, extract_urls, hash_url

_LLM_THRESHOLD = 0.6


def classify_link(url: str, raw_text: str | None = None) -> ClassificationResult:
    result = rules.classify(url, raw_text)
    if result.confidence < _LLM_THRESHOLD:
        result = llm.try_improve(url, raw_text, result)
    return result


__all__ = ["CATEGORIES", "ClassificationResult", "classify_link", "extract_urls", "hash_url"]
