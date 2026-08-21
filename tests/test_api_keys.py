"""Personal API keys, and above all what they cannot do.

Idea 80. A key exists to be pasted into scripts, CI files and third-party
automation — channels a session cookie never touches — so it leaks in ways
a cookie does not. These tests are therefore weighted towards the refusals
rather than the happy path: the happy path failing is an outage, but a
refusal failing is a breach.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.apikeys import KEY_SCHEME, MAX_KEYS_PER_USER
from app.database import SessionLocal
from app.errors import ERROR_CODE_HEADER, ErrorCode
from app.models import ApiKey
from tests.conftest import register_workspace


def _issue(client: TestClient, name: str = "script") -> str:
    response = client.post("/auth/api-keys", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["key"]


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


# --- the key works where it is meant to ------------------------------------


def test_a_key_can_read_and_add_links_without_a_cookie(client: TestClient):
    register_workspace(client, email="k1@example.com", workspace_name="K1")
    key = _issue(client)
    client.cookies.clear()

    assert client.get("/links", headers=_bearer(key)).status_code == 200
    added = client.post("/links", json={"text": "https://example.com/a.pdf"}, headers=_bearer(key))
    assert added.status_code == 201
    assert client.get("/links", headers=_bearer(key)).json()["total"] == 1


def test_a_key_identifies_its_own_workspace(client: TestClient):
    register_workspace(client, email="k2@example.com", workspace_name="K2")
    key = _issue(client)
    client.cookies.clear()

    assert client.get("/auth/me", headers=_bearer(key)).json()["workspace_name"] == "K2"


def test_a_key_cannot_read_another_workspace(client: TestClient):
    register_workspace(client, email="k3a@example.com", workspace_name="K3A")
    client.post("/links", json={"text": "https://example.com/theirs.pdf"})
    client.post("/auth/logout")

    register_workspace(client, email="k3b@example.com", workspace_name="K3B")
    key = _issue(client)
    client.cookies.clear()

    assert client.get("/links", headers=_bearer(key)).json()["total"] == 0


# --- the refusals: what a leaked key must never reach ----------------------


def test_a_key_cannot_delete_the_workspace(client: TestClient):
    """The worst case this design exists for. A key committed to a public
    repository must not be enough to erase the whole collection."""
    register_workspace(client, email="k4@example.com", workspace_name="K4")
    key = _issue(client)
    client.cookies.clear()

    response = client.post(
        "/auth/me/delete",
        json={"current_password": "j8Kd0-slwQ2x", "confirm": "DELETE"},
        headers=_bearer(key),
    )

    assert response.status_code == 403
    # And the workspace really is still there.
    assert client.get("/auth/me", headers=_bearer(key)).status_code == 200


def test_a_key_cannot_change_the_password(client: TestClient):
    register_workspace(client, email="k5@example.com", workspace_name="K5")
    key = _issue(client)
    client.cookies.clear()

    response = client.post(
        "/auth/change-password",
        json={"current_password": "j8Kd0-slwQ2x", "new_password": "an0ther-Long-One"},
        headers=_bearer(key),
    )

    assert response.status_code == 403


def test_a_key_cannot_mint_another_key(client: TestClient):
    """Without this, revocation is theatre: whoever holds a leaked key
    issues a successor, and the owner revokes a credential that is no
    longer the one being used."""
    register_workspace(client, email="k6@example.com", workspace_name="K6")
    key = _issue(client)
    client.cookies.clear()

    assert client.post("/auth/api-keys", json={"name": "second"}, headers=_bearer(key)).status_code == 403


def test_a_key_cannot_even_list_keys(client: TestClient):
    register_workspace(client, email="k7@example.com", workspace_name="K7")
    key = _issue(client)
    client.cookies.clear()

    assert client.get("/auth/api-keys", headers=_bearer(key)).status_code == 403


def test_a_key_cannot_sign_every_device_out(client: TestClient):
    """Denial of service against the account owner, not data theft — but a
    credential meant for a sync script has no business doing it."""
    register_workspace(client, email="k8@example.com", workspace_name="K8")
    key = _issue(client)
    client.cookies.clear()

    assert client.post("/auth/logout-all", headers=_bearer(key)).status_code == 403


def test_a_key_cannot_enumerate_the_owners_devices(client: TestClient):
    """The session list carries IP addresses and user agents. That is
    surveillance material about the owner, and no script needs it."""
    register_workspace(client, email="k9@example.com", workspace_name="K9")
    key = _issue(client)
    client.cookies.clear()

    assert client.get("/auth/sessions", headers=_bearer(key)).status_code == 403
    assert client.get("/auth/security-activity", headers=_bearer(key)).status_code == 403


def test_refusing_a_valid_key_is_403_not_401(client: TestClient):
    """401 would send someone to re-check a key that is perfectly fine.
    The credential is valid; this endpoint just does not accept its kind."""
    register_workspace(client, email="k10@example.com", workspace_name="K10")
    key = _issue(client)
    client.cookies.clear()

    response = client.get("/auth/sessions", headers=_bearer(key))

    assert response.status_code == 403
    assert "session" in response.json()["detail"]


def test_no_credential_at_all_is_still_401(client: TestClient):
    assert client.get("/auth/sessions").status_code == 401
    assert client.get("/links").status_code == 401


# --- storage and revocation ------------------------------------------------


def test_the_raw_key_is_never_stored(client: TestClient):
    """A database dump must not hand anyone a working credential."""
    register_workspace(client, email="k11@example.com", workspace_name="K11")
    key = _issue(client)

    with SessionLocal() as db:
        rows = db.query(ApiKey).all()
        assert len(rows) == 1
        stored = rows[0]
        assert key not in stored.token_hash
        assert stored.token_hash != key
        # The prefix is deliberately in the clear, and deliberately short.
        assert key.startswith(stored.prefix)
        assert len(stored.prefix) < len(key) / 2


def test_the_key_is_returned_exactly_once(client: TestClient):
    register_workspace(client, email="k12@example.com", workspace_name="K12")
    key = _issue(client)

    listed = client.get("/auth/api-keys").json()

    assert len(listed) == 1
    assert key not in str(listed)
    assert listed[0]["prefix"] == key[: len(listed[0]["prefix"])]


def test_a_revoked_key_stops_working_immediately(client: TestClient):
    register_workspace(client, email="k13@example.com", workspace_name="K13")
    key = _issue(client)
    key_id = client.get("/auth/api-keys").json()[0]["id"]

    assert client.delete(f"/auth/api-keys/{key_id}").status_code == 204

    client.cookies.clear()
    assert client.get("/links", headers=_bearer(key)).status_code == 401


def test_revoking_another_users_key_is_404(client: TestClient):
    register_workspace(client, email="k14a@example.com", workspace_name="K14A")
    _issue(client)
    victim_id = client.get("/auth/api-keys").json()[0]["id"]
    client.post("/auth/logout")

    register_workspace(client, email="k14b@example.com", workspace_name="K14B")
    assert client.delete(f"/auth/api-keys/{victim_id}").status_code == 404

    # And it still works for its real owner.
    client.post("/auth/logout")
    client.post("/auth/login", json={"email": "k14a@example.com", "password": "j8Kd0-slwQ2x"})
    assert len(client.get("/auth/api-keys").json()) == 1


def test_use_is_recorded_so_a_key_can_be_accounted_for(client: TestClient):
    """A key nobody can account for is a key nobody dares revoke."""
    register_workspace(client, email="k15@example.com", workspace_name="K15")
    key = _issue(client)

    assert client.get("/auth/api-keys").json()[0]["last_used_at"] is None

    # Without the cookie, or the session would answer instead and the key
    # would never be charged — which the next test pins deliberately.
    client.cookies.clear()
    client.get("/links", headers=_bearer(key))
    client.get("/links", headers=_bearer(key))

    client.post("/auth/login", json={"email": "k15@example.com", "password": "j8Kd0-slwQ2x"})
    listed = client.get("/auth/api-keys").json()[0]
    assert listed["use_count"] == 2
    assert listed["last_used_at"] is not None


def test_a_browser_session_does_not_burn_a_key_use(client: TestClient):
    """The cookie is checked first, so a request carrying both is charged
    to the session — otherwise the use counter would measure nothing."""
    register_workspace(client, email="k16@example.com", workspace_name="K16")
    key = _issue(client)

    client.get("/links", headers=_bearer(key))

    assert client.get("/auth/api-keys").json()[0]["use_count"] == 0


def test_the_number_of_active_keys_is_capped(client: TestClient):
    register_workspace(client, email="k17@example.com", workspace_name="K17")
    for n in range(MAX_KEYS_PER_USER):
        _issue(client, name=f"key {n}")

    response = client.post("/auth/api-keys", json={"name": "one too many"})

    assert response.status_code == 422
    assert response.headers[ERROR_CODE_HEADER] == ErrorCode.API_KEY_LIMIT

    # Revoking one frees a slot, rather than the cap being permanent.
    client.delete(f"/auth/api-keys/{client.get('/auth/api-keys').json()[0]['id']}")
    assert client.post("/auth/api-keys", json={"name": "now fits"}).status_code == 201


# --- malformed and hostile credentials -------------------------------------


def test_a_garbage_bearer_token_is_rejected_not_crashed(client: TestClient):
    for value in ("", "Bearer", "Bearer ", "Basic abc", f"Bearer {KEY_SCHEME}", "Bearer lipk_wrong"):
        assert client.get("/links", headers={"Authorization": value}).status_code == 401, value


def test_a_key_shaped_value_from_another_scheme_is_not_a_key(client: TestClient):
    register_workspace(client, email="k18@example.com", workspace_name="K18")
    real = _issue(client)
    client.cookies.clear()

    # Same secret, wrong scheme prefix — must not authenticate.
    assert client.get("/links", headers=_bearer(real.replace(KEY_SCHEME, "xxxx_"))).status_code == 401


def test_key_creation_and_revocation_are_audited(client: TestClient):
    """ "Who issued this credential" must have an answer later."""
    register_workspace(client, email="k19@example.com", workspace_name="K19")
    _issue(client)
    key_id = client.get("/auth/api-keys").json()[0]["id"]
    client.delete(f"/auth/api-keys/{key_id}")

    actions = [row["action"] for row in client.get("/auth/me/export").json()["audit_log"]]

    assert "apikey.create" in actions
    assert "apikey.revoke" in actions


def test_deleting_a_workspace_takes_its_keys_with_it(client: TestClient):
    """Caught by the account-data guard, not by inspection: a key that
    outlives its workspace is a live credential pointing at a deleted
    user, and nothing would ever revoke it."""
    register_workspace(client, email="k20@example.com", workspace_name="K20")
    key = _issue(client)

    response = client.post("/auth/me/delete", json={"current_password": "j8Kd0-slwQ2x", "confirm": "DELETE"})
    assert response.status_code == 200
    assert response.json()["deleted"]["api_keys"] == 1

    with SessionLocal() as db:
        assert db.query(ApiKey).count() == 0

    client.cookies.clear()
    assert client.get("/links", headers=_bearer(key)).status_code == 401


# --- requester IP on exports (idea 81) -------------------------------------


def test_every_export_path_records_where_it_came_from(client: TestClient):
    """ "It was exported" without "from where" is the least useful half of
    the record — the owner reviewing it is asking whether it was them."""
    register_workspace(client, email="ip1@example.com", workspace_name="IP1")
    client.post("/links", json={"text": "https://example.com/a.pdf"})

    origin = {"X-Forwarded-For": "203.0.113.9, 10.0.0.1"}
    for path in ("/links/export.csv", "/links/export.json", "/links/export.md", "/auth/me/export"):
        assert client.get(path, headers=origin).status_code == 200, path

    rows = [r for r in client.get("/auth/me/export").json()["audit_log"] if "export" in r["action"]]

    assert len(rows) >= 4
    # Only the first X-Forwarded-For entry: the rest are upstream proxies.
    assert {r["ip_address"] for r in rows} == {"203.0.113.9"}


def test_an_export_through_a_key_is_attributed_to_the_key_holder(client: TestClient):
    """Exports stay available to a key — programmatic migration is a real
    use — which is exactly why the address matters on that path too."""
    register_workspace(client, email="ip2@example.com", workspace_name="IP2")
    key = _issue(client)
    client.cookies.clear()

    response = client.get("/links/export.json", headers={**_bearer(key), "X-Forwarded-For": "198.51.100.7"})
    assert response.status_code == 200

    client.post("/auth/login", json={"email": "ip2@example.com", "password": "j8Kd0-slwQ2x"})
    rows = [r for r in client.get("/auth/me/export").json()["audit_log"] if r["action"] == "link.export"]

    assert rows[0]["ip_address"] == "198.51.100.7"


def test_a_refused_attempt_still_counts_as_a_use(client: TestClient):
    """A key presented repeatedly to endpoints that reject it is the shape
    of a leaked credential being probed. The counter is what would show
    that, so refusals must not be invisible."""
    register_workspace(client, email="k21@example.com", workspace_name="K21")
    key = _issue(client)
    client.cookies.clear()

    assert client.get("/auth/sessions", headers=_bearer(key)).status_code == 403
    assert client.post("/auth/logout-all", headers=_bearer(key)).status_code == 403

    client.post("/auth/login", json={"email": "k21@example.com", "password": "j8Kd0-slwQ2x"})
    assert client.get("/auth/api-keys").json()[0]["use_count"] == 2
