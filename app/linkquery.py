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
from app.search import fts_document, fts_query, fts_rank, parse_query


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


# --- ordering --------------------------------------------------------------

# The sorts the product offers. Lives here rather than in the router
# because the ordering they name is applied here: a caller that knows the
# option names but not how they are applied would be free to invent its
# own ordering for the same name, which is the divergence this module
# exists to stop.
SORT_OPTIONS: tuple[str, ...] = (
    "date",
    "domain",
    "category",
    "confidence",
    "checked",
    "domain_frequency",
)

DEFAULT_SORT = "date"


def ordered_links(
    query: OrmQuery[Link],
    *,
    sort: str = DEFAULT_SORT,
    ranked: bool = False,
    q: str | None = None,
) -> OrmQuery[Link]:
    """Apply one of ``SORT_OPTIONS`` and always end on a unique column.

    **Why the trailing ``Link.id``.** Every sort key here is non-unique —
    two links collected in the same flush share a ``created_at`` to the
    microsecond, and a whole workspace can share one ``domain`` or one
    ``category``. SQL leaves the order of tied rows undefined, and
    ``OFFSET``/``LIMIT`` paginate by *position* in that undefined order,
    so the same query run twice may place a tied row on page 1 the first
    time and page 2 the second. The reader sees one link twice and never
    sees another — with no error, and no way to tell from the response
    that anything was skipped.

    A unique final key removes the tie entirely: with ``id`` last, no two
    rows compare equal, so the total order is fully determined and page
    boundaries fall in the same place on every run. This is AC-SR03
    ("ترتيب النتائج ثابت عند تساوي القيم باستخدام tie-breaker محدد") and
    it is why the column is appended to *every* branch below rather than
    only the ones that looked risky.

    ``id`` rather than ``url`` or ``fingerprint``: it is the primary key,
    so uniqueness is enforced by the database rather than assumed, and it
    is already indexed. Descending, so it agrees with the recency the
    other keys express — of two links stored in the same flush, the one
    inserted second is the newer.
    """
    if sort == "domain":
        query = query.order_by(Link.domain.asc(), Link.created_at.desc())
    elif sort == "category":
        query = query.order_by(Link.category.asc(), Link.created_at.desc())
    elif sort == "confidence":
        query = query.order_by(Link.confidence.desc(), Link.created_at.desc())
    elif sort == "domain_frequency":
        # Groups the collection by how much of it comes from the same place,
        # busiest source first. A window function rather than a join on a
        # grouped subquery — verified to run identically on SQLite (>= 3.25)
        # and Postgres, so the two backends do not diverge here.
        frequency = func.count().over(partition_by=Link.domain)
        query = query.order_by(frequency.desc(), Link.domain.asc(), Link.created_at.desc())
    elif sort == "checked":
        # Most recently verified first. Different question from "newest":
        # this answers "what do I currently know to be working?", which the
        # collection date cannot.
        query = query.order_by(Link.last_checked_at.desc(), Link.created_at.desc())
    elif ranked and q:
        # Default "date" sort yields to relevance when there is a search
        # term and Postgres can actually rank it — a plain date order would
        # bury the best match under whatever was collected most recently.
        query = query.order_by(
            fts_rank(Link.raw_text, Link.url, parse_query(q).include).desc(), Link.created_at.desc()
        )
    else:
        query = query.order_by(Link.created_at.desc())

    return query.order_by(Link.id.desc())
