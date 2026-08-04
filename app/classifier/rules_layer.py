"""
Tier 1 — Rule-based classification (~80% of links).

Fast, deterministic, free. Catches obvious patterns:
    - WhatsApp group invites / wa.me links
    - Telegram t.me/ joins, +invites, channels
    - Direct file downloads (.pdf, .zip, .apk, .mp4, ...)
    - Settings / config files (.json, .yaml, .env, .conf)
    - Social media platforms (twitter, instagram, tiktok, ...)
    - News outlets (common domains)
    - GitHub / GitLab / dev tools
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from ..models import LinkCategory


@dataclass
class RuleResult:
    category: LinkCategory
    confidence: float
    rule_matched: str


# Rule definitions: (regex, category, confidence, rule_name)
# Higher in the list = higher priority.
# Use \b (word boundary) instead of $ so trailing context doesn't break matching.
_RULES: list[tuple[re.Pattern, LinkCategory, float, str]] = [
    # WhatsApp
    (re.compile(r"chat\.whatsapp\.com/", re.I), LinkCategory.WHATSAPP, 0.98, "wa_group_invite"),
    (re.compile(r"wa\.me/", re.I),                LinkCategory.WHATSAPP, 0.95, "wa_direct_link"),
    (re.compile(r"api\.whatsapp\.com/", re.I),    LinkCategory.WHATSAPP, 0.93, "wa_api_link"),

    # Telegram
    (re.compile(r"t(?:elegram)?\.me/\+", re.I),   LinkCategory.TELEGRAM, 0.98, "tg_private_invite"),
    (re.compile(r"t(?:elegram)?\.me/joinchat", re.I), LinkCategory.TELEGRAM, 0.98, "tg_joinchat"),
    (re.compile(r"t(?:elegram)?\.me/(?!joinchat|\+)", re.I), LinkCategory.TELEGRAM, 0.85, "tg_public_link"),
    (re.compile(r"telegram\.org/", re.I),         LinkCategory.TELEGRAM, 0.70, "tg_official"),

    # Settings / config files
    (re.compile(r"\.(json|yaml|yml|toml|ini|conf|env|config|properties)\b", re.I),
     LinkCategory.SETTING, 0.95, "config_file_ext"),
    (re.compile(r"/settings?/|/config/", re.I),   LinkCategory.SETTING, 0.80, "config_path"),

    # Direct file downloads (must come before generic)
    (re.compile(r"\.(pdf|zip|rar|7z|tar|gz|mp4|mkv|mp3|wav|apk|xlsx?|docx?|pptx?|epub|mobi)\b", re.I),
     LinkCategory.FILE, 0.95, "file_download_ext"),
    (re.compile(r"/download/|/files?/|/uploads?/", re.I),
     LinkCategory.FILE, 0.75, "file_path"),

    # Dev tools
    (re.compile(r"github\.com/|gitlab\.com/|bitbucket\.org/", re.I),
     LinkCategory.TOOL, 0.92, "dev_repo"),
    (re.compile(r"stackoverflow\.com/|dev\.to/|medium\.com/", re.I),
     LinkCategory.TOOL, 0.85, "dev_knowledge"),

    # News
    (re.compile(r"(?:cnn|bbc|reuters|aljazeera|nytimes|washingtonpost|bloomberg|techcrunch|theverge)\.com", re.I),
     LinkCategory.NEWS, 0.90, "news_outlet"),

    # Social
    (re.compile(r"(?:twitter|x|instagram|tiktok|facebook|linkedin|youtube|snapchat)\.com", re.I),
     LinkCategory.SOCIAL, 0.90, "social_platform"),
]


def classify_with_rules(url: str, context: str = "") -> Optional[RuleResult]:
    """
    Try to classify the URL with rule patterns.

    Returns None if no rule matches (caller should fall back to metadata/AI tiers).
    """
    # Use word boundaries (\b) instead of $ so trailing whitespace/context doesn't break matching
    full_text = f"{url} {context}".strip()
    for pattern, category, confidence, rule_name in _RULES:
        if pattern.search(full_text):
            return RuleResult(
                category=category,
                confidence=confidence,
                rule_matched=rule_name,
            )
    return None


def host_hint(url: str) -> Optional[LinkCategory]:
    """Use host TLD as a weak hint when no rule matches."""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return None
    if not host:
        return None
    if host.endswith(".gov") or host.endswith(".edu"):
        return LinkCategory.NEWS
    return None
