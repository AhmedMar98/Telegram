"""Text mechanics for ingestion: finding URLs, splitting context, hashing.

This module used to be the classifier as well. That moved to
``app.classifier.evidence`` when first-match-wins was replaced by evidence
aggregation, and what stayed here is the part that was never about
categories at all: pulling URLs out of a Telegram message, deciding which
words belong to which URL, guessing the script a message is written in, and
producing the dedupe key.

They stayed together because they share one property — every one of them
runs on **every** message, before anything is stored, and none of them may
raise on hostile input.
"""

from __future__ import annotations

import hashlib
import re

from app.classifier.canonical import canonical_url

URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]]+", re.IGNORECASE)

# Punctuation that routinely terminates a sentence rather than a URL. Telegram
# messages are prose, so "زوروا https://example.com." and "see https://x.io!"
# are the norm, not the exception. Leaving the trailing mark attached would
# corrupt the stored URL itself — and canonicalisation cannot undo that,
# because a trailing "." is a legal path character and this layer is the only
# one that knows it came from a sentence rather than from the link.
_TRAILING_PUNCTUATION = ".,!?:;\u060c\u061b\u061f\u2026'\"“”’«»"


def hash_url(url: str) -> str:
    """Fixed-length dedupe key for a URL (see ``Link.url_hash``).

    Hashes the **canonical** form (``app.classifier.canonical``), so the
    same link written with a tracking parameter, a ``www.`` prefix or a
    trailing slash lands on the row it already has instead of a second one.
    The URL itself is stored untouched; only this key is canonicalised.
    """
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()


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
