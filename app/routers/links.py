"""Search and browse collected links.

Full-text search uses native Postgres ``to_tsvector``/``plainto_tsquery``
in production (fast, free, no extra service) and falls back to a plain
``ILIKE`` scan on SQLite for local development and the test suite, since
SQLite has no ``to_tsvector`` builtin. Both paths are always additionally
filtered by ``workspace_id`` — this is what makes cross-tenant data leaks
(R-03) structurally impossible rather than merely "usually avoided".
"""

from __future__ import annotations

import csv
import io
import json
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Query as OrmQuery
from sqlalchemy.orm import Session

from app.audit import record as audit_record
from app.classifier import CATEGORIES
from app.database import get_db
from app.deps import get_current_user
from app.ingest import ingest_text, manual_channel
from app.models import Channel, ClassificationFeedback, Link, User
from app.schemas import (
    BulkDeleteRequest,
    BulkRecategorizeRequest,
    BulkResult,
    ClassificationFeedbackOut,
    FeedbackListResponse,
    LinkCategoryUpdate,
    LinkImportRequest,
    LinkImportResponse,
    LinkOut,
    SearchResponse,
    StatsResponse,
    VitalityStats,
)
from app.search import fts_document, fts_query, fts_rank
from app.security import is_action_rate_limited, record_action_event
from app.timeutil import utcnow

router = APIRouter(prefix="/links", tags=["links"])


def _dialect(db: Session) -> str:
    return db.bind.dialect.name if db.bind is not None else "sqlite"


SORT_OPTIONS = ("date", "domain", "category", "confidence", "checked")


def _filtered_query(
    db: Session,
    workspace_id: int,
    *,
    q: str | None,
    category: str | None,
    favorite: bool | None = None,
    alive: bool | None = None,
    channel_id: int | None = None,
    language: str | None = None,
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
        query = query.filter(Link.category == category)

    if favorite is not None:
        query = query.filter(Link.is_favorite == favorite)

    if alive is not None:
        # is_alive is nullable (never checked yet); an explicit filter only
        # ever means "show me a definite answer", never "include unknowns".
        query = query.filter(Link.is_alive == alive)

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
        if _dialect(db) == "postgresql":
            query = query.filter(fts_document(Link.raw_text, Link.url).op("@@")(fts_query(q)))
            ranked = True
        else:
            like = f"%{q}%"
            query = query.filter(or_(Link.url.ilike(like), Link.raw_text.ilike(like)))

    return query, ranked


@router.get("", response_model=SearchResponse)
def search_links(
    q: str | None = Query(default=None, max_length=300),
    category: str | None = Query(default=None),
    favorite: bool | None = Query(default=None),
    alive: bool | None = Query(default=None),
    channel_id: int | None = Query(default=None),
    language: str | None = Query(default=None, max_length=10),
    include_archived: bool = Query(default=False),
    sort: str = Query(default="date"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SearchResponse:
    if sort not in SORT_OPTIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"sort must be one of: {', '.join(SORT_OPTIONS)}",
        )

    query, ranked = _filtered_query(
        db,
        current_user.workspace_id,
        q=q,
        category=category,
        favorite=favorite,
        alive=alive,
        channel_id=channel_id,
        language=language,
        include_archived=include_archived,
    )
    total = query.count()

    if sort == "domain":
        query = query.order_by(Link.domain.asc(), Link.created_at.desc())
    elif sort == "category":
        query = query.order_by(Link.category.asc(), Link.created_at.desc())
    elif sort == "confidence":
        query = query.order_by(Link.confidence.desc(), Link.created_at.desc())
    elif sort == "checked":
        # Most recently verified first. Different question from "newest":
        # this answers "what do I currently know to be working?", which the
        # collection date cannot.
        query = query.order_by(Link.last_checked_at.desc(), Link.created_at.desc())
    elif ranked and q:
        # Default "date" sort yields to relevance when there is a search
        # term and Postgres can actually rank it — a plain date order would
        # bury the best match under whatever was collected most recently.
        query = query.order_by(fts_rank(Link.raw_text, Link.url, q).desc(), Link.created_at.desc())
    else:
        query = query.order_by(Link.created_at.desc())

    items = query.offset((page - 1) * page_size).limit(page_size).all()
    return SearchResponse(
        total=total, page=page, page_size=page_size, items=[LinkOut.model_validate(i) for i in items]
    )


@router.get("/stats", response_model=StatsResponse)
def stats(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)) -> StatsResponse:
    ws_id = current_user.workspace_id
    total_links = db.query(Link).filter(Link.workspace_id == ws_id).count()
    total_channels = db.query(Channel).filter(Channel.workspace_id == ws_id).count()

    rows = db.execute(
        select(Link.category, func.count(Link.id)).where(Link.workspace_id == ws_id).group_by(Link.category)
    ).all()
    by_category: dict[str, int] = {row[0]: row[1] for row in rows}

    domain_rows = db.execute(
        select(Link.domain, func.count(Link.id))
        .where(Link.workspace_id == ws_id)
        .group_by(Link.domain)
        .order_by(func.count(Link.id).desc())
        .limit(10)
    ).all()

    # One grouped query for the vitality split rather than three counts:
    # is_alive has exactly three states (True / False / NULL = never
    # checked), so grouping on it answers all three at once.
    vitality_rows = db.execute(
        select(Link.is_alive, func.count(Link.id)).where(Link.workspace_id == ws_id).group_by(Link.is_alive)
    ).all()
    vitality_counts = {row[0]: row[1] for row in vitality_rows}

    archived = (
        db.query(func.count(Link.id)).filter(Link.workspace_id == ws_id, Link.is_archived.is_(True)).scalar() or 0
    )

    deadest_rows = db.execute(
        select(Link.domain, func.count(Link.id))
        .where(Link.workspace_id == ws_id, Link.is_alive.is_(False))
        .group_by(Link.domain)
        .order_by(func.count(Link.id).desc())
        .limit(10)
    ).all()

    now = utcnow()
    added_this_week = (
        db.query(func.count(Link.id))
        .filter(Link.workspace_id == ws_id, Link.created_at >= now - timedelta(days=7))
        .scalar()
        or 0
    )
    added_this_month = (
        db.query(func.count(Link.id))
        .filter(Link.workspace_id == ws_id, Link.created_at >= now - timedelta(days=30))
        .scalar()
        or 0
    )

    return StatsResponse(
        total_links=total_links,
        total_channels=total_channels,
        by_category=by_category,
        top_domains=[(row[0], row[1]) for row in domain_rows],
        added_this_week=added_this_week,
        added_this_month=added_this_month,
        vitality=VitalityStats(
            alive=vitality_counts.get(True, 0),
            dead=vitality_counts.get(False, 0),
            unchecked=vitality_counts.get(None, 0),
            archived=archived,
            deadest_domains=[(row[0], row[1]) for row in deadest_rows],
        ),
    )


# Generous on purpose: a person pasting several messages in a row must
# never be throttled. This stops a scripted flood of calls, not normal use.
LINK_ADD_LIMIT = 60
LINK_ADD_WINDOW_MINUTES = 5


@router.post("", response_model=LinkImportResponse, status_code=status.HTTP_201_CREATED)
def add_links(
    payload: LinkImportRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> LinkImportResponse:
    """Add links by pasting text — one URL, a list, or a whole message.

    This is the entry point that does not depend on Telegram API access at
    all, so the platform is usable before (or without) the automated
    collector being wired up.
    """
    scope_id = str(current_user.workspace_id)
    if is_action_rate_limited(
        db, "link_add", scope_id, limit=LINK_ADD_LIMIT, window_minutes=LINK_ADD_WINDOW_MINUTES
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many submissions, please slow down",
        )
    record_action_event(db, "link_add", scope_id)

    channel = manual_channel(db, current_user.workspace_id)
    summary = ingest_text(db, workspace_id=current_user.workspace_id, channel_id=channel.id, text=payload.text)
    audit_record(
        db,
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        action="link.manual_add",
        detail=f"{summary.stored} new, {summary.duplicates} duplicate(s)",
    )
    db.commit()
    return LinkImportResponse(found=summary.total_found, stored=summary.stored, duplicates=summary.duplicates)


def _owned_link(db: Session, link_id: int, user: User) -> Link:
    """Fetch a link, scoped to the caller's workspace.

    Scoping the lookup itself (rather than fetching then checking) means a
    link belonging to another workspace is indistinguishable from one that
    does not exist, so ids cannot be probed for existence.
    """
    link = db.query(Link).filter(Link.id == link_id, Link.workspace_id == user.workspace_id).first()
    if link is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="link not found")
    return link


@router.delete("/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_link(
    link_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
) -> Response:
    """Remove a link permanently.

    The collector will re-add it if the source message is rescanned, since
    dedup is keyed on the URL rather than on a tombstone. Deleting is for
    pruning noise, not for blocking a URL.
    """
    link = _owned_link(db, link_id, current_user)
    audit_record(
        db,
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        action="link.delete",
        target_type="link",
        target_id=str(link.id),
        detail=link.url[:500],
    )
    db.delete(link)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/{link_id}", response_model=LinkOut)
def recategorize_link(
    link_id: int,
    payload: LinkCategoryUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Link:
    """Override the automatic classification.

    A human correction is recorded with full confidence and marked as
    ``manual`` so it is never silently revised by a later automatic pass.
    """
    if payload.category not in CATEGORIES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"category must be one of: {', '.join(CATEGORIES)}",
        )

    link = _owned_link(db, link_id, current_user)
    previous = link.category
    if payload.category != previous:
        # Recorded before the row is mutated, because the whole value of the
        # feedback is what the classifier *said* — once the link is updated
        # that answer is gone. Only a real change is recorded: re-selecting
        # the category that is already set is not a correction, and logging
        # it would dilute the signal with no-ops.
        db.add(
            ClassificationFeedback(
                workspace_id=current_user.workspace_id,
                link_id=link.id,
                url=link.url,
                previous_category=previous,
                new_category=payload.category,
                previous_confidence=link.confidence,
                previous_matched_rule=link.matched_rule,
            )
        )
    link.category = payload.category
    link.confidence = 1.0
    link.classified_by = "manual"
    audit_record(
        db,
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        action="link.recategorize",
        target_type="link",
        target_id=str(link.id),
        detail=f"{previous} -> {payload.category}",
    )
    db.commit()
    db.refresh(link)
    return link


@router.get("/feedback", response_model=FeedbackListResponse)
def list_feedback(
    link_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> FeedbackListResponse:
    """Corrections a human made to the classifier, newest first.

    Two questions, one endpoint: unfiltered it is the workspace's learning
    signal — the concrete list of what the rules get wrong, which is what a
    new rule should be written from. With ``link_id`` it is one link's
    classification history.

    Bulk recategorization deliberately does not appear here. Correcting one
    link is a judgement about that link; sweeping a filter is a
    reorganization, and mixing thousands of swept rows into this list would
    bury the individual judgements that are actually informative.
    """
    query = db.query(ClassificationFeedback).filter(
        ClassificationFeedback.workspace_id == current_user.workspace_id
    )
    if link_id is not None:
        # No ownership check on link_id: the query is already workspace
        # scoped, so a foreign id returns an empty list rather than
        # revealing whether it exists.
        query = query.filter(ClassificationFeedback.link_id == link_id)

    total = query.count()
    rows = (
        query.order_by(ClassificationFeedback.created_at.desc(), ClassificationFeedback.id.desc())
        .limit(limit)
        .all()
    )
    return FeedbackListResponse(total=total, items=[ClassificationFeedbackOut.model_validate(r) for r in rows])


@router.post("/{link_id}/archive", response_model=LinkOut)
def set_archived(
    link_id: int,
    is_archived: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Link:
    """Hide a link from default results without deleting it.

    The intended use is dead links: they pile up and bury the working ones,
    but deleting them loses the record that the content existed and where
    it was posted. Pass ``?is_archived=false`` to bring one back.
    """
    link = _owned_link(db, link_id, current_user)
    link.is_archived = is_archived
    audit_record(
        db,
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        action="link.archive" if is_archived else "link.unarchive",
        target_type="link",
        target_id=str(link.id),
    )
    db.commit()
    db.refresh(link)
    return link


@router.post("/{link_id}/favorite", response_model=LinkOut)
def set_favorite(
    link_id: int,
    is_favorite: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Link:
    """Mark or unmark a link as a favorite. Pass ?is_favorite=false to clear it."""
    link = _owned_link(db, link_id, current_user)
    link.is_favorite = is_favorite
    db.commit()
    db.refresh(link)
    return link


@router.post("/bulk/delete", response_model=BulkResult)
def bulk_delete(
    payload: BulkDeleteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> BulkResult:
    """Delete every link matching the given search/category filter.

    Acts on exactly what the same filter would return from ``GET /links``,
    so "delete these results" in the UI is one call instead of one per
    row. An empty filter matches the whole workspace — still safely
    scoped to it, never beyond.
    """
    query, _ = _filtered_query(db, current_user.workspace_id, q=payload.q, category=payload.category)
    count = query.count()
    query.delete(synchronize_session=False)

    audit_record(
        db,
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        action="link.bulk_delete",
        detail=f"q={payload.q or ''} category={payload.category or ''} count={count}",
    )
    db.commit()
    return BulkResult(affected=count)


@router.post("/bulk/recategorize", response_model=BulkResult)
def bulk_recategorize(
    payload: BulkRecategorizeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> BulkResult:
    """Recategorize every link matching the given search/category filter."""
    if payload.new_category not in CATEGORIES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"category must be one of: {', '.join(CATEGORIES)}",
        )

    query, _ = _filtered_query(db, current_user.workspace_id, q=payload.q, category=payload.category)
    count = query.update(
        {"category": payload.new_category, "confidence": 1.0, "classified_by": "manual"},
        synchronize_session=False,
    )

    audit_record(
        db,
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        action="link.bulk_recategorize",
        detail=f"q={payload.q or ''} category={payload.category or ''} -> {payload.new_category} count={count}",
    )
    db.commit()
    return BulkResult(affected=count)


def _export_row(link: Link) -> dict:
    return {
        "url": link.url,
        "category": link.category,
        "confidence": round(link.confidence, 2),
        "classified_by": link.classified_by,
        "matched_rule": link.matched_rule,
        "source_type": link.source_type,
        "forwarded_from": link.forwarded_from,
        "language": link.language,
        "is_favorite": link.is_favorite,
        "domain": link.domain,
        "posted_at": link.posted_at.isoformat() if link.posted_at else None,
        "collected_at": link.created_at.isoformat() if link.created_at else None,
        "is_alive": link.is_alive,
        "status_category": link.status_category,
        "http_status": link.http_status,
        "last_checked_at": link.last_checked_at.isoformat() if link.last_checked_at else None,
        "last_alive_at": link.last_alive_at.isoformat() if link.last_alive_at else None,
        "is_archived": link.is_archived,
        "context": (link.raw_text or "")[:300],
    }


# The CSV header and the CSV body are both derived from the JSON row above,
# so the two export formats cannot drift apart. They used to be three
# hand-maintained lists in the same order, which is a bug waiting for the
# next column.
EXPORT_COLUMNS: tuple[str, ...] = tuple(
    _export_row(
        Link(
            url="",
            category="",
            confidence=0.0,
            classified_by="",
            source_type="",
            domain="",
            is_favorite=False,
        )
    )
)


def _csv_cell(column: str, row: dict) -> str:
    """Render one exported value for CSV, where there is no null."""
    value = row[column]
    if column == "confidence":
        return f"{value:.2f}"
    if value is None:
        return ""
    if column == "context":
        # Embedded newlines would split one link across several CSV rows
        # in tools that do not honour quoting.
        return str(value).replace("\n", " ")
    return str(value)


@router.get("/export.csv")
def export_links_csv(
    category: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    """Stream the workspace's links as CSV.

    Data portability matters more than usual here: the free database tier
    this runs on is time-limited, so being able to take the collection out
    without database access is part of the product, not a nicety.

    Rows are streamed rather than materialised so a large collection does
    not have to fit in memory on a small free instance.
    """
    # include_archived: an export is about completeness. Archiving hides a
    # link from the dashboard; silently omitting it from the user's own
    # data export would make the export a lie.
    query, _ = _filtered_query(db, current_user.workspace_id, q=None, category=category, include_archived=True)

    audit_record(
        db,
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        action="link.export",
        detail=f"format=csv category={category or 'all'}",
    )
    db.commit()

    def rows():
        buffer = io.StringIO()
        writer = csv.writer(buffer)

        def flush() -> str:
            value = buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
            return value

        writer.writerow(list(EXPORT_COLUMNS))
        yield flush()

        for link in query.order_by(Link.created_at.desc()).yield_per(200):
            row = _export_row(link)
            writer.writerow([_csv_cell(column, row) for column in EXPORT_COLUMNS])
            yield flush()

    return StreamingResponse(
        rows(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="links.csv"'},
    )


@router.get("/export.json")
def export_links_json(
    category: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    """Stream the workspace's links as a JSON array, for programmatic use.

    Symmetric with the CSV export (same rows, same filter), for callers
    that want structured data rather than a spreadsheet.
    """
    # include_archived: an export is about completeness. Archiving hides a
    # link from the dashboard; silently omitting it from the user's own
    # data export would make the export a lie.
    query, _ = _filtered_query(db, current_user.workspace_id, q=None, category=category, include_archived=True)

    audit_record(
        db,
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        action="link.export",
        detail=f"format=json category={category or 'all'}",
    )
    db.commit()

    def rows():
        yield "["
        first = True
        for link in query.order_by(Link.created_at.desc()).yield_per(200):
            prefix = "" if first else ","
            first = False
            yield prefix + json.dumps(_export_row(link), ensure_ascii=False)
        yield "]"

    return StreamingResponse(
        rows(),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="links.json"'},
    )
