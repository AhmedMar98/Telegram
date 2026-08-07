"""
Billing API routes.

Endpoints:
    POST /api/v1/billing/checkout
        Body: {"plan": "pro"}
        Returns: {"checkout_url": "https://..."}
    GET  /api/v1/billing/usage
        Returns: {"plan": "free", "used": 123, "limit": 1000, "percent": 12.3}
    POST /api/v1/billing/webhook
        Stripe webhook receiver (no auth, signed payload)

All endpoints except /webhook require X-API-Key (tenant auth).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from loguru import logger
from pydantic import BaseModel

from ..tenant import TenantContext, tenant_dependency
from .stripe_client import StripeClient, check_tenant_quota, increment_tenant_usage

router = APIRouter(prefix="/api/v1/billing")


class CheckoutRequest(BaseModel):
    plan: str  # pro | enterprise
    success_url: str
    cancel_url: str


@router.post("/checkout")
async def create_checkout(
    req: CheckoutRequest,
    tenant: TenantContext = Depends(tenant_dependency),
) -> dict:
    """Create a Stripe Checkout session for plan upgrade."""
    if req.plan not in ("pro", "enterprise"):
        raise HTTPException(400, "plan must be 'pro' or 'enterprise'")
    client = StripeClient()
    if not client.is_configured:
        raise HTTPException(503, "Stripe not configured")
    try:
        result = client.create_checkout_session(
            tenant_id=tenant.tenant_id,
            plan=req.plan,
            success_url=req.success_url,
            cancel_url=req.cancel_url,
        )
    except Exception as e:
        logger.exception("[billing] checkout failed")
        raise HTTPException(500, str(e)) from e
    return result


@router.get("/usage")
async def get_usage(
    tenant: TenantContext = Depends(tenant_dependency),
) -> dict:
    """Return current tenant's quota usage."""
    allowed, used, limit = check_tenant_quota(tenant.tenant_id)
    return {
        "plan": tenant.plan,
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used),
        "percent": round((used / limit * 100) if limit > 0 else 0, 2),
        "allowed": allowed,
    }


@router.post("/webhook")
async def stripe_webhook(request: Request) -> Response:
    """Receive Stripe webhook events (signature verified via webhook secret)."""
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    client = StripeClient()
    if not client.is_configured:
        return Response("Stripe not configured", status_code=503)
    try:
        event = client.construct_webhook_event(payload, signature)
    except Exception as e:
        logger.warning("[billing] webhook signature invalid: {}", e)
        return Response("Invalid signature", status_code=400)
    try:
        client.handle_webhook_event(event)
    except Exception as e:
        logger.exception("[billing] webhook handler failed")
        return Response(f"Handler error: {e}", status_code=500)
    return Response("OK", status_code=200)


@router.post("/usage/increment")
async def increment_usage(
    tenant: TenantContext = Depends(tenant_dependency),
) -> dict:
    """Manually increment usage counter (called by API gateway in production)."""
    increment_tenant_usage(tenant.tenant_id)
    allowed, used, limit = check_tenant_quota(tenant.tenant_id)
    return {"used": used, "limit": limit, "allowed": allowed}
