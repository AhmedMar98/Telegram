"""
Multi-tenant support.

Since SQLite has no native Row-Level Security (RLS) like Postgres, we enforce
tenant isolation at the ORM layer:
    1. Every data row has a `tenant_id` column (default 1 = single-tenant compat).
    2. `TenantContext` is a context manager that injects tenant_id into new rows.
    3. `TenantMiddleware` extracts tenant_id from the API key on every request.
    4. `tenant_filter()` applies `WHERE tenant_id = current` to all queries.

For production multi-tenant SaaS, migrate to PostgreSQL where RLS is enforced
at the database level (cannot be bypassed by app bugs).
"""
from __future__ import annotations

import hashlib
import secrets
from contextvars import ContextVar
from dataclasses import dataclass

from fastapi import Header, HTTPException, Request
from loguru import logger

from .database import SessionLocal
from .models import Tenant

# ContextVar propagates tenant_id across async calls in the same request
_current_tenant: ContextVar[TenantContext | None] = ContextVar(
    "_current_tenant", default=None
)


@dataclass
class TenantContext:
    """The current tenant, propagated via ContextVar."""
    tenant_id: int
    slug: str
    plan: str

    @classmethod
    def default(cls) -> TenantContext:
        """Fallback for single-tenant deployments (no API key required)."""
        return cls(tenant_id=1, slug="default", plan="free")


def get_current_tenant() -> TenantContext:
    """Get the current tenant from context, falling back to default."""
    return _current_tenant.get() or TenantContext.default()


def set_current_tenant(ctx: TenantContext) -> None:
    _current_tenant.set(ctx)


def reset_current_tenant() -> None:
    _current_tenant.set(None)


# ============================================================
# API key management
# ============================================================

def hash_api_key(plaintext: str) -> str:
    """SHA-256 hash of an API key (we never store plaintext)."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_api_key(slug: str) -> str:
    """Generate a new API key like 'lip_<slug>_<32hex>'."""
    return f"lip_{slug}_{secrets.token_hex(16)}"


def create_tenant(name: str, slug: str, plan: str = "free") -> tuple[Tenant, str]:
    """Create a new tenant + return (tenant_row, plaintext_api_key)."""
    plaintext = generate_api_key(slug)
    api_key_hash = hash_api_key(plaintext)
    with SessionLocal() as db:
        existing = db.query(Tenant).filter(Tenant.slug == slug).first()
        if existing:
            raise ValueError(f"Tenant slug '{slug}' already exists")
        tenant = Tenant(
            name=name,
            slug=slug,
            api_key_hash=api_key_hash,
            plan=plan,
            is_active=True,
        )
        db.add(tenant)
        db.commit()
        db.refresh(tenant)
        logger.info("[tenant] created: id={} slug={} plan={}", tenant.id, slug, plan)
        return tenant, plaintext


def resolve_tenant_by_api_key(api_key: str) -> TenantContext | None:
    """Look up tenant by API key. Returns None if not found / inactive."""
    if not api_key or not api_key.startswith("lip_"):
        return None
    key_hash = hash_api_key(api_key)
    with SessionLocal() as db:
        tenant = db.query(Tenant).filter(
            Tenant.api_key_hash == key_hash,
            Tenant.is_active.is_(True),
        ).first()
        if tenant is None:
            return None
        return TenantContext(
            tenant_id=tenant.id,
            slug=tenant.slug,
            plan=tenant.plan,
        )


# ============================================================
# FastAPI dependency
# ============================================================

async def tenant_dependency(
    request: Request,
    x_api_key: str | None = Header(None, alias="X-API-Key"),
) -> TenantContext:
    """
    FastAPI dependency: extract tenant from X-API-Key header.

    For backward compatibility, requests without X-API-Key default to
    tenant_id=1 (single-tenant mode). When settings.saas_mode is true,
    missing API key returns 401.
    """
    from .config import get_settings
    saas_mode = get_settings().saas_mode

    if x_api_key:
        ctx = resolve_tenant_by_api_key(x_api_key)
        if ctx is None:
            raise HTTPException(401, "Invalid or inactive API key")
        set_current_tenant(ctx)
        return ctx

    if saas_mode:
        raise HTTPException(401, "X-API-Key header required in SaaS mode")

    # Single-tenant fallback
    ctx = TenantContext.default()
    set_current_tenant(ctx)
    return ctx


# ============================================================
# Tenant-scoped query helper
# ============================================================

def tenant_filter(model_cls, query):
    """Apply WHERE tenant_id = current_tenant.id to a SQLAlchemy query."""
    ctx = get_current_tenant()
    return query.filter(model_cls.tenant_id == ctx.tenant_id)
