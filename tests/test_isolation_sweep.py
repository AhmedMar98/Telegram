"""Systematic cross-workspace probe over every tenant-scoped endpoint.

The existing isolation tests each cover one endpoint. This one is
deliberately different: it enumerates the live OpenAPI schema and asserts
that **every** path taking a resource id refuses another workspace's id,
so an endpoint added later without an isolation test is caught here
instead of shipping unnoticed.

The distinction it enforces: a foreign id must be *indistinguishable from
one that does not exist* (404), never rejected with 403. A 403 confirms
the id is real and belongs to somebody, which is exactly the signal an
attacker enumerating ids is looking for.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import TelegramAccount, User
from tests.conftest import register_workspace

# Paths that legitimately take no resource id, or are not tenant-scoped at
# all. Listed explicitly rather than pattern-matched so that adding a new
# endpoint forces a deliberate decision about which group it belongs to.
NOT_TENANT_SCOPED = {
    "/",
    "/healthz",
    "/readyz",
    "/login",
    "/register",
    "/dashboard",
    "/auth/login",
    "/auth/logout",
    "/auth/logout-all",
    "/auth/register",
    "/auth/api-keys",
    "/auth/me",
    "/auth/me/delete",
    "/auth/me/export",
    "/auth/change-password",
    "/auth/security-activity",
    "/auth/sessions",
    "/auth/workspace",
    "/bot/link-code",
    "/channels",
    "/channels/accounts",
    "/links",
    "/notifications",
    "/notifications/read-all",
    "/notifications/unread-count",
    "/notifications/preferences",
    "/notifications/webhook",
    "/notifications/webhook/test",
    # The parameter here is a global vocabulary key (an alert type), not a
    # tenant-owned identifier, so there is no "another workspace's id" to
    # probe: every workspace addresses the same key set, and the scoping
    # comes from the session. The isolation that *does* apply — one
    # workspace's switch not moving another's — is asserted directly in
    # tests/test_notifications.py rather than by this sweep.
    "/notifications/preferences/{alert_type}",
    "/links/stats",
    "/links/bulk/delete",
    "/links/bulk/recategorize",
    "/links/export.csv",
    "/links/export.json",
    "/links/export.md",
    "/links/random",
    "/links/saved",
    "/telegram/webhook/{secret}",
}


def _parameterised_paths() -> list[tuple[str, str]]:
    """Every (path, method) that interpolates a resource id."""
    out = []
    for path, methods in app.openapi()["paths"].items():
        if path in NOT_TENANT_SCOPED or "{" not in path:
            continue
        for method in methods:
            out.append((path, method.upper()))
    return sorted(out)


def test_every_parameterised_path_is_covered_by_this_sweep():
    """Guards the guard: a new id-taking endpoint must be probed, not skipped."""
    discovered = {path for path, _ in _parameterised_paths()}
    expected = {
        "/auth/api-keys/{key_id}",
        "/notifications/{notification_id}/read",
        "/auth/sessions/{session_id}",
        "/channels/{channel_id}",
        "/channels/import/{batch_id}/undo",
        "/links/{link_id}",
        "/links/{link_id}/favorite",
        "/links/{link_id}/archive",
        "/links/{link_id}/notes",
        "/links/{link_id}/pin",
        "/links/{link_id}/open",
        "/links/saved/{saved_id}",
        "/channels/accounts/{account_id}/reactivate",
        "/leads/{lead_id}",
        "/leads/keywords/{rule_id}",
        "/leads/beneficiaries/{beneficiary_id}",
        "/team/{user_id}",
    }
    assert discovered == expected, (
        f"Tenant-scoped paths changed. New ones must be probed below or "
        f"consciously added to NOT_TENANT_SCOPED. Difference: {discovered ^ expected}"
    )


@pytest.fixture
def victim_ids(client: TestClient) -> dict[str, int]:
    """Create a workspace with one of everything, then log out of it."""
    register_workspace(client, email="victim@example.com", workspace_name="Victim Co")
    channel = client.post("/channels", json={"tg_channel_id": "1", "username": "victim"}).json()
    client.post("/links", json={"text": "https://victim.example/secret.pdf"})
    link = client.get("/links").json()["items"][0]
    session = client.get("/auth/sessions").json()[0]
    saved = client.post("/links/saved", json={"name": "victim search", "filters": {"q": "secret"}}).json()
    client.post("/auth/api-keys", json={"name": "victim key"})
    api_key = client.get("/auth/api-keys").json()[0]

    # A collecting account exists only once the collector has registered
    # one, so the fixture creates it directly — the sweep needs a real id
    # of every kind the API exposes, not only the ones the API creates.
    db = SessionLocal()
    try:
        workspace_id = db.query(User).filter(User.email == "victim@example.com").one().workspace_id
        account = TelegramAccount(workspace_id=workspace_id, label="victim account", session_string="x")
        db.add(account)
        db.commit()
        account_id = account.id
    finally:
        db.close()

    # A raised alert, so the sweep has a real foreign notification id to
    # probe rather than one that never existed.
    import asyncio as _asyncio

    from app.alerts import COLLECTOR_FAILED as _COLLECTOR_FAILED
    from app.notify import raise_alert as _raise_alert

    db = SessionLocal()
    try:
        _asyncio.run(_raise_alert(db, workspace_id, _COLLECTOR_FAILED.key, title="victim alert", body="secret"))
    finally:
        db.close()
    notification_id = client.get("/notifications").json()["items"][0]["id"]

    # Lead-detection rows. Created directly rather than through the API,
    # because the whole feature is inert unless LEADS_ENABLED is set — and
    # the sweep must probe these paths whether the flag is on or off. An
    # endpoint that leaks another workspace's data while its feature is
    # "disabled" leaks it just the same.
    db = SessionLocal()
    try:
        from app.models import Beneficiary, KeywordRule, Lead

        rule = KeywordRule(workspace_id=workspace_id, phrase="سرّي", weight=3)
        person = Beneficiary(workspace_id=workspace_id, tg_user_id="victim-user", username="victim_person")
        db.add_all([rule, person])
        db.flush()
        lead = Lead(
            workspace_id=workspace_id,
            beneficiary_id=person.id,
            channel_id=channel["id"],
            message_id=1,
            text="طلب الضحية السرّي",
            matched="سرّي",
            score=3,
        )
        db.add(lead)
        db.commit()
        rule_id, beneficiary_id, lead_id = rule.id, person.id, lead.id
        victim_user_id = db.query(User).filter(User.email == "victim@example.com").one().id
    finally:
        db.close()

    # A committed bulk source import, so the sweep has a real batch id to
    # probe. Undo is the most destructive id-taking endpoint the platform
    # has — it deletes sources — so "another workspace's batch id" is
    # exactly the probe that must come back 404.
    batch_id = client.post("/channels/import", json={"text": "@victim_imported_source", "commit": True}).json()[
        "batch_id"
    ]

    client.post("/auth/logout")
    return {
        "batch_id": batch_id,
        "notification_id": notification_id,
        "channel_id": channel["id"],
        "link_id": link["id"],
        "session_id": session["id"],
        "saved_id": saved["id"],
        "key_id": api_key["id"],
        "account_id": account_id,
        "lead_id": lead_id,
        "rule_id": rule_id,
        "beneficiary_id": beneficiary_id,
        "user_id": victim_user_id,
    }


@pytest.fixture
def attacker(client: TestClient, victim_ids):
    register_workspace(client, email="attacker@example.com", workspace_name="Attacker Co")
    return client


# A body that each PATCH endpoint will actually accept. Sending the wrong
# shape makes the endpoint answer 422 from validation before it ever looks
# the resource up, so the probe would pass without testing ownership at all.
_VALID_PATCH_BODIES = {
    "/links/{link_id}": {"category": "games"},
    "/links/{link_id}/notes": {"notes": "probe"},
    "/channels/{channel_id}": {"account_id": None},
    "/leads/{lead_id}": {"status": "contacted"},
    "/leads/keywords/{rule_id}": {"weight": 2},
    "/team/{user_id}": {"role": "agent"},
}


def _probe(attacker: TestClient, path: str, method: str, ids: dict[str, int]):
    url = path.format(**ids)
    body = _VALID_PATCH_BODIES.get(path) if method == "PATCH" else ({} if method == "POST" else None)
    return attacker.request(method, url, json=body)


def test_every_patch_path_has_a_valid_probe_body():
    """Guards the guard: a PATCH path with no body here is probed with
    None, gets a 422 from validation, and never reaches the ownership
    check it is supposed to be testing."""
    patch_paths = {path for path, method in _parameterised_paths() if method == "PATCH"}
    assert patch_paths <= set(_VALID_PATCH_BODIES), (
        f"no probe body for: {sorted(patch_paths - set(_VALID_PATCH_BODIES))}"
    )


@pytest.mark.parametrize(("path", "method"), _parameterised_paths())
def test_foreign_resource_id_is_not_found(attacker, victim_ids, path: str, method: str):
    """A resource belonging to another workspace must read as nonexistent."""
    response = _probe(attacker, path, method, victim_ids)

    assert response.status_code == 404, (
        f"{method} {path} returned {response.status_code} for another workspace's id. "
        "Expected 404 — a 403 would confirm the id exists."
    )


@pytest.mark.parametrize(("path", "method"), _parameterised_paths())
def test_foreign_resource_survives_the_probe(attacker, victim_ids, path: str, method: str):
    """The probe must not mutate the victim's data as a side effect."""
    _probe(attacker, path, method, victim_ids)

    attacker.post("/auth/logout")
    attacker.post("/auth/login", json={"email": "victim@example.com", "password": "j8Kd0-slwQ2x"})

    assert attacker.get("/links").json()["total"] == 1, f"{method} {path} destroyed the victim's link"
    assert len(attacker.get("/channels").json()) == 3  # manual bucket, the added one, the imported one
    assert len(attacker.get("/links/saved").json()) == 1, f"{method} {path} destroyed the victim's saved search"
    assert len(attacker.get("/auth/api-keys").json()) == 1, f"{method} {path} revoked the victim's API key"

    # The lead rows too. A "forget this person" that another workspace can
    # reach is the worst possible version of this endpoint: it destroys
    # data silently and the owner has no way to know who did it.
    db = SessionLocal()
    try:
        from app.models import Beneficiary as _B
        from app.models import Lead as _L

        ws = db.query(User).filter(User.email == "victim@example.com").one().workspace_id
        assert db.query(_L).filter(_L.workspace_id == ws).count() == 1, (
            f"{method} {path} destroyed the victim's lead"
        )
        assert db.query(_B).filter(_B.workspace_id == ws).count() == 1, (
            f"{method} {path} erased the victim's beneficiary"
        )
    finally:
        db.close()
    assert attacker.get("/notifications").json()["unread"] == 1, (
        f"{method} {path} marked the victim's notification read"
    )


def test_unauthenticated_access_is_rejected_everywhere(client: TestClient, victim_ids):
    """No tenant endpoint may be reachable without a session at all."""
    for path, method in _parameterised_paths():
        response = _probe(client, path, method, victim_ids)
        assert response.status_code == 401, f"{method} {path} allowed an unauthenticated caller"
