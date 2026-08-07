"""API routes — FastAPI routers for REST endpoints + web panel."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import (
    Account,
    Link,
    LinkCategory,
    LinkStatus,
    LLMProviderState,
)
from ..search.hybrid import HybridSearch

# ============================================================
# Routers
# ============================================================

router = APIRouter()
api_router = APIRouter(prefix="/api/v1")

templates = Jinja2Templates(directory="app/web/templates")

# Shared search engine (initialized lazily)
_search_engine: HybridSearch | None = None


def get_search_engine() -> HybridSearch:
    global _search_engine
    if _search_engine is None:
        from ..main import get_orchestrator

        orch = get_orchestrator()
        _search_engine = HybridSearch(llm_orchestrator=orch)
    return _search_engine


# ============================================================
# Web pages
# ============================================================


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    """Main dashboard with KPIs."""
    total = db.query(Link).count()
    alive = db.query(Link).filter(Link.alive.is_(True)).count()
    dead = db.query(Link).filter(Link.alive.is_(False)).count()
    new = db.query(Link).filter(Link.status == LinkStatus.NEW.value).count()
    classified = (
        db.query(Link)
        .filter(
            Link.status.in_(
                [LinkStatus.CLASSIFIED.value, LinkStatus.ALIVE.value, LinkStatus.DEAD.value]
            )
        )
        .count()
    )

    # Category breakdown
    cat_rows = db.execute(select(Link.category, func.count(Link.id)).group_by(Link.category)).all()
    categories = {cat: count for cat, count in cat_rows}

    # LLM providers
    providers = db.query(LLMProviderState).all()

    # Recent links
    recent_links = db.query(Link).order_by(Link.discovered_at.desc()).limit(10).all()

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "total": total,
            "alive": alive,
            "dead": dead,
            "new": new,
            "classified": classified,
            "categories": categories,
            "providers": providers,
            "recent_links": recent_links,
        },
    )


@router.get("/links", response_class=HTMLResponse)
async def links_page(
    request: Request,
    category: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
):
    """Browse all links with optional filters."""
    q = db.query(Link).filter(Link.archived.is_(False))
    if category:
        q = q.filter(Link.category == category)
    if status:
        q = q.filter(Link.status == status)
    links = q.order_by(Link.discovered_at.desc()).limit(200).all()
    return templates.TemplateResponse(
        request=request,
        name="links.html",
        context={
            "links": links,
            "categories": [c.value for c in LinkCategory],
            "statuses": [s.value for s in LinkStatus],
            "current_category": category,
            "current_status": status,
        },
    )


@router.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request, db: Session = Depends(get_db)):
    """Manage Telegram userbot accounts."""
    accounts = db.query(Account).all()
    return templates.TemplateResponse(
        request=request,
        name="accounts.html",
        context={"accounts": accounts},
    )


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    """Live log viewer (WebSocket-based)."""
    return templates.TemplateResponse(
        request=request,
        name="logs.html",
        context={},
    )


@router.get("/tenants", response_class=HTMLResponse)
async def tenants_page(request: Request, db: Session = Depends(get_db)):
    """Multi-tenant management panel."""
    from ..models import Tenant

    tenants = db.query(Tenant).all()
    return templates.TemplateResponse(
        request=request,
        name="tenants.html",
        context={"tenants": tenants},
    )


# ============================================================
# REST API
# ============================================================


@api_router.get("/links")
async def list_links(
    category: str | None = None,
    alive: bool | None = None,
    limit: int = Query(50, le=500),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    q = db.query(Link).filter(Link.archived.is_(False))
    if category:
        q = q.filter(Link.category == category)
    if alive is not None:
        q = q.filter(Link.alive.is_(alive))
    total = q.count()
    links = q.order_by(Link.discovered_at.desc()).offset(offset).limit(limit).all()
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [_link_to_dict(link) for link in links],
    }


@api_router.get("/links/{link_id}")
async def get_link(link_id: int, db: Session = Depends(get_db)):
    link = db.get(Link, link_id)
    if link is None:
        raise HTTPException(404, "Link not found")
    item = _link_to_dict(link)
    item["classification_logs"] = [
        {
            "tier": log.tier,
            "category": log.category,
            "confidence": log.confidence,
            "rule_matched": log.rule_matched,
            "llm_provider": log.llm_provider,
            "semantic_reused": log.semantic_reused,
            "created_at": log.created_at.isoformat() if log.created_at else None,
        }
        for log in link.classification_logs
    ]
    item["vitality_checks"] = [
        {
            "checked_at": v.checked_at.isoformat() if v.checked_at else None,
            "http_status": v.http_status,
            "alive": v.alive,
            "response_time_ms": v.response_time_ms,
            "error": v.error,
        }
        for v in link.vitality_checks
    ]
    return item


@api_router.post("/links/{link_id}/check")
async def trigger_vitality_check(link_id: int, db: Session = Depends(get_db)):
    """Manually trigger a vitality check for a link."""
    link = db.get(Link, link_id)
    if link is None:
        raise HTTPException(404, "Link not found")
    from ..vitality.checker import VitalityChecker

    checker = VitalityChecker(delay_min=0, delay_max=0)
    result = await checker.check_link(link, db=db)
    return {
        "link_id": link_id,
        "alive": result.alive,
        "http_status": result.http_status,
        "response_time_ms": result.response_time_ms,
        "error": result.error,
    }


@api_router.get("/search")
async def api_search(
    q: str = Query(..., min_length=1),
    category: str | None = None,
    alive_only: bool = False,
    limit: int = Query(20, le=100),
):
    engine = get_search_engine()
    results = await engine.search(q, category=category, alive_only=alive_only, limit=limit)
    return {
        "query": q,
        "count": len(results),
        "items": [
            {
                "link_id": r.link_id,
                "url": r.url,
                "title": r.title,
                "description": r.description,
                "category": r.category,
                "status": r.status,
                "alive": r.alive,
                "score": r.score,
                "score_breakdown": r.score_breakdown,
            }
            for r in results
        ],
    }


@api_router.get("/stats")
async def api_stats(db: Session = Depends(get_db)):
    total = db.query(Link).count()
    alive = db.query(Link).filter(Link.alive.is_(True)).count()
    dead = db.query(Link).filter(Link.alive.is_(False)).count()
    new = db.query(Link).filter(Link.status == LinkStatus.NEW.value).count()
    by_cat = db.execute(
        select(Link.category, func.count(Link.id))
        .where(Link.archived.is_(False))
        .group_by(Link.category)
    ).all()
    providers = db.query(LLMProviderState).all()
    return {
        "links": {
            "total": total,
            "alive": alive,
            "dead": dead,
            "new": new,
        },
        "by_category": {c: n for c, n in by_cat},
        "providers": [
            {
                "name": p.name,
                "healthy": p.is_healthy,
                "requests_today": p.requests_today,
                "requests_total": p.requests_total,
                "quota_limit": p.quota_limit,
                "quota_remaining": p.quota_remaining,
                "cooldown_until": p.cooldown_until.isoformat() if p.cooldown_until else None,
                "last_error": p.last_error,
            }
            for p in providers
        ],
    }


@api_router.post("/links/{link_id}/click")
async def record_click(link_id: int, db: Session = Depends(get_db)):
    """Increment click count (used by the bot when sending a link)."""
    link = db.get(Link, link_id)
    if link is None:
        raise HTTPException(404, "Link not found")
    link.click_count = (link.click_count or 0) + 1
    db.commit()
    return {"click_count": link.click_count}


def _link_to_dict(link: Link) -> dict:
    return {
        "id": link.id,
        "url": link.url,
        "title": link.title,
        "description": link.description,
        "category": link.category,
        "status": link.status,
        "classification_tier": link.classification_tier,
        "confidence": link.confidence,
        "alive": link.alive,
        "http_status": link.http_status,
        "source_channel": link.source_channel,
        "discovered_at": link.discovered_at.isoformat() if link.discovered_at else None,
        "last_checked_at": link.last_checked_at.isoformat() if link.last_checked_at else None,
        "click_count": link.click_count,
        "search_hit_count": link.search_hit_count,
    }
