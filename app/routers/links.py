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

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.audit import record as audit_record
from app.classifier import CATEGORIES
from app.database import get_db
from app.deps import get_current_user
from app.ingest import ingest_text, manual_channel
from app.models import Channel, Link, User
from app.schemas import (
    LinkCategoryUpdate,
    LinkImportRequest,
    LinkImportResponse,
    LinkOut,
    SearchResponse,
    StatsResponse,
)
from app.search import fts_document, fts_query

router = APIRouter(prefix="/links", tags=["links"])


@router.get("", response_model=SearchResponse)
def search_links(
    q: str | None = Query(default=None, max_length=300),
    category: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SearchResponse:
    query = db.query(Link).filter(Link.workspace_id == current_user.workspace_id)

    if category:
        query = query.filter(Link.category == category)

    if q:
        dialect = db.bind.dialect.name if db.bind is not None else "sqlite"
        if dialect == "postgresql":
            query = query.filter(fts_document(Link.raw_text, Link.url).op("@@")(fts_query(q)))
        else:
            like = f"%{q}%"
            query = query.filter(or_(Link.url.ilike(like), Link.raw_text.ilike(like)))

    total = query.count()
    items = query.order_by(Link.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
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


@router.get("/export.csv")
def export_links(
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
    query = db.query(Link).filter(Link.workspace_id == current_user.workspace_id)
    if category:
        query = query.filter(Link.category == category)

    audit_record(
        db,
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        action="link.export",
        detail=f"category={category or 'all'}",
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
            writer.writerow(
                [
                    link.url,
                    link.category,
                    f"{link.confidence:.2f}",
                    link.classified_by,
                    link.domain,
                    link.posted_at.isoformat() if link.posted_at else "",
                    link.created_at.isoformat() if link.created_at else "",
                    (link.raw_text or "").replace("\n", " ")[:300],
                ]
            )
            yield flush()

    return StreamingResponse(
        rows(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="links.csv"'},
    )
