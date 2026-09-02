"""Weighing every signal a link carries, instead of stopping at the first.

The previous classifier was first-match-wins: extension, then domain, then
the first keyword that appeared anywhere in the message, then ``other``. It
returned a fixed confidence per tier (0.9, 0.9, 0.55, 0.0). Three things
were wrong with that, and all three are visible in stored rows:

**It decided before it had read the evidence.** A ``youtube.com`` link in a
message about a course was a film, because the domain rule fired first and
nothing after it could speak. Agreement between two signals could not raise
confidence and disagreement could not lower it, because only one signal was
ever consulted.

**It confused the platform with the category.** ``youtube.com ->
movies_series`` answers "which service is this on", which is what the
``platform`` column has answered since §39. YouTube carries courses, music,
gaming and films; the domain says nothing about which. So YouTube is not a
category signal here at all — its category has to come from the words, the
path, or the channel that posted it.

**Its keyword matching had a real bug.** ``"app" in haystack`` matched
*happens*, *apply* and *happy*; ``"crack"`` matched *cracked* in an English
sentence about eggs. Substring matching is right for Arabic — which glues
و/ب/ل/ال onto the front of words, so a word boundary would miss the very
forms people write — and wrong for Latin script, which does not.

What replaces it: gather every piece of evidence, add up the weight per
category, and let the winner's *share* of the total be the confidence. A
signal no longer wins by arriving first; it wins by outweighing what
disagrees with it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.arabic import normalise

# The version stamped onto every row this module classifies. Stored in
# ``Link.classified_by``, which used to carry "rules" or "llm" and became a
# constant when the LLM tier was removed. Bump it when weights or maps
# change in a way that would classify an old row differently — that is what
# makes "why does this row say that?" answerable a year later.
CLASSIFIER_VERSION = "rules-v2"

# What ``classified_by`` says when a person overruled the rules. Not a
# classifier version and never produced by one — the one value in that
# column that no automatic pass may write over.
HUMAN_VERDICT = "manual"


def may_reclassify(classified_by: str | None) -> bool:
    """Whether an automatic pass is allowed to rewrite this row's category.

    The rule is one line and the reason it lives here is not: it used to be
    an inline ``if classified_by != "manual"`` inside migration 0025, which
    made it a **rule no test could reach**. A migration runs once and is
    then history; a condition buried in one is a guard whose removal
    nothing detects — the pattern §43.9 records as its fourth defect.

    Every future re-classification (a weights change, a rules version bump)
    needs exactly this rule, so it is named, importable and tested once.
    A human correction outranks every rule the machine has.
    """
    return classified_by != HUMAN_VERDICT


# Named rather than spelled inline at its one other use site (the adult
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
DEFAULT_CATEGORY = "other"

# --- weights ---------------------------------------------------------------
#
# Ordered by how much the signal actually proves, not by how easy it is to
# compute. The absolute numbers matter less than their ratios: an extension
# must outweigh a single keyword decisively, and two agreeing weak signals
# must be able to outvote one strong one that disagrees.
W_EXTENSION = 4.0  # the URL literally ends in .apk — nearly a fact
W_DOMAIN = 3.0  # a known service, but a service is not its content
W_PATH = 2.0  # the site itself named the section: /course/, /movie/
W_KEYWORD = 1.0  # one word in the message
W_SIBLING = 1.0  # another link in the same message, confidently classified
# Below W_KEYWORD, deliberately. A channel is a *prior* on what it usually
# posts, not a description of this particular link — the message is. Set
# at or above a keyword's weight, one contested case makes the point:
# "كتاب رائع" posted in a channel titled "قناة الأفلام" classified as a
# film, because the title alone (one category match) already outweighed
# the one word actually written about this link. Weaker than a keyword
# means a channel breaks a tie or fills a blank, and never overrides what
# the message itself says.
W_CHANNEL = 0.75

# A repeated word is one voice, not five. Without this cap a message that
# says "فيلم" six times outweighs the file extension of the link itself.
MAX_KEYWORD_HITS = 3

# Confidence = winner / (total + SMOOTHING). The denominator holds every
# category's score, so contested evidence lowers confidence on its own. The
# constant stops a single weak signal from claiming near-certainty: one
# keyword alone lands at 1/(1+1) = 0.50, an extension alone at 4/5 = 0.80,
# and an extension backed by two agreeing keywords at 6/7 = 0.86.
SMOOTHING = 1.0
MAX_CONFIDENCE = 0.99

# A link is only allowed to lend its category to a sibling if it is itself
# well supported, and only a link that could not decide on its own asks.
SIBLING_DONOR_MIN = 0.70
SIBLING_ASKER_MAX = 0.50

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
    "m4a": "music",
    "wav": "music",
    "mp4": "movies_series",
    "mkv": "movies_series",
    "avi": "movies_series",
    "torrent": "movies_series",
}

# Only services whose content is overwhelmingly one category. A file host
# (mega, mediafire, drive) carries everything and is therefore **not** here:
# its links get their category from the words around them, which is the
# honest answer rather than a confident wrong one.
#
# YouTube's absence is the deliberate one. See the module docstring.
_DOMAIN_MAP: dict[str, str] = {
    "github.com": "software_apps",
    "sourceforge.net": "software_apps",
    "apkpure.com": "software_apps",
    "uptodown.com": "software_apps",
    "play.google.com": "software_apps",
    "apps.apple.com": "software_apps",
    "pypi.org": "software_apps",
    "npmjs.com": "software_apps",
    "imdb.com": "movies_series",
    "netflix.com": "movies_series",
    "shahid.mbc.net": "movies_series",
    "udemy.com": "books_courses",
    "coursera.org": "books_courses",
    "edx.org": "books_courses",
    "goodreads.com": "books_courses",
    "t.me": "social_channels",
    "telegram.me": "social_channels",
    "spotify.com": "music",
    "soundcloud.com": "music",
    "anghami.com": "music",
    "deezer.com": "music",
    "store.steampowered.com": "games",
    "epicgames.com": "games",
    "gog.com": "games",
    "itch.io": "games",
}

# Matched against whole path *segments*, never as substrings: a substring
# rule for "app" would fire on /happening/, which is the same class of bug
# the keyword matcher had.
_PATH_MAP: dict[str, str] = {
    "movie": "movies_series",
    "movies": "movies_series",
    "film": "movies_series",
    "films": "movies_series",
    "series": "movies_series",
    "episode": "movies_series",
    "watch": "movies_series",
    "course": "books_courses",
    "courses": "books_courses",
    "lesson": "books_courses",
    "book": "books_courses",
    "books": "books_courses",
    "ebook": "books_courses",
    "game": "games",
    "games": "games",
    "album": "music",
    "track": "music",
    "song": "music",
    "app": "software_apps",
    "apps": "software_apps",
    "software": "software_apps",
}

_KEYWORD_MAP: dict[str, tuple[str, ...]] = {
    "movies_series": (
        "فيلم",
        "افلام",
        "مسلسل",
        "مسلسلات",
        "حلقة",
        "حلقات",
        "مترجم",
        "movie",
        "series",
        "episode",
    ),
    "software_apps": ("برنامج", "برامج", "تطبيق", "تطبيقات", "تفعيل", "كراك", "app", "software", "crack"),
    "books_courses": (
        "كتاب",
        "كتب",
        "رواية",
        "روايات",
        "كورس",
        "دورة",
        "دورات",
        "محاضرة",
        "شرح",
        "book",
        "course",
        "ebook",
    ),
    "games": ("لعبة", "العاب", "game", "gaming"),
    "music": ("اغنية", "اغاني", "موسيقى", "البوم", "song", "music", "album"),
    "social_channels": ("قناة", "قروب", "مجموعة", "channel", "group"),
    ADULT_CATEGORY: ("+18", "xxx", "adult"),
}


# Escapes rather than literals, for the reason spelled out in app/arabic.py:
# Arabic range endpoints written literally are reordered on screen by the
# bidirectional algorithm, and a swapped range fails silently.
_ARABIC_RANGE = ("\u0600", "\u08ff")


def _is_arabic(word: str) -> bool:
    return any(_ARABIC_RANGE[0] <= char <= _ARABIC_RANGE[1] for char in word)


def _compile(keyword: str) -> re.Pattern[str]:
    """A matcher for one keyword, folded the same way the text will be.

    Latin keywords are anchored at word boundaries, because "app" inside
    "happens" is not a mention of an app. Arabic keywords are matched as
    substrings, because Arabic attaches its conjunctions and prepositions
    to the front of the word ("وفيلم", "بالفيلم"), and anchoring would miss
    exactly the forms people write. The two scripts get different rules
    because they behave differently, not as an approximation.
    """
    folded = normalise(keyword)
    if _is_arabic(folded):
        return re.compile(re.escape(folded))
    # Not ``\b``: that treats ``_`` as a letter, so "adult" inside
    # ``/adult_hot_show`` would not match — and an underscore is a
    # separator everywhere this text comes from (URLs, filenames, slugs),
    # never part of a word. Letters and digits only, on already-folded
    # lower-case text.
    return re.compile(rf"(?<![a-z0-9]){re.escape(folded)}(?![a-z0-9])")


_KEYWORD_PATTERNS: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    category: tuple((word, _compile(word)) for word in words) for category, words in _KEYWORD_MAP.items()
}


@dataclass(frozen=True)
class Evidence:
    """One reason to believe a link belongs to a category."""

    kind: str  # extension | domain | path | channel | keyword | sibling
    detail: str  # "pdf", "github.com", "فيلم"
    category: str
    weight: float

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.detail}"


@dataclass(frozen=True)
class ClassificationResult:
    category: str
    confidence: float
    matched_rule: str
    evidence: tuple[Evidence, ...] = ()

    @property
    def classifier_version(self) -> str:
        return CLASSIFIER_VERSION


def _domain_of(url: str) -> str:
    try:
        netloc = urlsplit(url).netloc.lower()
    except ValueError:
        return ""
    netloc = netloc.rsplit("@", 1)[-1].split(":", 1)[0]
    return netloc[4:] if netloc.startswith("www.") else netloc


def _path_of(url: str) -> str:
    try:
        return urlsplit(url).path
    except ValueError:
        return ""


def collect_evidence(
    url: str,
    raw_text: str | None = None,
    *,
    channel_title: str | None = None,
    siblings: tuple[str, ...] = (),
) -> list[Evidence]:
    """Every signal this link carries, with nothing decided yet."""
    found: list[Evidence] = []

    path = _path_of(url)
    last_segment = path.lower().rsplit("/", 1)[-1]
    extension = last_segment.rsplit(".", 1)[-1] if "." in last_segment else ""
    if extension in _EXTENSION_MAP:
        found.append(Evidence("extension", extension, _EXTENSION_MAP[extension], W_EXTENSION))

    domain = _domain_of(url)
    for known, category in _DOMAIN_MAP.items():
        if domain == known or domain.endswith("." + known):
            found.append(Evidence("domain", known, category, W_DOMAIN))
            break

    for segment in path.lower().strip("/").split("/"):
        if segment in _PATH_MAP:
            found.append(Evidence("path", segment, _PATH_MAP[segment], W_PATH))
            break

    # The URL is folded in with the message text: a slug like
    # /the-great-movie-2019 carries the same words a caption would.
    haystack = normalise(f"{url} {raw_text or ''}")
    for category, patterns in _KEYWORD_PATTERNS.items():
        hits = 0
        for word, pattern in patterns:
            if hits >= MAX_KEYWORD_HITS:
                break
            if pattern.search(haystack):
                found.append(Evidence("keyword", word, category, W_KEYWORD))
                hits += 1

    if channel_title:
        title = normalise(channel_title)
        for category, patterns in _KEYWORD_PATTERNS.items():
            for word, pattern in patterns:
                if pattern.search(title):
                    found.append(Evidence("channel", word, category, W_CHANNEL))
                    break

    for category in siblings:
        if category in CATEGORIES and category != DEFAULT_CATEGORY:
            found.append(Evidence("sibling", category, category, W_SIBLING))

    return found


def decide(evidence: list[Evidence]) -> ClassificationResult:
    """Turn evidence into one category and a confidence in [0, 0.99]."""
    if not evidence:
        return ClassificationResult(DEFAULT_CATEGORY, 0.0, "unmatched", ())

    scores: dict[str, float] = {}
    for item in evidence:
        scores[item.category] = scores.get(item.category, 0.0) + item.weight

    total = sum(scores.values())
    # Ties broken by the heaviest single piece of evidence, then by the
    # category name — so the same input always produces the same row, which
    # a test can assert and a reader can reproduce.
    best = max(sorted(scores), key=lambda category: scores[category])
    confidence = min(scores[best] / (total + SMOOTHING), MAX_CONFIDENCE)

    supporting = [item for item in evidence if item.category == best]
    strongest = max(supporting, key=lambda item: item.weight)
    return ClassificationResult(best, round(confidence, 4), strongest.label, tuple(evidence))


def classify(
    url: str,
    raw_text: str | None = None,
    *,
    channel_title: str | None = None,
    siblings: tuple[str, ...] = (),
) -> ClassificationResult:
    """Classify one link from everything known about it."""
    return decide(collect_evidence(url, raw_text, channel_title=channel_title, siblings=siblings))
