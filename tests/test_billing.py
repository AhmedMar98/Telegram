"""Tests for the billing module (v5.0 Stripe integration)."""

import pytest

from app.billing.stripe_client import (
    PLAN_LIMITS,
    StripeClient,
    check_tenant_quota,
    increment_tenant_usage,
)
from app.models import Subscription, Tenant


@pytest.fixture(autouse=True)
def patch_session_local(db_session):
    """Patch SessionLocal in stripe_client to use the test session."""
    import app.billing.stripe_client as sc

    original = sc.SessionLocal
    sc.SessionLocal = lambda: _Ctx(db_session)
    yield
    sc.SessionLocal = original


class _Ctx:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, *args):
        return False


# ============================================================
# Plan limits
# ============================================================


def test_plan_limits_defined():
    assert PLAN_LIMITS["free"] == 1_000
    assert PLAN_LIMITS["pro"] == 100_000
    assert PLAN_LIMITS["enterprise"] == 1_000_000


# ============================================================
# Quota checking
# ============================================================


def test_check_quota_no_subscription_returns_free_limits(db_session):
    """A tenant with no subscription row gets free-tier limits."""
    allowed, used, limit = check_tenant_quota(tenant_id=999)
    assert allowed is True
    assert used == 0
    assert limit == PLAN_LIMITS["free"]


def test_check_quota_with_subscription(db_session):
    """A tenant with a pro subscription and 50k requests used."""
    db_session.add(Tenant(name="T", slug="t1", api_key_hash="h", plan="pro"))
    db_session.commit()
    tenant_id = db_session.query(Tenant).first().id
    db_session.add(
        Subscription(
            tenant_id=tenant_id,
            plan="pro",
            status="active",
            requests_this_period=50_000,
        )
    )
    db_session.commit()

    allowed, used, limit = check_tenant_quota(tenant_id)
    assert allowed is True
    assert used == 50_000
    assert limit == PLAN_LIMITS["pro"]


def test_check_quota_exceeded(db_session):
    """When used >= limit, allowed=False."""
    db_session.add(Tenant(name="T", slug="t2", api_key_hash="h", plan="free"))
    db_session.commit()
    tenant_id = db_session.query(Tenant).first().id
    db_session.add(
        Subscription(
            tenant_id=tenant_id,
            plan="free",
            status="active",
            requests_this_period=PLAN_LIMITS["free"],
        )
    )
    db_session.commit()

    allowed, used, limit = check_tenant_quota(tenant_id)
    assert allowed is False  # exactly at limit


# ============================================================
# Usage increment
# ============================================================


def test_increment_usage_creates_subscription_row(db_session):
    """If no subscription row exists, increment creates one."""
    db_session.add(Tenant(name="T", slug="t3", api_key_hash="h", plan="free"))
    db_session.commit()
    tenant_id = db_session.query(Tenant).first().id

    increment_tenant_usage(tenant_id)
    sub = db_session.query(Subscription).filter_by(tenant_id=tenant_id).first()
    assert sub is not None
    assert sub.requests_this_period == 1
    assert sub.plan == "free"


def test_increment_usage_increments_existing(db_session):
    db_session.add(Tenant(name="T", slug="t4", api_key_hash="h", plan="pro"))
    db_session.commit()
    tenant_id = db_session.query(Tenant).first().id
    db_session.add(
        Subscription(
            tenant_id=tenant_id,
            plan="pro",
            status="active",
            requests_this_period=10,
        )
    )
    db_session.commit()

    increment_tenant_usage(tenant_id)
    increment_tenant_usage(tenant_id)
    increment_tenant_usage(tenant_id)

    sub = db_session.query(Subscription).filter_by(tenant_id=tenant_id).first()
    assert sub.requests_this_period == 13  # 10 + 3


# ============================================================
# StripeClient
# ============================================================


def test_stripe_client_not_configured_without_key():
    """Without STRIPE_API_KEY, is_configured=False."""
    client = StripeClient()
    client.api_key = ""
    assert client.is_configured is False


def test_stripe_client_is_configured_with_key():
    client = StripeClient()
    client.api_key = "sk_test_123"
    assert client.is_configured is True


def test_stripe_create_checkout_not_configured_raises():
    client = StripeClient()
    client.api_key = ""
    with pytest.raises(RuntimeError, match="Stripe not configured"):
        client._ensure_client()
