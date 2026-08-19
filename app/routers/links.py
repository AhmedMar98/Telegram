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
from app.models import Channel, Link, User
from app.schemas import (
    BulkDeleteRequest,
    BulkRecategorizeRequest,
    BulkResult,
    LinkCategoryUpdate,
    LinkImportRequest,
    LinkImportResponse,
    LinkOut,
    SearchResponse,
    StatsResponse,
)
from app.search import fts_document, fts_query, fts_rank
from app.security import is_action_rate_limited, record_action_event

router = APIRouter(prefix="/links", tags=["links"])


def _dialect(db: Session) -> str:
    return db.bind.dialect.name if db.bind is not None else "sqlite"


def _filtered_query(
    db: Session, workspace_id: int, *, q: str | None, category: str | None
) -> tuple[OrmQuery[Link], bool]:
    """The base query shared by search, export and bulk actions.

    Returns the filtered query alongside whether it can be ranked by
    relevance (only true for a Postgres full-text search with a term).
    """
    query = db.query(Link).filter(Link.workspace_id == workspace_id)
    ranked = False

    if category:
        query = query.filter(Link.category == category)

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
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SearchResponse:
    query, ranked = _filtered_query(db, current_user.workspace_id, q=q, category=category)
    total = query.count()

    if ranked and q:
        # Most relevant first, then newest as a tiebreak among equally
        # relevant results — a search with no term keeps pure recency.
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
    return StatsResponse(total_links=total_links, total_channels=total_channels, by_category=by_category)


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
        "domain": link.domain,
        "posted_at": link.posted_at.isoformat() if link.posted_at else None,
        "collected_at": link.created_at.isoformat() if link.created_at else None,
        "context": (link.raw_text or "")[:300],
    }


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
    query, _ = _filtered_query(db, current_user.workspace_id, q=None, category=category)

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

        writer.writerow(
            ["url", "category", "confidence", "classified_by", "domain", "posted_at", "collected_at", "context"]
        )
        yield flush()

        for link in query.order_by(Link.created_at.desc()).yield_per(200):
            row = _export_row(link)
            writer.writerow(
                [
                    row["url"],
                    row["category"],
                    f"{row['confidence']:.2f}",
                    row["classified_by"],
                    row["domain"],
                    row["posted_at"] or "",
                    row["collected_at"] or "",
                    (row["context"] or "").replace("\n", " "),
                ]
            )
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
    query, _ = _filtered_query(db, current_user.workspace_id, q=None, category=category)

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
