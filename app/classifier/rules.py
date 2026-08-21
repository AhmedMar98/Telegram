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

# Named rather than spelled inline at its one other use site (the idea-152
# alert in app/ingest.py). A category renamed here would otherwise leave a
# string comparison that quietly never matches again — an alert that stops
# firing looks exactly like an alert with nothing to report.
ADULT_CATEGORY = "adult"

CATEGORIES = (
    "movies_series",
    "software_apps",
    "books_courses",
    "games",
    "music",
    "social_channels",
    ADULT_CATEGORY,
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


def extract_url_spans(text: str | None) -> list[tuple[str, int, int]]:
    """Every http(s) URL plus where it sits in the text, as (url, start, end).

    ``end`` is the offset just past the URL *after* trailing punctuation is
    stripped, so slicing ``text[start:end]`` returns exactly the stored URL.
    Duplicates within a single message are collapsed while preserving the
    order they were written in, so a message that repeats the same link
    yields it once.
    """
    seen: set[str] = set()
    spans: list[tuple[str, int, int]] = []
    for match in URL_RE.finditer(text or ""):
        url = _strip_trailing_punctuation(match.group(0))
        # A bare scheme ("https://") left over after stripping is not a link.
        if not url or url.rstrip("/").endswith(":/"):
            continue
        if url in seen:
            continue
        seen.add(url)
        spans.append((url, match.start(), match.start() + len(url)))
    return spans


def extract_urls(text: str | None) -> list[str]:
    """Pull every http(s) URL out of a raw Telegram message body."""
    return [url for url, _, _ in extract_url_spans(text)]


def _midpoint_word_boundary(gap: str) -> int:
    """Halfway through ``gap``, nudged to the nearest space.

    Cutting at the exact midpoint would slice a word in half and store the
    fragments as searchable text; snapping to whitespace keeps whole words
    on whichever side they land.
    """
    middle = len(gap) // 2
    candidates = [i for i, char in enumerate(gap) if char.isspace()]
    if not candidates:
        return middle
    return min(candidates, key=lambda i: abs(i - middle)) + 1


def split_context(text: str | None, spans: list[tuple[str, int, int]]) -> list[str]:
    """Give each URL only the words that belong to it, not the whole message.

    Storing the entire message alongside every URL it contains is what made
    a twenty-link dump match any search term appearing anywhere in it: one
    hit returned all twenty. Splitting the message means a search for
    "الحلقة الثالثة" returns the link that line actually labelled.

    The message is cut at the gaps between consecutive URLs. A gap is
    split at its **first** newline, which is what the two dominant Telegram
    formats both need:

        🎬 اسم الفيلم          https://a.example - film
        https://a.example      https://b.example - book
        📚 اسم الكتاب
        https://b.example

    In the left form the label precedes its link, in the right form it
    follows; cutting at the first newline assigns the label correctly in
    both. A gap with no newline at all is genuinely ambiguous
    ("https://a b https://c"), so it is split down the middle rather than
    guessed. Text before the first URL and after the last belongs wholly
    to that first/last URL.

    A message with one URL (or none) keeps the full text: there is nothing
    to disambiguate, and truncating it would only lose search recall.
    """
    body = text or ""
    if len(spans) <= 1:
        return [body] * len(spans)

    cuts: list[int] = []
    for (_, _, end), (_, next_start, _) in zip(spans, spans[1:], strict=False):
        gap = body[end:next_start]
        newline = gap.find("\n")
        offset = newline + 1 if newline != -1 else _midpoint_word_boundary(gap)
        cuts.append(end + offset)

    bounds = [0, *cuts, len(body)]
    return [body[bounds[i] : bounds[i + 1]].strip() for i in range(len(spans))]


# Arabic script, including the Arabic Supplement and Extended-A blocks, so
# that letters used by Persian/Urdu-influenced spellings still count.
_ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]")
_LATIN_RE = re.compile(r"[A-Za-z]")

# Below this share, a script is treated as incidental rather than as one of
# the languages the text is written in — a single English brand name inside
# an Arabic sentence should not make the message "mixed".
_MIXED_MINOR_SHARE = 0.2


def detect_language(text: str | None) -> str | None:
    """Rough script-based label for a message: "ar", "en", "mixed" or None.

    This detects *script*, not language: it cannot tell English from French,
    and it calls Persian "ar". That is deliberate — the useful question here
    is "can I filter to the Arabic half of my collection", and script answers
    it with no dependency, no model and no network. Anything more precise
    would need a language-detection package, which is not worth its weight
    for a filter chip.

    URLs are removed first, because every link contributes Latin letters and
    would otherwise make every Arabic message look mixed.
    """
    body = URL_RE.sub(" ", text or "")
    arabic = len(_ARABIC_RE.findall(body))
    latin = len(_LATIN_RE.findall(body))
    total = arabic + latin
    if total == 0:
        return None
    minor = min(arabic, latin) / total
    if minor >= _MIXED_MINOR_SHARE:
        return "mixed"
    return "ar" if arabic > latin else "en"


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
