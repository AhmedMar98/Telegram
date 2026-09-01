"""The one query that answers "which links?".

Extracted from ``app.routers.links`` because it had two callers with two
different answers: the web API built this query, and the Telegram bot
wrote its own ``ILIKE`` by hand. On Postgres that meant the bot silently
skipped full-text search, relevance ranking, exclusion terms and the
archived filter — the same search text returned different results
depending on which surface asked. One builder, one behaviour.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy import func, or_
from sqlalchemy.orm import Query as OrmQuery
from sqlalchemy.orm import Session

from app.models import Link
from app.search import fts_document, fts_query, parse_query


def dialect_of(db: Session) -> str:
    return db.bind.dialect.name if db.bind is not None else "sqlite"


def filtered_links(
    db: Session,
    workspace_id: int,
    *,
    q: str | None,
    category: str | None,
    favorite: bool | None = None,
    alive: bool | None = None,
    channel_id: int | None = None,
    language: str | None = None,
    domain: str | None = None,
    platform: str | None = None,
    since: date | None = None,
    until: date | None = None,
    include_archived: bool = False,
) -> tuple[OrmQuery[Link], bool]:
    """The base query shared by search, export and bulk actions.

    Returns the filtered query alongside whether it can be ranked by
    relevance (only true for a Postgres full-text search with a term).
    """
    query = db.query(Link).filter(Link.workspace_id == workspace_id)
    ranked = False

    if not include_archived:
        # Archiving is the point of the feature: dead links accumulate and
        # drown out live ones. They stay reachable via ?include_archived=true
        # and are never removed from exports, which are about completeness.
        query = query.filter(Link.is_archived.is_(False))

    if category:
        # Comma-separated means "any of these". A single value still takes
        # the equality path so the common case keeps the same query plan
        # rather than becoming a one-element IN.
        wanted = [part.strip() for part in category.split(",") if part.strip()]
        if len(wanted) == 1:
            query = query.filter(Link.category == wanted[0])
        elif wanted:
            query = query.filter(Link.category.in_(wanted))

    # A date window on when the link was collected (idea 179). Compared
    # against midnight boundaries rather than the bare dates so a
    # DateTime column is not silently truncated: `created_at <= until`
    # with a date would exclude everything collected *during* the last
    # day, which reads as an off-by-one to anyone using it.
    if since is not None:
        query = query.filter(Link.created_at >= datetime.combine(since, time.min))
    if until is not None:
        query = query.filter(Link.created_at < datetime.combine(until, time.min) + timedelta(days=1))

    # Comma-separated means "any of these", matching how `category` behaves
    # one filter up. The two are independent axes and combine as an AND:
    # "Telegram links that are courses" is a question the pair answers and
    # neither answers alone.
    if platform:
        wanted = [part.strip() for part in platform.split(",") if part.strip()]
        if len(wanted) == 1:
            query = query.filter(Link.platform == wanted[0])
        elif wanted:
            query = query.filter(Link.platform.in_(wanted))

    if favorite is not None:
        query = query.filter(Link.is_favorite == favorite)

    if alive is not None:
        # is_alive is nullable (never checked yet); an explicit filter only
        # ever means "show me a definite answer", never "include unknowns".
        query = query.filter(Link.is_alive == alive)

    if domain:
        # Exact match on the stored domain, which is already normalised
        # (lowercased, "www." stripped) at ingest time — so this is the same
        # value the stats panel and the "similar links" button hand back.
        query = query.filter(Link.domain == domain.lower())

    if language:
        # Deliberately an exact match on the stored label rather than a
        # "contains Arabic" test: the label is what the UI offers as a chip,
        # so filtering on anything else would return rows the chip did not
        # promise. Links stored before language detection existed have NULL
        # here and are correctly excluded from every language filter.
        query = query.filter(Link.language == language)

    if channel_id is not None:
        # No existence check is needed: the query is already scoped to the
        # workspace, so another workspace's channel id simply matches
        # nothing rather than leaking whether it exists.
        query = query.filter(Link.channel_id == channel_id)

    if q:
        parsed = parse_query(q)
        postgres = dialect_of(db) == "postgresql"

        if parsed.include:
            if postgres:
                query = query.filter(fts_document(Link.raw_text, Link.url).op("@@")(fts_query(parsed.include)))
                ranked = True
            else:
                like = f"%{parsed.include}%"
                query = query.filter(or_(Link.url.ilike(like), Link.raw_text.ilike(like)))

        for term in parsed.exclude:
            # Negation as a SQL NOT rather than inside the tsquery: this is
            # what lets the user's text stay literal input to
            # plainto_tsquery instead of becoming a query language that has
            # to be sanitised.
            if postgres:
                query = query.filter(~fts_document(Link.raw_text, Link.url).op("@@")(fts_query(term)))
            else:
                like = f"%{term}%"
                query = query.filter(~or_(Link.url.ilike(like), func.coalesce(Link.raw_text, "").ilike(like)))

    return query, ranked
