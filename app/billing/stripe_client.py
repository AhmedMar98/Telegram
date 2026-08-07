"""
Stripe billing client — wraps Stripe Checkout + Webhooks.

Lazily imports the `stripe` Python package so the platform runs even
without Stripe configured (free-tier single-tenant mode).
"""

from __future__ import annotations

from loguru import logger

from ..config import get_settings
from ..database import SessionLocal
from ..models import Subscription, Tenant

# Plan limits (requests per billing period)
PLAN_LIMITS = {
    "free": 1_000,  # 1k API requests / month
    "pro": 100_000,  # 100k / month
    "enterprise": 1_000_000,  # 1M / month
}


class StripeClient:
    """Wrapper around the Stripe API."""

    def __init__(self) -> None:
        s = get_settings()
        self.api_key = s.stripe_api_key
        self.webhook_secret = s.stripe_webhook_secret
        self.price_ids = {
            "free": s.stripe_price_free,
            "pro": s.stripe_price_pro,
            "enterprise": s.stripe_price_enterprise,
        }
        self._client = None

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not self.is_configured:
            raise RuntimeError("Stripe not configured (STRIPE_API_KEY missing)")
        try:
            import stripe

            stripe.api_key = self.api_key
            self._client = stripe
        except ImportError as e:
            raise RuntimeError("stripe package not installed: pip install stripe") from e
        return self._client

    def create_checkout_session(
        self,
        tenant_id: int,
        plan: str,
        success_url: str,
        cancel_url: str,
    ) -> dict:
        """Create a Stripe Checkout session for upgrading to `plan`."""
        stripe = self._ensure_client()
        price_id = self.price_ids.get(plan)
        if not price_id:
            raise ValueError(f"No Stripe price ID configured for plan '{plan}'")

        with SessionLocal() as db:
            tenant = db.get(Tenant, tenant_id)
            if tenant is None:
                raise ValueError(f"Tenant {tenant_id} not found")
            # Reuse existing customer if we have one
            sub = db.query(Subscription).filter(Subscription.tenant_id == tenant_id).first()
            customer_id = sub.stripe_customer_id if sub else None

        params = {
            "mode": "subscription",
            "line_items": [{"price": price_id, "quantity": 1}],
            "success_url": success_url,
            "cancel_url": cancel_url,
            "client_reference_id": str(tenant_id),
            "metadata": {"tenant_id": str(tenant_id), "plan": plan},
        }
        if customer_id:
            params["customer"] = customer_id
        session = stripe.checkout.Session.create(**params)
        return {"checkout_url": session.url, "session_id": session.id}

    def construct_webhook_event(self, payload: bytes, signature: str):
        """Verify + parse a Stripe webhook event."""
        stripe = self._ensure_client()
        return stripe.Webhook.construct_event(payload, signature, self.webhook_secret)

    def handle_webhook_event(self, event) -> None:
        """Persist subscription changes to DB."""
        etype = event["type"]
        data = event["data"]["object"]
        logger.info("[stripe] webhook: {} customer={}", etype, data.get("customer"))

        if etype in ("checkout.session.completed",):
            tenant_id = int(data.get("client_reference_id", 0))
            if not tenant_id:
                return
            with SessionLocal() as db:
                sub = db.query(Subscription).filter(Subscription.tenant_id == tenant_id).first()
                if sub is None:
                    sub = Subscription(tenant_id=tenant_id)
                    db.add(sub)
                sub.stripe_customer_id = data.get("customer")
                sub.stripe_subscription_id = data.get("subscription")
                sub.status = "active"
                db.commit()

        elif etype in ("customer.subscription.updated", "customer.subscription.created"):
            sub_id = data.get("id")
            with SessionLocal() as db:
                sub = (
                    db.query(Subscription)
                    .filter(Subscription.stripe_subscription_id == sub_id)
                    .first()
                )
                if sub is None:
                    return
                sub.status = data.get("status", "active")
                # Map price → plan
                price_id = (data.get("items", {}).get("data") or [{}])[0].get("price", {}).get("id")
                for plan, pid in self.price_ids.items():
                    if pid == price_id:
                        sub.plan = plan
                        # Update tenant.plan too
                        tenant = db.get(Tenant, sub.tenant_id)
                        if tenant:
                            tenant.plan = plan
                        break
                # Reset usage counter on new period
                if data.get("current_period_end"):
                    import datetime as _dt

                    sub.current_period_end = _dt.datetime.fromtimestamp(data["current_period_end"])
                    sub.requests_this_period = 0
                db.commit()

        elif etype == "customer.subscription.deleted":
            sub_id = data.get("id")
            with SessionLocal() as db:
                sub = (
                    db.query(Subscription)
                    .filter(Subscription.stripe_subscription_id == sub_id)
                    .first()
                )
                if sub:
                    sub.status = "canceled"
                    sub.plan = "free"
                    tenant = db.get(Tenant, sub.tenant_id)
                    if tenant:
                        tenant.plan = "free"
                    db.commit()


# ============================================================
# Usage enforcement
# ============================================================


def check_tenant_quota(tenant_id: int) -> tuple[bool, int, int]:
    """
    Returns (allowed, used, limit).
    If allowed=False, tenant exceeded their plan's request quota.
    """
    with SessionLocal() as db:
        sub = db.query(Subscription).filter(Subscription.tenant_id == tenant_id).first()
        if sub is None:
            # No subscription row → free tier
            return True, 0, PLAN_LIMITS["free"]
        plan = sub.plan or "free"
        limit = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])
        used = sub.requests_this_period or 0
        return (used < limit), used, limit


def increment_tenant_usage(tenant_id: int) -> None:
    """Increment the request counter for the current billing period."""
    with SessionLocal() as db:
        sub = db.query(Subscription).filter(Subscription.tenant_id == tenant_id).first()
        if sub is None:
            # Auto-create free-tier subscription row
            sub = Subscription(tenant_id=tenant_id, plan="free", status="active")
            db.add(sub)
        sub.requests_this_period = (sub.requests_this_period or 0) + 1
        db.commit()
