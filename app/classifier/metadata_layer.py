"""
Tier 2 — Metadata-based classification (~15% of links).

Uses HTTP HEAD/GET to fetch:
    - Final URL (after redirects)
    - Content-Type header
    - HTML <title> / og:title
    - og:type (article, video, website, ...)
    - File extension from final URL
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup
from loguru import logger

from ..models import LinkCategory


@dataclass
class MetadataResult:
    category: LinkCategory
    confidence: float
    title: str | None = None
    description: str | None = None
    http_status: int | None = None
    final_url: str | None = None
    content_type: str | None = None


# Map content-type → category
_CT_MAP = [
    ("application/pdf", LinkCategory.FILE, 0.92),
    ("application/zip", LinkCategory.FILE, 0.92),
    ("application/octet-stream", LinkCategory.FILE, 0.70),
    ("image/", LinkCategory.FILE, 0.75),
    ("video/", LinkCategory.FILE, 0.85),
    ("audio/", LinkCategory.FILE, 0.85),
    ("text/html", LinkCategory.NEWS, 0.55),  # weak default
    ("application/json", LinkCategory.SETTING, 0.80),
    ("text/plain", LinkCategory.OTHER, 0.40),
]


def _categorize_by_content_type(ct: str) -> tuple[LinkCategory, float] | None:
    for prefix, cat, conf in _CT_MAP:
        if ct.lower().startswith(prefix):
            return cat, conf
    return None


def _categorize_by_html_title(title: str, desc: str = "") -> tuple[LinkCategory, float] | None:
    """Heuristic: scan title keywords."""
    text = f"{title} {desc}".lower()
    if not text.strip():
        return None
    hints = [
        (("join", "group", "channel", "invite"), LinkCategory.TELEGRAM, 0.60),
        (("movie", "film", "watch", "stream"), LinkCategory.FILE, 0.65),
        (("tutorial", "course", "learn"), LinkCategory.TOOL, 0.60),
        (("news", "breaking", "report"), LinkCategory.NEWS, 0.65),
    ]
    for keywords, cat, conf in hints:
        if any(kw in text for kw in keywords):
            return cat, conf
    return None


async def fetch_metadata(url: str, timeout: float = 8.0) -> MetadataResult | None:
    """
    Fetch URL metadata (HEAD first, GET if HTML needed).

    Returns None if the URL is unreachable.
    """
    headers = {"User-Agent": "Mozilla/5.0 (compatible; LinkIntelBot/1.0; +https://example.com/bot)"}
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=timeout, headers=headers
        ) as client:
            # HEAD first
            try:
                r = await client.head(url)
                ct = r.headers.get("content-type", "")
                if r.status_code < 400 and ct and not ct.startswith("text/html"):
                    cat_conf = _categorize_by_content_type(ct)
                    if cat_conf:
                        cat, conf = cat_conf
                        return MetadataResult(
                            category=cat,
                            confidence=conf,
                            http_status=r.status_code,
                            content_type=ct,
                            final_url=str(r.url),
                        )
            except httpx.HTTPError:
                pass  # Some servers reject HEAD; fall through to GET

            # GET (full body for HTML)
            r = await client.get(url)
            ct = r.headers.get("content-type", "")
            title = None
            description = None

            if "text/html" in ct.lower():
                try:
                    soup = BeautifulSoup(r.text[:50_000], "lxml")
                    if soup.title and soup.title.string:
                        title = soup.title.string.strip()[:500]
                    og_title = soup.find("meta", property="og:title")
                    og_desc = soup.find("meta", property="og:description")
                    if og_title and og_title.get("content"):
                        title = og_title["content"][:500]
                    if og_desc and og_desc.get("content"):
                        description = og_desc["content"][:1000]
                except Exception as e:
                    logger.debug("HTML parse failed for {}: {}", url, e)

            category: LinkCategory | None = None
            conf = 0.0
            ct_match = _categorize_by_content_type(ct)
            if ct_match:
                category, conf = ct_match
            else:
                title_match = _categorize_by_html_title(title or "", description or "")
                if title_match:
                    category, conf = title_match

            if category is None:
                return None

            return MetadataResult(
                category=category,
                confidence=conf,
                title=title,
                description=description,
                http_status=r.status_code,
                content_type=ct,
                final_url=str(r.url),
            )
    except httpx.TimeoutException:
        logger.debug("Metadata fetch timeout for {}", url)
    except Exception as e:
        logger.debug("Metadata fetch failed for {}: {}", url, e)
    return None
