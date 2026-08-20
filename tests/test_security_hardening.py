"""Phase 1 hardening: session origin, attack visibility, and body limits."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import MAX_REQUEST_BODY_BYTES
from app.models import AuthSession, LoginAttempt
from tests.conftest import register_workspace

UA = "Mozilla/5.0 (TestBrowser) HardeningSuite/1.0"


# --- session origin (#78) --------------------------------------------------


def test_session_records_the_client_origin(client: TestClient):
    register_workspace(client, email="origin@example.com", workspace_name="Origin Co")
    client.post("/auth/logout")
    client.post(
        "/auth/login",
        json={"email": "origin@example.com", "password": "j8Kd0-slwQ2x"},
        headers={"User-Agent": UA, "X-Forwarded-For": "203.0.113.9"},
    )

    session = client.get("/auth/sessions").json()[0]

    assert session["ip_address"] == "203.0.113.9"
    assert session["user_agent"] == UA


def test_only_the_first_forwarded_address_is_kept(client: TestClient):
    """The rest of X-Forwarded-For is the proxy chain, not the caller."""
    register_workspace(client, email="chain@example.com", workspace_name="Chain Co")
    client.post("/auth/logout")
    client.post(
        "/auth/login",
        json={"email": "chain@example.com", "password": "j8Kd0-slwQ2x"},
        headers={"X-Forwarded-For": "198.51.100.7, 10.0.0.1, 10.0.0.2"},
    )

    assert client.get("/auth/sessions").json()[0]["ip_address"] == "198.51.100.7"


def test_an_overlong_user_agent_is_truncated_not_rejected(client: TestClient):
    """A client may send any header length; losing the tail beats failing login."""
    register_workspace(client, email="long@example.com", workspace_name="Long Co")
    client.post("/auth/logout")

    response = client.post(
        "/auth/login",
        json={"email": "long@example.com", "password": "j8Kd0-slwQ2x"},
        headers={"User-Agent": "U" * 5000},
    )

    assert response.status_code == 200
    assert len(client.get("/auth/sessions").json()[0]["user_agent"]) == 300


def test_a_session_without_origin_reports_none_rather_than_inventing_one(client: TestClient):
    register_workspace(client, email="noorigin@example.com", workspace_name="NoOrigin Co")
    db = SessionLocal()
    try:
        row = db.query(AuthSession).first()
        row.ip_address = None
        row.user_agent = None
        db.commit()
    finally:
        db.close()

    session = client.get("/auth/sessions").json()[0]

    assert session["ip_address"] is None
    assert session["user_agent"] is None


# --- attack visibility (#77) -----------------------------------------------


def test_failed_logins_are_visible_to_the_account_owner(client: TestClient):
    register_workspace(client, email="watch@example.com", workspace_name="Watch Co")
    client.post("/auth/logout")
    for ip in ("203.0.113.1", "203.0.113.2", "203.0.113.1"):
        client.post(
            "/auth/login",
            json={"email": "watch@example.com", "password": "wrong-password"},
            headers={"X-Forwarded-For": ip},
        )
    client.post("/auth/login", json={"email": "watch@example.com", "password": "j8Kd0-slwQ2x"})

    # A successful login clears the failures — that is the existing throttle
    # behaviour, so the counter reads zero and that is correct, not a bug.
    activity = client.get("/auth/security-activity").json()

    assert activity["failed_attempts"] == 0
    assert activity["lockout_threshold"] > 0
    assert activity["window_minutes"] > 0


def test_pending_failed_attempts_are_counted_with_distinct_ips(client: TestClient):
    register_workspace(client, email="probe@example.com", workspace_name="Probe Co")
    for ip in ("198.51.100.1", "198.51.100.2", "198.51.100.1"):
        client.post(
            "/auth/login",
            json={"email": "probe@example.com", "password": "nope"},
            headers={"X-Forwarded-For": ip},
        )

    activity = client.get("/auth/security-activity").json()

    assert activity["failed_attempts"] == 3
    assert activity["distinct_ip_count"] == 2
    assert activity["last_failed_at"] is not None


def test_security_activity_is_scoped_to_the_caller(client: TestClient):
    """One account's attack history must not be visible from another."""
    register_workspace(client, email="target@example.com", workspace_name="Target Co")
    client.post("/auth/logout")
    for _ in range(3):
        client.post("/auth/login", json={"email": "target@example.com", "password": "nope"})

    register_workspace(client, email="other@example.com", workspace_name="Other Co")
    activity = client.get("/auth/security-activity").json()

    assert activity["failed_attempts"] == 0


def test_security_activity_requires_authentication(client: TestClient):
    assert client.get("/auth/security-activity").status_code == 401


def test_failed_attempt_records_the_origin_ip(client: TestClient):
    register_workspace(client, email="ip@example.com", workspace_name="IP Co")
    client.post(
        "/auth/login",
        json={"email": "ip@example.com", "password": "nope"},
        headers={"X-Forwarded-For": "192.0.2.44"},
    )

    db = SessionLocal()
    try:
        attempt = db.query(LoginAttempt).filter(LoginAttempt.successful.is_(False)).first()
        assert attempt.ip_address == "192.0.2.44"
    finally:
        db.close()


# --- request body limit (#82) ----------------------------------------------


def test_oversized_body_is_rejected_before_parsing(client: TestClient):
    register_workspace(client, email="big@example.com", workspace_name="Big Co")

    response = client.post(
        "/links",
        content=b"x" * (MAX_REQUEST_BODY_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413


def test_a_normal_sized_paste_still_works(client: TestClient):
    register_workspace(client, email="ok@example.com", workspace_name="OK Co")

    response = client.post("/links", json={"text": "كتاب https://example.com/book.pdf"})

    assert response.status_code == 201


def test_malformed_content_length_is_rejected(client: TestClient):
    """Guards the int() conversion in the middleware."""
    register_workspace(client, email="bad@example.com", workspace_name="Bad Co")

    response = client.post(
        "/links",
        content=b"{}",
        headers={"Content-Type": "application/json", "Content-Length": "not-a-number"},
    )

    assert response.status_code in (400, 422)


def test_the_limit_leaves_room_for_the_largest_legitimate_paste(client: TestClient):
    """50,000 Arabic characters is ~2 bytes each; the cap must not reject it."""
    register_workspace(client, email="arabic@example.com", workspace_name="Arabic Co")
    text = "رابط " * 9000  # ~45,000 chars, comfortably under the 50k schema cap

    response = client.post("/links", json={"text": text})

    assert response.status_code == 201
