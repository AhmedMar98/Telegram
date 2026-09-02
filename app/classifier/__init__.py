"""Link classification: rules only, in-process, deterministic.

There is no second tier. There was one — an optional Groq-hosted model
consulted when rule confidence was low — and it was removed in §43 along
with every trace of its configuration. The reasoning is recorded there, and
the short form is: a model called to cover a weak rule set is a way of not
fixing the rule set, and it made an offline, zero-cost, reproducible
pipeline depend on a third party's availability and quota.

What replaced it is stronger where it matters: the rules now weigh every
signal a link carries instead of stopping at the first one that matches.
"""

from __future__ import annotations

from app.classifier.canonical import canonical_url
from app.classifier.evidence import (
    ADULT_CATEGORY,
    CATEGORIES,
    CLASSIFIER_VERSION,
    DEFAULT_CATEGORY,
    HUMAN_VERDICT,
    ClassificationResult,
    Evidence,
    classify,
    may_reclassify,
)
from app.classifier.platform import DEFAULT_PLATFORM, PLATFORMS, link_platform
from app.classifier.rules import (
    detect_language,
    extract_url_spans,
    extract_urls,
    hash_url,
    split_context,
)


def classify_link(
    url: str,
    raw_text: str | None = None,
    *,
    channel_title: str | None = None,
    siblings: tuple[str, ...] = (),
) -> ClassificationResult:
    """The one entry point every ingestion path uses.

    ``channel_title`` and ``siblings`` are optional because not every caller
    has them: a manually pasted link has no source channel and no siblings.
    They are *evidence when present*, never requirements — a caller that
    cannot supply them gets a classification from the link and its text, the
    same as before.
    """
    return classify(url, raw_text, channel_title=channel_title, siblings=siblings)


__all__ = [
    "ADULT_CATEGORY",
    "CATEGORIES",
    "CLASSIFIER_VERSION",
    "DEFAULT_CATEGORY",
    "HUMAN_VERDICT",
    "DEFAULT_PLATFORM",
    "PLATFORMS",
    "ClassificationResult",
    "Evidence",
    "canonical_url",
    "classify_link",
    "detect_language",
    "extract_url_spans",
    "extract_urls",
    "hash_url",
    "link_platform",
    "may_reclassify",
    "split_context",
]
