"""The optional second factor, and the recovery path that makes it safe.

Idea 79. The riskiest thing about adding a second factor to *this*
platform is not that it might fail open — it is that it might fail
*closed* permanently. One user per workspace, no administrator (phase 6b
is deferred), so a lost authenticator with no recovery is the whole
collection gone. Most of what follows is about that.
"""

from __future__ import annotations

import pyotp
import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.errors import ERROR_CODE_HEADER, ErrorCode
from app.models import User
from app.totp import TIME_STEP, current_step, decrypt_secret
from tests.conftest import register_workspace

PASSWORD = "j8Kd0-slwQ2x"


def _next_code(secret: str) -> str:
    """A code one step ahead.

    Enrolment spends the code that proved the authenticator, so the very
    next login inside the same 30-second window cannot reuse it — correct,
    and the reason these tests reach forward a step rather than calling
    ``now()`` again.
    """
    return pyotp.TOTP(secret).at((current_step() + 1) * TIME_STEP)


def _stored_secret(email: str) -> str:
    with SessionLocal() as db:
        user = db.query(User).filter(User.email == email).one()
        secret = decrypt_secret(user.totp_secret or "")
    assert secret is not None
    return secret


def _enable(client: TestClient, email: str) -> tuple[str, list[str]]:
    """Full enrolment, returning the secret and the recovery codes."""
    assert client.post("/auth/totp/setup").status_code == 200
    secret = _stored_secret(email)
    response = client.post("/auth/totp/enable", json={"code": pyotp.TOTP(secret).now()})
    assert response.status_code == 200, response.text
    return secret, response.json()["recovery_codes"]


# --- nothing changes for accounts that never opt in ------------------------


def test_login_is_untouched_when_no_second_factor_is_configured(client: TestClient):
    """The whole feature must be invisible to everyone who did not ask for
    it — a migration that changed how existing accounts log in would be a
    far worse outcome than not shipping it."""
    register_workspace(client, email="tf0@example.com", workspace_name="TF0")
    client.post("/auth/logout")

    response = client.post("/auth/login", json={"email": "tf0@example.com", "password": PASSWORD})

    assert response.status_code == 200
    assert client.get("/auth/me").status_code == 200


def test_status_reports_it_off_by_default(client: TestClient):
    register_workspace(client, email="tf1@example.com", workspace_name="TF1")

    assert client.get("/auth/totp").json() == {"enabled": False, "recovery_codes_remaining": 0}


# --- enrolment cannot lock you out ----------------------------------------


def test_setup_alone_does_not_enable_anything(client: TestClient):
    """A mistyped or abandoned setup must leave the account exactly as it
    was. Storing the secret and flipping the switch are separate steps for
    precisely this reason."""
    register_workspace(client, email="tf2@example.com", workspace_name="TF2")

    client.post("/auth/totp/setup")

    assert client.get("/auth/totp").json()["enabled"] is False
    client.post("/auth/logout")
    assert client.post("/auth/login", json={"email": "tf2@example.com", "password": PASSWORD}).status_code == 200


def test_enabling_requires_a_working_code(client: TestClient):
    register_workspace(client, email="tf3@example.com", workspace_name="TF3")
    client.post("/auth/totp/setup")

    response = client.post("/auth/totp/enable", json={"code": "000000"})

    assert response.status_code == 422
    assert response.headers[ERROR_CODE_HEADER] == ErrorCode.TOTP_INVALID
    assert client.get("/auth/totp").json()["enabled"] is False


def test_enabling_always_issues_recovery_codes(client: TestClient):
    """Not a later optional step. With one user per workspace and no
    admin, a second factor without recovery is a way to lose everything to
    a dropped phone."""
    register_workspace(client, email="tf4@example.com", workspace_name="TF4")

    _, codes = _enable(client, "tf4@example.com")

    assert len(codes) == 10
    assert len(set(codes)) == 10
    assert client.get("/auth/totp").json() == {"enabled": True, "recovery_codes_remaining": 10}


def test_setup_is_refused_while_already_enabled(client: TestClient):
    register_workspace(client, email="tf5@example.com", workspace_name="TF5")
    _enable(client, "tf5@example.com")

    response = client.post("/auth/totp/setup")

    assert response.status_code == 422
    # And the working secret was not replaced out from under the app.
    assert client.get("/auth/totp").json()["enabled"] is True


# --- logging in with the second factor -------------------------------------


def test_the_password_alone_stops_working(client: TestClient):
    register_workspace(client, email="tf6@example.com", workspace_name="TF6")
    _enable(client, "tf6@example.com")
    client.post("/auth/logout")

    response = client.post("/auth/login", json={"email": "tf6@example.com", "password": PASSWORD})

    assert response.status_code == 422
    assert response.headers[ERROR_CODE_HEADER] == ErrorCode.TOTP_REQUIRED
    assert client.get("/auth/me").status_code == 401


def test_a_valid_code_completes_the_login(client: TestClient):
    register_workspace(client, email="tf7@example.com", workspace_name="TF7")
    secret, _ = _enable(client, "tf7@example.com")
    client.post("/auth/logout")

    response = client.post(
        "/auth/login",
        json={"email": "tf7@example.com", "password": PASSWORD, "totp_code": _next_code(secret)},
    )

    assert response.status_code == 200
    assert client.get("/auth/me").json()["email"] == "tf7@example.com"


def test_a_wrong_password_with_a_right_code_is_still_refused(client: TestClient):
    """A second factor is a second factor, not a replacement first one."""
    register_workspace(client, email="tf8@example.com", workspace_name="TF8")
    secret, _ = _enable(client, "tf8@example.com")
    client.post("/auth/logout")

    response = client.post(
        "/auth/login",
        json={"email": "tf8@example.com", "password": "wrong-password", "totp_code": _next_code(secret)},
    )

    assert response.status_code == 401


def test_a_code_cannot_be_replayed_inside_its_own_window(client: TestClient):
    """A TOTP code stays valid for its whole 30-second step, so without
    replay defence anyone who observes one has the rest of that window to
    reuse it."""
    register_workspace(client, email="tf9@example.com", workspace_name="TF9")
    secret, _ = _enable(client, "tf9@example.com")
    client.post("/auth/logout")

    code = _next_code(secret)
    first = client.post("/auth/login", json={"email": "tf9@example.com", "password": PASSWORD, "totp_code": code})
    client.post("/auth/logout")
    second = client.post("/auth/login", json={"email": "tf9@example.com", "password": PASSWORD, "totp_code": code})

    assert first.status_code == 200
    assert second.status_code == 422
    assert second.headers[ERROR_CODE_HEADER] == ErrorCode.TOTP_REQUIRED


def test_a_code_from_the_previous_step_still_works_for_clock_drift(client: TestClient):
    """Phone clocks drift. A window so tight that a correct authenticator
    is rejected produces exactly the support burden this design avoids."""
    register_workspace(client, email="tf10@example.com", workspace_name="TF10")
    secret, _ = _enable(client, "tf10@example.com")
    client.post("/auth/logout")

    with SessionLocal() as db:
        user = db.query(User).filter(User.email == "tf10@example.com").one()
        user.totp_last_step = None
        db.commit()

    previous = pyotp.TOTP(secret).at((current_step() - 1) * TIME_STEP)
    response = client.post(
        "/auth/login", json={"email": "tf10@example.com", "password": PASSWORD, "totp_code": previous}
    )

    assert response.status_code == 200


@pytest.mark.parametrize("code", ["", "   ", "abcdef", "12345", "1234567", "000000", "0" * 40])
def test_no_junk_code_is_ever_accepted(client: TestClient, code: str):
    register_workspace(client, email="tf11@example.com", workspace_name="TF11")
    _enable(client, "tf11@example.com")
    client.post("/auth/logout")

    response = client.post(
        "/auth/login", json={"email": "tf11@example.com", "password": PASSWORD, "totp_code": code}
    )

    assert response.status_code in (401, 422)
    assert client.get("/auth/me").status_code == 401


def test_code_guessing_is_covered_by_the_existing_login_throttle(client: TestClient):
    """Six digits without a throttle is not a second factor."""
    register_workspace(client, email="tf12@example.com", workspace_name="TF12")
    _enable(client, "tf12@example.com")
    client.post("/auth/logout")

    for _ in range(10):
        client.post("/auth/login", json={"email": "tf12@example.com", "password": PASSWORD, "totp_code": "000000"})

    locked = client.post("/auth/login", json={"email": "tf12@example.com", "password": PASSWORD})

    assert locked.status_code == 429
    assert locked.headers["Retry-After"]


# --- recovery: the reason the feature is survivable ------------------------


def test_a_recovery_code_logs_you_in_without_the_authenticator(client: TestClient):
    """The failure this exists for: the phone is gone and there is nobody
    to ask for help."""
    register_workspace(client, email="tf13@example.com", workspace_name="TF13")
    _, codes = _enable(client, "tf13@example.com")
    client.post("/auth/logout")

    response = client.post(
        "/auth/login", json={"email": "tf13@example.com", "password": PASSWORD, "totp_code": codes[0]}
    )

    assert response.status_code == 200
    assert client.get("/auth/totp").json()["recovery_codes_remaining"] == 9


def test_a_recovery_code_works_only_once(client: TestClient):
    """A reusable recovery code is a password with extra steps."""
    register_workspace(client, email="tf14@example.com", workspace_name="TF14")
    _, codes = _enable(client, "tf14@example.com")
    client.post("/auth/logout")

    client.post("/auth/login", json={"email": "tf14@example.com", "password": PASSWORD, "totp_code": codes[0]})
    client.post("/auth/logout")
    again = client.post(
        "/auth/login", json={"email": "tf14@example.com", "password": PASSWORD, "totp_code": codes[0]}
    )

    assert again.status_code == 422


def test_recovery_codes_are_accepted_as_they_are_actually_typed(client: TestClient):
    """Off paper, locked out, in a hurry. Case and dashes are noise; the
    entropy is in the characters."""
    register_workspace(client, email="tf15@example.com", workspace_name="TF15")
    _, codes = _enable(client, "tf15@example.com")
    client.post("/auth/logout")

    messy = f"  {codes[0].upper().replace('-', ' ')}  "
    response = client.post(
        "/auth/login", json={"email": "tf15@example.com", "password": PASSWORD, "totp_code": messy}
    )

    assert response.status_code == 200


def test_recovery_still_works_when_the_secret_cannot_be_decrypted(client: TestClient):
    """A rotated-away encryption key makes the secret unreadable. That is
    not a wrong code — and the account must still be reachable, which is
    the second reason recovery codes exist."""
    register_workspace(client, email="tf16@example.com", workspace_name="TF16")
    _, codes = _enable(client, "tf16@example.com")

    with SessionLocal() as db:
        user = db.query(User).filter(User.email == "tf16@example.com").one()
        user.totp_secret = "gAAAAABmnotavalidtokenatall"
        db.commit()

    client.post("/auth/logout")
    response = client.post(
        "/auth/login", json={"email": "tf16@example.com", "password": PASSWORD, "totp_code": codes[0]}
    )

    assert response.status_code == 200


def test_regenerating_invalidates_every_previous_code(client: TestClient):
    """Codes get printed and filed. A set that grew instead of being
    replaced would keep an old printout working forever."""
    register_workspace(client, email="tf17@example.com", workspace_name="TF17")
    _, old_codes = _enable(client, "tf17@example.com")

    fresh = client.post("/auth/totp/recovery-codes").json()["recovery_codes"]

    assert len(fresh) == 10
    assert not set(fresh) & set(old_codes)
    assert client.get("/auth/totp").json()["recovery_codes_remaining"] == 10

    client.post("/auth/logout")
    stale = client.post(
        "/auth/login", json={"email": "tf17@example.com", "password": PASSWORD, "totp_code": old_codes[0]}
    )
    assert stale.status_code == 422


# --- turning it off --------------------------------------------------------


def test_disabling_requires_the_password(client: TestClient):
    """A stolen session cookie is enough to browse. It must not also be
    enough to quietly strip the second factor off the account."""
    register_workspace(client, email="tf18@example.com", workspace_name="TF18")
    _enable(client, "tf18@example.com")

    refused = client.post("/auth/totp/disable", json={"current_password": "not-the-password"})

    assert refused.status_code == 401
    assert client.get("/auth/totp").json()["enabled"] is True


def test_disabling_erases_the_secret_and_the_recovery_codes(client: TestClient):
    """Leaving them behind keeps a credential in the database for a feature
    the account no longer uses."""
    register_workspace(client, email="tf19@example.com", workspace_name="TF19")
    _enable(client, "tf19@example.com")

    assert client.post("/auth/totp/disable", json={"current_password": PASSWORD}).status_code == 204

    with SessionLocal() as db:
        user = db.query(User).filter(User.email == "tf19@example.com").one()
        assert user.totp_secret is None
        assert user.totp_recovery_hashes is None
        assert user.totp_enabled is False

    client.post("/auth/logout")
    assert client.post("/auth/login", json={"email": "tf19@example.com", "password": PASSWORD}).status_code == 200


# --- storage ---------------------------------------------------------------


def test_the_secret_is_encrypted_and_the_recovery_codes_are_hashed(client: TestClient):
    """A dump holding plaintext TOTP secrets is a second-factor bypass, not
    merely a data leak."""
    register_workspace(client, email="tf20@example.com", workspace_name="TF20")
    secret, codes = _enable(client, "tf20@example.com")

    with SessionLocal() as db:
        user = db.query(User).filter(User.email == "tf20@example.com").one()
        stored_secret = user.totp_secret or ""
        stored_hashes = user.totp_recovery_hashes or ""

    assert secret not in stored_secret
    assert decrypt_secret(stored_secret) == secret
    for code in codes:
        assert code not in stored_hashes
        assert code.replace("-", "") not in stored_hashes


def test_no_endpoint_ever_returns_the_secret_again(client: TestClient):
    """Setup is the single opportunity to copy it."""
    register_workspace(client, email="tf21@example.com", workspace_name="TF21")
    secret, codes = _enable(client, "tf21@example.com")

    for path in ("/auth/totp", "/auth/me", "/auth/me/summary", "/auth/me/export"):
        body = client.get(path).text
        assert secret not in body, path
        for code in codes:
            assert code not in body, path


def test_enabling_and_disabling_are_audited(client: TestClient):
    register_workspace(client, email="tf22@example.com", workspace_name="TF22")
    _enable(client, "tf22@example.com")
    client.post("/auth/totp/disable", json={"current_password": PASSWORD})

    actions = [row["action"] for row in client.get("/auth/me/export").json()["audit_log"]]

    assert "totp.enabled" in actions
    assert "totp.disabled" in actions


def test_the_code_that_enabled_it_cannot_then_log_you_in(client: TestClient):
    """Found by a test I had written wrong. Enrolment accepts a code and
    records its step, so that code is spent — reusing it seconds later to
    log in is a replay and is refused. Correct, and worth pinning: someone
    reading the replay defence should not "fix" it into letting the
    enrolment code through."""
    register_workspace(client, email="tf23@example.com", workspace_name="TF23")
    assert client.post("/auth/totp/setup").status_code == 200
    secret = _stored_secret("tf23@example.com")
    used = pyotp.TOTP(secret).now()
    assert client.post("/auth/totp/enable", json={"code": used}).status_code == 200
    client.post("/auth/logout")

    replay = client.post(
        "/auth/login", json={"email": "tf23@example.com", "password": PASSWORD, "totp_code": used}
    )
    fresh = client.post(
        "/auth/login", json={"email": "tf23@example.com", "password": PASSWORD, "totp_code": _next_code(secret)}
    )

    assert replay.status_code == 422
    assert fresh.status_code == 200
