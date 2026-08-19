"""Search and browse collected links.

Full-text search uses native Postgres ``to_tsvector``/``plainto_tsquery``
in production (fast, free, no extra service) and falls back to a plain
``ILIKE`` scan on SQLite for local development and the test suite, since
SQLite has no ``to_tsvector`` builtin. Both paths are always additionally
filtered by ``workspace_id`` — this is what makes cross-tenant data leaks
(R-03) structurally impossible rather than merely "usually avoided".
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models import Channel, Link, User
from app.schemas import LinkOut, SearchResponse, StatsResponse

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
            tsquery = func.plainto_tsquery("simple", q)
            haystack = func.to_tsvector("simple", func.coalesce(Link.raw_text, "") + " " + Link.url)
            query = query.filter(haystack.op("@@")(tsquery))
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
