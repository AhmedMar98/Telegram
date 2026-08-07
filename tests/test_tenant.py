"""Tests for Multi-tenant support (v4.1)."""

import pytest

from app.tenant import (
    TenantContext,
    create_tenant,
    generate_api_key,
    get_current_tenant,
    hash_api_key,
    reset_current_tenant,
    resolve_tenant_by_api_key,
    set_current_tenant,
)


@pytest.fixture(autouse=True)
def reset_tenant_context():
    """Each test starts with no tenant set."""
    reset_current_tenant()
    yield
    reset_current_tenant()


def test_default_tenant_context():
    ctx = TenantContext.default()
    assert ctx.tenant_id == 1
    assert ctx.slug == "default"
    assert ctx.plan == "free"


def test_get_current_tenant_returns_default_when_unset():
    ctx = get_current_tenant()
    assert ctx.tenant_id == 1


def test_set_and_get_current_tenant():
    ctx = TenantContext(tenant_id=42, slug="acme", plan="pro")
    set_current_tenant(ctx)
    assert get_current_tenant().tenant_id == 42
    assert get_current_tenant().plan == "pro"


def test_hash_api_key_is_deterministic():
    """Same input → same hash."""
    h1 = hash_api_key("lip_acme_abc123")
    h2 = hash_api_key("lip_acme_abc123")
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_hash_api_key_different_inputs_diverge():
    assert hash_api_key("a") != hash_api_key("b")


def test_generate_api_key_format():
    key = generate_api_key("acme")
    assert key.startswith("lip_acme_")
    assert len(key) > 30  # prefix + 32 hex chars


def test_resolve_tenant_returns_none_for_invalid_key():
    assert resolve_tenant_by_api_key("") is None
    assert resolve_tenant_by_api_key("not_a_key") is None
    assert resolve_tenant_by_api_key("lip_unknown_nonexistent") is None


def test_create_tenant_and_resolve_roundtrip(db_session):
    """Create a tenant → resolve it by API key."""
    # Patch SessionLocal to use the test session
    import app.tenant as tenant_mod

    original = tenant_mod.SessionLocal
    tenant_mod.SessionLocal = lambda: _Ctx(db_session)
    try:
        tenant, plaintext_key = create_tenant(name="Test Corp", slug="test-corp", plan="pro")
        assert tenant.id > 0
        assert tenant.slug == "test-corp"
        assert plaintext_key.startswith("lip_test-corp_")

        # Resolve via the API key
        ctx = resolve_tenant_by_api_key(plaintext_key)
        assert ctx is not None
        assert ctx.tenant_id == tenant.id
        assert ctx.slug == "test-corp"
        assert ctx.plan == "pro"
    finally:
        tenant_mod.SessionLocal = original


def test_create_tenant_rejects_duplicate_slug(db_session):
    import app.tenant as tenant_mod

    original = tenant_mod.SessionLocal
    tenant_mod.SessionLocal = lambda: _Ctx(db_session)
    try:
        create_tenant(name="A", slug="dup", plan="free")
        import pytest

        with pytest.raises(ValueError, match="already exists"):
            create_tenant(name="B", slug="dup", plan="free")
    finally:
        tenant_mod.SessionLocal = original


class _Ctx:
    """Context manager wrapper around a session."""

    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, *args):
        return False
