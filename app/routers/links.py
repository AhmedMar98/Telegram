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
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import record as audit_record
from app.classifier import CATEGORIES
from app.database import get_db
from app.deps import get_current_user
from app.ingest import ingest_text, manual_channel
from app.linkquery import filtered_links as _filtered_query
from app.models import AuditLog, Channel, ClassificationFeedback, Link, SavedSearch, User
from app.schemas import (
    BulkDeleteRequest,
    BulkRecategorizeRequest,
    BulkResult,
    ClassificationFeedbackOut,
    CollectionHealth,
    FeedbackListResponse,
    LinkCategoryUpdate,
    LinkImportRequest,
    LinkImportResponse,
    LinkNotesUpdate,
    LinkOut,
    SavedSearchCreate,
    SavedSearchOut,
    SearchResponse,
    StatsResponse,
    StorageStats,
    VitalityStats,
)
from app.search import fts_rank, parse_query
from app.security import is_action_rate_limited, record_action_event
from app.storage import database_bytes, largest_table
from app.timeutil import utcnow

router = APIRouter(prefix="/links", tags=["links"])


SORT_OPTIONS = ("date", "domain", "category", "confidence", "checked", "domain_frequency")

# The collector runs every six hours. A day of silence is well past a single
# missed run, a FloodWait, or a slow schedule, so it is worth telling the
# user about rather than letting the collection quietly stop growing.
COLLECTOR_STALL_HOURS = 24


@router.get("", response_model=SearchResponse)
def search_links(
    q: str | None = Query(default=None, max_length=300),
    category: str | None = Query(default=None),
    favorite: bool | None = Query(default=None),
    alive: bool | None = Query(default=None),
    channel_id: int | None = Query(default=None),
    language: str | None = Query(default=None, max_length=10),
    domain: str | None = Query(default=None, max_length=300),
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
        domain=domain,
        include_archived=include_archived,
    )
    total = query.count()

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

    # count(*) rather than count(Link.id), and the WHERE mirrors
    # ix_links_ws_archived_domain exactly. Measured on 50k rows: with
    # count(id) the planner falls back to a bitmap heap scan (10.7ms, 1118
    # buffers) because id is not in the index; with count(*) it is an
    # index-only scan with zero heap fetches (1.9ms, 13 buffers). The two
    # counts are equivalent here because id is a NOT NULL primary key.
    domain_rows = db.execute(
        select(Link.domain, func.count())
        .where(Link.workspace_id == ws_id, Link.is_archived.is_(False))
        .group_by(Link.domain)
        .order_by(func.count().desc())
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

    # The collector writes one audit row per channel per run, so the most
    # recent of them is when collection last actually happened. Read from
    # the audit trail rather than a new column: the fact is already
    # recorded, and a second copy could disagree with it.
    last_run_at = (
        db.query(func.max(AuditLog.created_at))
        .filter(AuditLog.workspace_id == ws_id, AuditLog.action == "collector.run")
        .scalar()
    )
    hours_since = (now - last_run_at).total_seconds() / 3600 if last_run_at else None
    # The schedule is six-hourly; a full day of silence is well past a
    # missed run or a FloodWait, and is worth surfacing. A workspace that
    # has never run the collector is not stalled — it may simply be
    # manual-only — so a missing timestamp is never a warning.
    looks_stalled = hours_since is not None and hours_since > COLLECTOR_STALL_HOURS

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
        storage=StorageStats(
            database_bytes=database_bytes(db),
            link_count=total_links,
            largest_table=largest_table(db),
        ),
        collection=CollectionHealth(
            last_run_at=last_run_at,
            hours_since_last_run=round(hours_since, 1) if hours_since is not None else None,
            looks_stalled=looks_stalled,
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


# The filter keys a saved search is allowed to carry. Validated rather than
# trusted: without this a saved search is a stored blob that gets replayed
# into a later request, so an unexpected key would be a way to smuggle a
# parameter past the caller who created it.
SAVEABLE_FILTERS = frozenset(
    {"q", "category", "favorite", "alive", "channel_id", "language", "domain", "include_archived", "sort"}
)

MAX_SAVED_SEARCHES = 50


@router.get("/saved", response_model=list[SavedSearchOut])
def list_saved_searches(
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
) -> list[SavedSearchOut]:
    rows = (
        db.query(SavedSearch)
        .filter(SavedSearch.workspace_id == current_user.workspace_id)
        .order_by(SavedSearch.name.asc())
        .all()
    )
    return [
        SavedSearchOut(id=r.id, name=r.name, filters=json.loads(r.filters), created_at=r.created_at) for r in rows
    ]


@router.post("/saved", response_model=SavedSearchOut, status_code=status.HTTP_201_CREATED)
def create_saved_search(
    payload: SavedSearchCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SavedSearchOut:
    """Store a filter combination under a name. Re-using a name replaces it."""
    unknown = sorted(set(payload.filters) - SAVEABLE_FILTERS)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unknown filter(s): {', '.join(unknown)}",
        )

    existing = (
        db.query(SavedSearch)
        .filter(SavedSearch.workspace_id == current_user.workspace_id, SavedSearch.name == payload.name)
        .first()
    )
    if existing is None:
        count = (
            db.query(func.count(SavedSearch.id))
            .filter(SavedSearch.workspace_id == current_user.workspace_id)
            .scalar()
        )
        if count and count >= MAX_SAVED_SEARCHES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"at most {MAX_SAVED_SEARCHES} saved searches per workspace",
            )
        existing = SavedSearch(workspace_id=current_user.workspace_id, name=payload.name, filters="{}")
        db.add(existing)

    existing.filters = json.dumps(payload.filters, ensure_ascii=False)
    db.commit()
    db.refresh(existing)
    return SavedSearchOut(
        id=existing.id, name=existing.name, filters=json.loads(existing.filters), created_at=existing.created_at
    )


@router.delete("/saved/{saved_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_saved_search(
    saved_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
) -> Response:
    row = (
        db.query(SavedSearch)
        .filter(SavedSearch.id == saved_id, SavedSearch.workspace_id == current_user.workspace_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="saved search not found")
    db.delete(row)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# Schemes a stored URL may be redirected to. The extractor only ever
# produces http(s), so anything else in the column means the row was
# written by something other than the extractor — and a redirect to
# javascript:, data: or file: would be an XSS or a local-file probe
# handed out by our own domain.
_REDIRECTABLE_SCHEMES = ("http://", "https://")


@router.get("/{link_id}/open")
def open_link(
    link_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RedirectResponse:
    """Count an open, then redirect to the link.

    Authenticated and workspace-scoped, so this is not an open redirect: a
    caller can only ever be sent to a URL already in their own workspace,
    and an id from another workspace 404s exactly like a nonexistent one.
    The scheme is re-checked at redirect time anyway — a URL stored years
    ago should not be trusted more than one stored today.

    The count is deliberately incomplete and the dashboard says so: a user
    who copies the URL instead of clicking through is invisible here.
    Presenting it as total opens would be a fabricated metric.
    """
    link = _owned_link(db, link_id, current_user)

    if not link.url.lower().startswith(_REDIRECTABLE_SCHEMES):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="this link cannot be opened through the redirect",
        )

    link.click_count = (link.click_count or 0) + 1
    db.commit()
    # 302, not 301: a permanent redirect would be cached by the browser and
    # every later open would skip the server, silently freezing the count.
    return RedirectResponse(url=link.url, status_code=status.HTTP_302_FOUND)


@router.get("/random", response_model=list[LinkOut])
def random_links(
    count: int = Query(default=5, ge=1, le=20),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[Link]:
    """A handful of links at random, for rediscovering an old collection.

    ``ORDER BY random() LIMIT n`` is a full scan and would be the wrong
    tool on a large table, but it is correct on both backends and the
    measured corpus size here (see docs/07-phase0-measurements.md) is
    nowhere near where that matters. Replacing it with something cleverer
    before it is a measured problem would be guessing.
    """
    query, _ = _filtered_query(db, current_user.workspace_id, q=None, category=None)
    return query.order_by(func.random()).limit(count).all()


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


@router.patch("/{link_id}/notes", response_model=LinkOut)
def set_notes(
    link_id: int,
    payload: LinkNotesUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Link:
    """Attach the user's own note to a link.

    Stored separately from ``raw_text``, which is what the source message
    said. Conflating them would let editing a note destroy the original
    context the classifier and the search both read.
    """
    link = _owned_link(db, link_id, current_user)
    # An empty note is an absent note, not an empty string — otherwise
    # "cleared" and "never written" become indistinguishable.
    link.notes = payload.notes.strip() or None
    db.commit()
    db.refresh(link)
    return link


@router.post("/{link_id}/pin", response_model=LinkOut)
def set_pinned(
    link_id: int,
    is_pinned: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Link:
    """Mark a link as a permanent reference.

    Deliberately not the same flag as favourite: starring is affection,
    pinning is "this is the one I keep coming back to". With one flag, the
    links you like bury the one you rely on.
    """
    link = _owned_link(db, link_id, current_user)
    link.is_pinned = is_pinned
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
    q: str | None = Query(default=None, max_length=300),
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
    query, _ = _filtered_query(db, current_user.workspace_id, q=q, category=category, include_archived=True)

    audit_record(
        db,
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        action="link.export",
        detail=f"format=csv category={category or 'all'} q={q or ''}",
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
    q: str | None = Query(default=None, max_length=300),
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
    query, _ = _filtered_query(db, current_user.workspace_id, q=q, category=category, include_archived=True)

    audit_record(
        db,
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        action="link.export",
        detail=f"format=json category={category or 'all'} q={q or ''}",
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


# Markdown link text is delimited by these; leaving them raw lets a message
# body break out of the [label](url) it is placed in. Escaped rather than
# stripped so the reader still sees what the message actually said.
_MARKDOWN_ESCAPES = str.maketrans({"[": r"\[", "]": r"\]", "(": r"\(", ")": r"\)", "\\": "\\\\"})


def _markdown_safe(text: str) -> str:
    return text.translate(_MARKDOWN_ESCAPES).replace("\n", " ").strip()


@router.get("/export.md")
def export_links_markdown(
    category: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=300),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    """Stream a search's results as a readable Markdown document.

    CSV and JSON are for moving data to another program. This one is for
    moving it to a person: grouped by category, with the message text that
    labelled each link, so a search like "دورات بايثون" becomes something
    that can be pasted into a note or a message and read as-is.

    Unlike the other two formats this one honours the *search term*, since
    a curated share is the point — exporting the whole workspace as prose
    would not be.
    """
    query, _ = _filtered_query(db, current_user.workspace_id, q=q, category=category, include_archived=True)

    audit_record(
        db,
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        action="link.export",
        detail=f"format=md category={category or 'all'} q={q or ''}",
    )
    db.commit()

    def rows():
        heading = "# روابط" + (f" — بحث: {_markdown_safe(q)}" if q else "")
        yield heading + "\n\n"

        current_category: str | None = None
        # Ordered by category so the grouping needs no buffering: rows are
        # still streamed one at a time, which is what keeps a large export
        # from having to fit in memory on a small instance.
        for link in query.order_by(Link.category.asc(), Link.created_at.desc()).yield_per(200):
            if link.category != current_category:
                current_category = link.category
                yield f"\n## {current_category}\n\n"
            label = _markdown_safe((link.raw_text or "")[:120]) or link.domain
            line = f"- [{label}]({link.url})"
            if link.is_alive is False:
                line += "  _(رابط ميت)_"
            yield line + "\n"

    return StreamingResponse(
        rows(),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="links.md"'},
    )
