"""Zero-cost, zero-external-call link classifier.

This is the ``rules`` tier: it runs entirely in-process on every collected
link and never fails, never rate-limits, and never costs money. It is the
tier that makes the platform 100% functional even if every optional paid
or free-quota LLM provider is absent or down.

The result is always one of ``CATEGORIES`` plus a confidence in [0, 1].
A confident rules match (extension or domain) short-circuits at 0.9;
a keyword-only match is weaker (0.55); anything unmatched falls back to
``other`` at 0.0 so the LLM tier (see ``app.classifier.llm``) knows it is
free to try improving on it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urlparse

CATEGORIES = (
    "movies_series",
    "software_apps",
    "books_courses",
    "games",
    "music",
    "social_channels",
    "adult",
    "other",
)

URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]]+", re.IGNORECASE)

# Punctuation that routinely terminates a sentence rather than a URL. Telegram
# messages are prose, so "زوروا https://example.com." and "see https://x.io!"
# are the norm, not the exception. Leaving the trailing mark attached would
# both corrupt the stored URL and defeat dedup, since url_hash is computed
# over the exact string (the same link would hash differently depending on the
# sentence it happened to end).
_TRAILING_PUNCTUATION = ".,!?:;\u060c\u061b\u061f\u2026'\"“”’«»"

_EXTENSION_MAP: dict[str, str] = {
    "apk": "software_apps",
    "exe": "software_apps",
    "msi": "software_apps",
    "dmg": "software_apps",
    "ipa": "software_apps",
    "pdf": "books_courses",
    "epub": "books_courses",
    "mobi": "books_courses",
    "mp3": "music",
    "flac": "music",
    "mp4": "movies_series",
    "mkv": "movies_series",
    "avi": "movies_series",
    "torrent": "movies_series",
}

_DOMAIN_MAP: dict[str, str] = {
    "github.com": "software_apps",
    "sourceforge.net": "software_apps",
    "apkpure.com": "software_apps",
    "uptodown.com": "software_apps",
    "imdb.com": "movies_series",
    "netflix.com": "movies_series",
    "shahid.mbc.net": "movies_series",
    "udemy.com": "books_courses",
    "coursera.org": "books_courses",
    "edx.org": "books_courses",
    "t.me": "social_channels",
    "telegram.me": "social_channels",
    "youtube.com": "movies_series",
    "youtu.be": "movies_series",
    "spotify.com": "music",
    "soundcloud.com": "music",
    "store.steampowered.com": "games",
    "epicgames.com": "games",
}

_KEYWORD_MAP: dict[str, tuple[str, ...]] = {
    "movies_series": ("فيلم", "افلام", "مسلسل", "حلقة", "movie", "series", "episode"),
    "software_apps": ("برنامج", "تطبيق", "تفعيل", "كراك", "app", "software", "crack"),
    "books_courses": ("كتاب", "رواية", "كورس", "دورة", "book", "course", "ebook"),
    "games": ("لعبة", "العاب", "game", "gaming"),
    "music": ("اغنية", "أغنية", "موسيقى", "البوم", "song", "music", "album"),
    "adult": ("+18", "xxx", "adult"),
}


@dataclass(frozen=True)
class ClassificationResult:
    category: str
    confidence: float
    matched_rule: str


def _domain_of(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return netloc[4:] if netloc.startswith("www.") else netloc


def hash_url(url: str) -> str:
    """Fixed-length dedupe key for a URL (see Link.url_hash)."""
    return hashlib.sha256(url.strip().lower().encode("utf-8")).hexdigest()


def _strip_trailing_punctuation(url: str) -> str:
    """Drop sentence punctuation that the URL pattern greedily swallowed."""
    return url.rstrip(_TRAILING_PUNCTUATION)


def extract_urls(text: str | None) -> list[str]:
    """Pull every http(s) URL out of a raw Telegram message body.

    Duplicates within a single message are collapsed while preserving the
    order they were written in, so a message that repeats the same link
    yields it once.
    """
    seen: set[str] = set()
    urls: list[str] = []
    for match in URL_RE.findall(text or ""):
        url = _strip_trailing_punctuation(match)
        # A bare scheme ("https://") left over after stripping is not a link.
        if not url or url.rstrip("/").endswith(":/"):
            continue
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def classify(url: str, raw_text: str | None = None) -> ClassificationResult:
    domain = _domain_of(url)
    last_segment = urlparse(url).path.lower().rsplit("/", 1)[-1]
    extension = last_segment.rsplit(".", 1)[-1] if "." in last_segment else ""

    if extension in _EXTENSION_MAP:
        return ClassificationResult(_EXTENSION_MAP[extension], 0.9, f"extension:{extension}")

    for known_domain, category in _DOMAIN_MAP.items():
        if domain == known_domain or domain.endswith("." + known_domain):
            return ClassificationResult(category, 0.9, f"domain:{known_domain}")

    haystack = f"{url} {raw_text or ''}".lower()
    for category, keywords in _KEYWORD_MAP.items():
        for keyword in keywords:
            if keyword.lower() in haystack:
                return ClassificationResult(category, 0.55, f"keyword:{keyword}")

    return ClassificationResult("other", 0.0, "unmatched")
