"""Adding a collecting account from the dashboard, not a terminal.

The interesting failure here is not "the login fails" — it is "the login
succeeds and the wrong thing gets stored, or the wrong caller can trigger
it." So most of these tests are about the guards around the happy path
(password re-confirmation, the label/limit check running before Telegram
is ever contacted, the session string landing encrypted) rather than the
happy path itself, which is the shortest test in the file.

``FakeTelethonClient`` stands in for ``telethon.TelegramClient`` via
``app.account_login._make_client`` — the one seam the module exposes for
exactly this reason. Errors raised through it are real ``telethon.errors``
instances (they accept ``request=None`` happily), so the except clauses
under test are the real ones, not a lookalike.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from telethon.errors import PhoneCodeInvalidError, SessionPasswordNeededError

from app import account_login
from app.config import get_settings
from app.crypto import decrypt_field
from app.database import SessionLocal
from app.models import TelegramAccount
from tests.conftest import register_workspace

CURRENT_PASSWORD = "j8Kd0-slwQ2x"


class _FakeSession:
    def __init__(self, value: str) -> None:
        self._value = value

    def save(self) -> str:
        return self._value


class FakeTelethonClient:
    """Consumes ``code_error`` / ``password_error`` once, then behaves —
    so a test can simulate "wrong code, then the right one" on the same
    client, which is exactly what the retry path in ``verify_login`` is
    for."""

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        *,
        needs_password: bool = False,
        code_error: Exception | None = None,
        password_error: Exception | None = None,
        session_value: str = "raw-telethon-session-string",
    ) -> None:
        self.api_id = api_id
        self.api_hash = api_hash
        self.connected = False
        self.disconnected = False
        self.phone: str | None = None
        self._needs_password = needs_password
        self._code_error = code_error
        self._password_error = password_error
        self.session = _FakeSession(session_value)

    async def connect(self) -> None:
        self.connected = True

    async def send_code_request(self, phone: str) -> None:
        self.phone = phone

    async def sign_in(
        self, phone: str | None = None, code: str | None = None, password: str | None = None
    ) -> None:
        if password is not None:
            if self._password_error is not None:
                err, self._password_error = self._password_error, None
                raise err
            return
        if self._needs_password:
            self._needs_password = False
            raise SessionPasswordNeededError(request=None)
        if self._code_error is not None:
            err, self._code_error = self._code_error, None
            raise err

    async def disconnect(self) -> None:
        self.disconnected = True


def _patch_client(monkeypatch, **kwargs) -> list[FakeTelethonClient]:
    created: list[FakeTelethonClient] = []

    def factory(api_id: int, api_hash: str) -> FakeTelethonClient:
        c = FakeTelethonClient(api_id, api_hash, **kwargs)
        created.append(c)
        return c

    monkeypatch.setattr(account_login, "_make_client", factory)
    return created


def _configure_tg(monkeypatch) -> None:
    monkeypatch.setenv("TG_API_ID", "123456")
    monkeypatch.setenv("TG_API_HASH", "deadbeefcafebabe")


def _start(
    client: TestClient, *, label: str = "second account", phone: str = "+9665xxxxxxxx", password=CURRENT_PASSWORD
):
    return client.post(
        "/channels/accounts/login/start",
        json={"current_password": password, "label": label, "phone": phone},
    )


def test_the_full_login_flow_adds_an_encrypted_account(client: TestClient, monkeypatch):
    register_workspace(client, email="al1@example.com", workspace_name="AL1")
    _configure_tg(monkeypatch)
    _patch_client(monkeypatch)

    start = _start(client)
    assert start.status_code == 200, start.text
    token = start.json()["login_token"]

    verify = client.post("/channels/accounts/login/verify", json={"login_token": token, "code": "12345"})
    assert verify.status_code == 200, verify.text
    body = verify.json()
    assert body["status"] == "added"
    assert body["account"]["label"] == "second account"
    assert "session_string" not in body["account"]

    db = SessionLocal()
    try:
        row = db.query(TelegramAccount).filter(TelegramAccount.label == "second account").one()
        assert row.session_string != "raw-telethon-session-string"  # stored encrypted, not raw
        assert decrypt_field(row.session_string) == "raw-telethon-session-string"
    finally:
        db.close()

    listed = client.get("/channels/accounts").json()
    assert any(a["label"] == "second account" for a in listed)


def test_starting_a_login_requires_the_correct_password(client: TestClient, monkeypatch):
    register_workspace(client, email="al2@example.com", workspace_name="AL2")
    _configure_tg(monkeypatch)
    created = _patch_client(monkeypatch)

    resp = _start(client, password="wrong password entirely")
    assert resp.status_code == 401
    assert created == []  # Telegram was never contacted


def test_start_fails_cleanly_without_tg_credentials_configured(client: TestClient, monkeypatch):
    register_workspace(client, email="al3@example.com", workspace_name="AL3")
    monkeypatch.delenv("TG_API_ID", raising=False)
    monkeypatch.delenv("TG_API_HASH", raising=False)
    created = _patch_client(monkeypatch)

    resp = _start(client)
    assert resp.status_code == 400
    assert "TG_API_ID" in resp.json()["detail"] or "TG_API_HASH" in resp.json()["detail"]
    assert created == []


def test_the_account_limit_is_checked_before_contacting_telegram(client: TestClient, monkeypatch):
    register_workspace(client, email="al4@example.com", workspace_name="AL4")
    _configure_tg(monkeypatch)
    monkeypatch.setattr(get_settings(), "max_accounts_per_workspace", 0)
    created = _patch_client(monkeypatch)

    resp = _start(client)
    assert resp.status_code == 400
    assert "الحد الأقصى" in resp.json()["detail"]
    assert created == []  # the limit is cheap; Telegram is not bothered for a request that cannot succeed


def test_a_duplicate_label_is_rejected(client: TestClient, monkeypatch):
    register_workspace(client, email="al5@example.com", workspace_name="AL5")
    _configure_tg(monkeypatch)
    created = _patch_client(monkeypatch)

    me = client.get("/auth/me").json()
    db = SessionLocal()
    try:
        db.add(
            TelegramAccount(workspace_id=me["workspace_id"], label="taken", session_string="irrelevant-ciphertext")
        )
        db.commit()
    finally:
        db.close()

    resp = _start(client, label="taken")
    assert resp.status_code == 400
    assert "taken" in resp.json()["detail"]
    assert created == []


def test_two_factor_login_asks_for_the_password_then_succeeds(client: TestClient, monkeypatch):
    register_workspace(client, email="al6@example.com", workspace_name="AL6")
    _configure_tg(monkeypatch)
    _patch_client(monkeypatch, needs_password=True)

    token = _start(client).json()["login_token"]

    first = client.post("/channels/accounts/login/verify", json={"login_token": token, "code": "12345"})
    assert first.status_code == 200
    assert first.json()["status"] == "needs_password"

    second = client.post(
        "/channels/accounts/login/verify", json={"login_token": token, "password": "the 2fa password"}
    )
    assert second.status_code == 200
    assert second.json()["status"] == "added"


def test_a_wrong_code_can_be_retried_on_the_same_token(client: TestClient, monkeypatch):
    register_workspace(client, email="al7@example.com", workspace_name="AL7")
    _configure_tg(monkeypatch)
    _patch_client(monkeypatch, code_error=PhoneCodeInvalidError(request=None))

    token = _start(client).json()["login_token"]

    wrong = client.post("/channels/accounts/login/verify", json={"login_token": token, "code": "00000"})
    assert wrong.status_code == 400
    assert "غير صحيح" in wrong.json()["detail"]

    right = client.post("/channels/accounts/login/verify", json={"login_token": token, "code": "12345"})
    assert right.status_code == 200
    assert right.json()["status"] == "added"


def test_an_unknown_or_expired_token_is_rejected(client: TestClient):
    register_workspace(client, email="al8@example.com", workspace_name="AL8")
    resp = client.post(
        "/channels/accounts/login/verify", json={"login_token": "not-a-real-token", "code": "12345"}
    )
    assert resp.status_code == 400


def test_one_workspace_cannot_verify_another_workspaces_pending_login(client: TestClient, monkeypatch):
    register_workspace(client, email="al9@example.com", workspace_name="AL9")
    _configure_tg(monkeypatch)
    _patch_client(monkeypatch)
    token = _start(client).json()["login_token"]

    other = TestClient(client.app)
    register_workspace(other, email="al9b@example.com", workspace_name="AL9B")
    resp = other.post("/channels/accounts/login/verify", json={"login_token": token, "code": "12345"})
    assert resp.status_code == 400


def test_repeated_start_attempts_are_rate_limited(client: TestClient, monkeypatch):
    register_workspace(client, email="al10@example.com", workspace_name="AL10")
    _configure_tg(monkeypatch)
    monkeypatch.setattr(get_settings(), "max_accounts_per_workspace", 1000)
    _patch_client(monkeypatch)

    from app.routers.channels import ACCOUNT_LOGIN_LIMIT

    for i in range(ACCOUNT_LOGIN_LIMIT):
        resp = _start(client, label=f"account {i}")
        assert resp.status_code == 200, resp.text

    limited = _start(client, label="one too many")
    assert limited.status_code == 429
