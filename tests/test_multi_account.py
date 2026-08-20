"""Multi-account collection: several Telegram accounts, one workspace.

The data model always allowed a workspace to hold more than one
``TelegramAccount``, but the collector only ever used the first one. These
tests cover the split-by-account behaviour and — more importantly — the
isolation guarantee: one broken account must not cost the run the channels
every other account could still have collected.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.crypto import encrypt_field
from app.database import SessionLocal
from app.models import Channel, Link, TelegramAccount, Workspace
from scripts import collect as collector
from tests.conftest import register_workspace
from tests.test_collector import FakeClient, FakeMessage

SESSION = "x" * 120


@pytest.fixture
def workspace():
    db = SessionLocal()
    try:
        ws = Workspace(name="Multi WS")
        db.add(ws)
        db.commit()
        return ws.id
    finally:
        db.close()


def _add_account(workspace_id: int, label: str, *, session: str = SESSION, active: bool = True) -> int:
    db = SessionLocal()
    try:
        account = TelegramAccount(
            workspace_id=workspace_id,
            label=label,
            session_string=encrypt_field(session),
            is_active=active,
        )
        db.add(account)
        db.commit()
        return account.id
    finally:
        db.close()


def _add_channel(workspace_id: int, tg_id: str, account_id: int | None = None) -> int:
    db = SessionLocal()
    try:
        channel = Channel(
            workspace_id=workspace_id, tg_channel_id=tg_id, username=f"c{tg_id}", account_id=account_id
        )
        db.add(channel)
        db.commit()
        return channel.id
    finally:
        db.close()


def _run_collect(monkeypatch, workspace_id: int, clients_by_session: dict[str, object]):
    """Run collect() with a fake TelegramClient keyed by session string."""
    started: list[str] = []

    class _FakeTelegramClient:
        def __init__(self, session, api_id, api_hash):
            self._session = str(session)
            self._inner = clients_by_session[self._session]

        async def start(self):
            started.append(self._session)
            if isinstance(self._inner, Exception):
                raise self._inner

        async def disconnect(self):
            pass

        async def get_entity(self, ref):
            return await self._inner.get_entity(ref)

        def iter_messages(self, entity, **kwargs):
            return self._inner.iter_messages(entity, **kwargs)

    monkeypatch.setattr(collector, "TelegramClient", _FakeTelegramClient)
    monkeypatch.setattr(collector, "StringSession", lambda s: s)
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "hash")
    monkeypatch.setenv("TG_SESSION_STRING", SESSION)
    monkeypatch.setenv("COLLECTOR_WORKSPACE_ID", str(workspace_id))

    asyncio.run(collector.collect())
    return started


def _urls(workspace_id: int) -> set[str]:
    db = SessionLocal()
    try:
        return {r.url for r in db.query(Link).filter(Link.workspace_id == workspace_id).all()}
    finally:
        db.close()


def test_each_account_collects_only_its_own_channels(workspace, monkeypatch):
    first = _add_account(workspace, "first", session="session-one" + "x" * 110)
    second = _add_account(workspace, "second", session="session-two" + "x" * 110)
    _add_channel(workspace, "100", account_id=first)
    _add_channel(workspace, "200", account_id=second)

    db = SessionLocal()
    try:
        sessions = {a.label: a.session_string for a in db.query(TelegramAccount).all()}
    finally:
        db.close()

    from app.crypto import decrypt_field

    started = _run_collect(
        monkeypatch,
        workspace,
        {
            decrypt_field(sessions["first"]): FakeClient([FakeMessage(1, "https://example.com/one.apk")]),
            decrypt_field(sessions["second"]): FakeClient([FakeMessage(1, "https://example.com/two.apk")]),
        },
    )

    assert len(started) == 2
    assert _urls(workspace) == {"https://example.com/one.apk", "https://example.com/two.apk"}


def test_unassigned_channels_fall_to_the_default_account(workspace, monkeypatch):
    """A single-account workspace keeps working with nothing assigned."""
    _add_account(workspace, "primary")
    _add_channel(workspace, "100", account_id=None)

    _run_collect(monkeypatch, workspace, {SESSION: FakeClient([FakeMessage(1, "https://example.com/x.apk")])})

    assert _urls(workspace) == {"https://example.com/x.apk"}


def test_a_broken_account_does_not_stop_the_others(workspace, monkeypatch):
    """The isolation guarantee this feature exists for."""
    broken = _add_account(workspace, "broken", session="broken-session" + "x" * 110)
    working = _add_account(workspace, "working", session="working-session" + "x" * 110)
    _add_channel(workspace, "100", account_id=broken)
    _add_channel(workspace, "200", account_id=working)

    from app.crypto import decrypt_field

    db = SessionLocal()
    try:
        stored = {a.label: decrypt_field(a.session_string) for a in db.query(TelegramAccount).all()}
    finally:
        db.close()

    _run_collect(
        monkeypatch,
        workspace,
        {
            stored["broken"]: ConnectionError("session revoked"),
            stored["working"]: FakeClient([FakeMessage(1, "https://example.com/survived.apk")]),
        },
    )

    assert _urls(workspace) == {"https://example.com/survived.apk"}


def test_an_undecryptable_account_is_skipped_not_fatal(workspace, monkeypatch):
    """A row stored under a different FIELD_ENCRYPTION_KEY must not abort the run."""
    db = SessionLocal()
    try:
        db.add(TelegramAccount(workspace_id=workspace, label="stale", session_string="not-a-fernet-token"))
        db.commit()
    finally:
        db.close()
    working = _add_account(workspace, "working", session="good-session" + "x" * 110)
    _add_channel(workspace, "200", account_id=working)

    from app.crypto import decrypt_field

    db = SessionLocal()
    try:
        good = decrypt_field(
            db.query(TelegramAccount).filter(TelegramAccount.label == "working").one().session_string
        )
    finally:
        db.close()

    _run_collect(monkeypatch, workspace, {good: FakeClient([FakeMessage(1, "https://example.com/ok.apk")])})

    assert _urls(workspace) == {"https://example.com/ok.apk"}


def test_inactive_accounts_are_not_used(workspace, monkeypatch):
    _add_account(workspace, "primary")
    disabled = _add_account(workspace, "disabled", session="disabled" + "x" * 110, active=False)
    _add_channel(workspace, "100", account_id=disabled)

    started = _run_collect(monkeypatch, workspace, {SESSION: FakeClient([])})

    assert started == []  # the only active account owns no channels
    assert _urls(workspace) == set()


# --- API surface -----------------------------------------------------------


def test_accounts_endpoint_never_exposes_the_session_string(client: TestClient):
    register_workspace(client, email="acc@example.com", workspace_name="Acc Co")
    workspace_id = client.get("/auth/me").json()["workspace_id"]
    _add_account(workspace_id, "primary")

    body = client.get("/channels/accounts").json()

    assert [a["label"] for a in body] == ["primary"]
    assert all("session_string" not in a for a in body)


def test_channel_can_be_assigned_to_an_account(client: TestClient):
    register_workspace(client, email="acc2@example.com", workspace_name="Acc2")
    workspace_id = client.get("/auth/me").json()["workspace_id"]
    account_id = _add_account(workspace_id, "second")
    channel = client.post("/channels", json={"tg_channel_id": "1", "username": "c1"}).json()

    assert channel["account_id"] is None

    updated = client.patch(f"/channels/{channel['id']}", json={"account_id": account_id})

    assert updated.status_code == 200
    assert updated.json()["account_id"] == account_id


def test_assigning_another_workspaces_account_is_not_found(client: TestClient):
    register_workspace(client, email="acc3@example.com", workspace_name="Acc3")
    other_ws = client.get("/auth/me").json()["workspace_id"]
    foreign_account = _add_account(other_ws, "theirs")
    client.post("/auth/logout")

    register_workspace(client, email="acc4@example.com", workspace_name="Acc4")
    channel = client.post("/channels", json={"tg_channel_id": "9", "username": "c9"}).json()

    resp = client.patch(f"/channels/{channel['id']}", json={"account_id": foreign_account})

    assert resp.status_code == 404


def test_account_endpoints_require_authentication(client: TestClient):
    assert client.get("/channels/accounts").status_code == 401
    assert client.patch("/channels/1", json={"account_id": None}).status_code == 401
