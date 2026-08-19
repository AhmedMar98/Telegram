"""Full-text search expressions, defined once for index and query alike.

Postgres will only use an expression index when the query's expression is
identical to the indexed one, so both are built from the constants here
rather than written out twice.

Why the text is word-split before indexing: Postgres's default parser
treats a URL as atomic tokens — ``https://example.com/python-book.pdf``
becomes ``example.com`` and ``/python-book.pdf``, never the word
``python-book``. Searching for part of a URL therefore matched nothing at
all on Postgres while working fine on SQLite's ILIKE fallback, so the
behaviour users got in production was strictly worse than in development.
Replacing every non-alphanumeric run with a space first turns the URL into
ordinary words (``https example com python book pdf``) that tokenize and
match normally. The same transformation is applied to the user's query, so
a hyphenated term like ``python-book`` becomes ``python book`` on both
sides. Arabic is unaffected: ``[:alnum:]`` covers its letters under a
UTF-8 locale, so Arabic words pass through untouched.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlalchemy.sql.elements import ColumnElement

# 'simple' rather than a language configuration: the corpus mixes Arabic and
# English, and 'simple' applies no language-specific stemming to either.
FTS_CONFIG = "simple"

# Any run of non-alphanumeric characters becomes a single space.
FTS_SPLIT_PATTERN = "[^[:alnum:]]+"

# The exact SQL of the indexed document. The migration that creates the GIN
# index embeds this same text; tests assert the planner actually chooses
# that index, which is what proves the two have not drifted apart.
FTS_DOCUMENT_SQL = (
    f"to_tsvector('{FTS_CONFIG}', "
    f"regexp_replace(coalesce(raw_text, '') || ' ' || url, '{FTS_SPLIT_PATTERN}', ' ', 'g'))"
)


def fts_document(raw_text_col: Any, url_col: Any) -> ColumnElement[Any]:
    """The searchable document for a link row."""
    return func.to_tsvector(
        FTS_CONFIG,
        func.regexp_replace(func.coalesce(raw_text_col, "") + " " + url_col, FTS_SPLIT_PATTERN, " ", "g"),
    )


def fts_query(text: str) -> ColumnElement[Any]:
    """The user's search terms, split the same way as the document."""
    return func.plainto_tsquery(FTS_CONFIG, func.regexp_replace(text, FTS_SPLIT_PATTERN, " ", "g"))


def fts_rank(raw_text_col: Any, url_col: Any, term: str) -> ColumnElement[Any]:
    """Relevance score for ordering results, highest first.

    Without this, a search with many matches is only ever sorted by
    recency, so a link that mentions the term once in passing outranks
    (chronologically) a link that is centrally about it. ``ts_rank``
    scores how much of / how prominently the document matches the query.
    """
    return func.ts_rank(fts_document(raw_text_col, url_col), fts_query(term))
