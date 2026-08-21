"""The one place this service makes a request to an address a user chose.

Idea 162. Every other outbound call in the project goes to a constant
(Telegram, Groq); this one goes wherever a workspace says. So the tests
here are mostly about refusal, and the ones that matter most assert that
**no request was made at all** rather than that a request failed — a
refusal after the packet has left is not a refusal.
"""

from __future__ import annotations

import asyncio
import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from app.alerts import COLLECTOR_FAILED
from app.database import SessionLocal
from app.errors import ERROR_CODE_HEADER, ErrorCode
from app.models import Notification, Workspace
from app.notify import raise_alert
from app.webhook import WebhookRefused, mask, validate
from tests.conftest import register_workspace

PUBLIC = "https://hooks.example.com/services/T000/B000/SECRETTOKEN"


@pytest.fixture
def resolves_publicly(monkeypatch):
    """Pin DNS so the tests describe the code, not the network they run on."""

    def _fake(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port or 443))]

    monkeypatch.setattr(socket, "getaddrinfo", _fake)
    return _fake


def _resolve_to(monkeypatch, address: str, family=socket.AF_INET):
    def _fake(host, port, *args, **kwargs):
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port or 443))]

    monkeypatch.setattr(socket, "getaddrinfo", _fake)


# --- what the URL itself has to be -----------------------------------------


def test_http_is_refused(resolves_publicly):
    with pytest.raises(WebhookRefused):
        validate("http://hooks.example.com/x")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/y", "ftp://x/y", "//hooks.example.com/x", "x"])
def test_only_https_survives(resolves_publicly, url):
    with pytest.raises(WebhookRefused):
        validate(url)


def test_a_public_https_url_is_accepted(resolves_publicly):
    assert validate(PUBLIC) == PUBLIC


def test_an_absurdly_long_url_is_refused(resolves_publicly):
    with pytest.raises(WebhookRefused):
        validate("https://hooks.example.com/" + "a" * 5000)


# --- what the URL must not resolve to --------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",  # loopback
        "10.0.0.5",  # private
        "192.168.1.1",  # private
        "172.16.0.1",  # private
        "169.254.169.254",  # link-local: the cloud metadata address
        "0.0.0.0",  # unspecified
        "224.0.0.1",  # multicast
        "240.0.0.1",  # reserved
    ],
)
def test_internal_addresses_are_refused(monkeypatch, address):
    _resolve_to(monkeypatch, address)

    with pytest.raises(WebhookRefused):
        validate("https://looks-fine.example.com/hook")


def test_ipv6_loopback_is_refused(monkeypatch):
    _resolve_to(monkeypatch, "::1", family=socket.AF_INET6)

    with pytest.raises(WebhookRefused):
        validate("https://looks-fine.example.com/hook")


@pytest.mark.parametrize("address", ["::ffff:127.0.0.1", "::ffff:169.254.169.254", "::ffff:10.0.0.5"])
def test_an_internal_address_wearing_an_ipv6_hat_is_refused(monkeypatch, address):
    """Pins the outcome, deliberately not the mechanism.

    On CPython 3.11.15 these already answer True to is_private without any
    unwrapping, so this passes with or without _public_form today. That is
    exactly why the assertion is written about the refusal rather than
    about is_loopback: how CPython classifies mapped addresses has been
    corrected more than once, and a test that pinned the interpreter's
    opinion would be pinning something that is allowed to change.
    """
    _resolve_to(monkeypatch, address, family=socket.AF_INET6)

    with pytest.raises(WebhookRefused):
        validate("https://looks-fine.example.com/hook")


def test_a_mapped_public_address_is_still_allowed(monkeypatch):
    """Unwrapping must not turn every IPv6-wrapped address into a refusal."""
    _resolve_to(monkeypatch, "::ffff:93.184.216.34", family=socket.AF_INET6)

    assert validate(PUBLIC) == PUBLIC


def test_one_internal_address_among_several_is_enough_to_refuse(monkeypatch):
    """A host with both a public and a private A record must not be usable:
    which one the client picks is not this code's decision."""

    def _fake(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _fake)
    with pytest.raises(WebhookRefused):
        validate("https://split-horizon.example.com/hook")


def test_a_refusal_never_names_the_internal_address(monkeypatch):
    """It is precisely what somebody probing internal ranges wants back."""
    _resolve_to(monkeypatch, "169.254.169.254")

    with pytest.raises(WebhookRefused) as exc:
        validate("https://probe.example.com/hook")

    assert "169.254" not in str(exc.value)


def test_a_name_that_does_not_resolve_is_refused(monkeypatch):
    def _fake(*args, **kwargs):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", _fake)
    with pytest.raises(WebhookRefused):
        validate("https://nope.invalid/hook")


# --- what is stored, and what is ever shown --------------------------------


def test_the_url_is_never_returned_whole(client: TestClient, resolves_publicly):
    register_workspace(client, email="w1@example.com", workspace_name="W1")

    stored = client.put("/notifications/webhook", json={"url": PUBLIC}).json()

    assert stored["configured"] is True
    assert "SECRETTOKEN" not in stored["masked_url"]
    assert "SECRETTOKEN" not in client.get("/notifications/webhook").text


def test_the_url_is_encrypted_at_rest(client: TestClient, resolves_publicly):
    """A database dump must not be a set of keys to other people's channels."""
    register_workspace(client, email="w2@example.com", workspace_name="W2")
    client.put("/notifications/webhook", json={"url": PUBLIC})

    with SessionLocal() as db:
        raw = db.query(Workspace).one().webhook_url

    assert raw is not None
    assert "SECRETTOKEN" not in raw
    assert "hooks.example.com" not in raw


def test_the_export_carries_the_fact_but_not_the_credential(client: TestClient, resolves_publicly):
    register_workspace(client, email="w3@example.com", workspace_name="W3")
    client.put("/notifications/webhook", json={"url": PUBLIC})

    export = client.get("/auth/me/export").text

    assert "SECRETTOKEN" not in export
    assert "hooks.example.com" in export


def test_the_audit_log_records_the_masked_form(client: TestClient, resolves_publicly):
    """An audit log is read by more people and kept longer than the setting."""
    register_workspace(client, email="w4@example.com", workspace_name="W4")
    client.put("/notifications/webhook", json={"url": PUBLIC})

    activity = client.get("/auth/security-activity").text
    assert "SECRETTOKEN" not in activity


def test_a_refused_url_answers_with_a_stable_code(client: TestClient, monkeypatch):
    register_workspace(client, email="w5@example.com", workspace_name="W5")
    _resolve_to(monkeypatch, "127.0.0.1")

    response = client.put("/notifications/webhook", json={"url": "https://evil.example.com/x"})

    assert response.status_code == 422
    assert response.headers[ERROR_CODE_HEADER] == ErrorCode.WEBHOOK_REFUSED


def test_clearing_it_removes_the_stored_value(client: TestClient, resolves_publicly):
    register_workspace(client, email="w6@example.com", workspace_name="W6")
    client.put("/notifications/webhook", json={"url": PUBLIC})

    assert client.delete("/notifications/webhook").status_code == 204

    assert client.get("/notifications/webhook").json()["configured"] is False
    with SessionLocal() as db:
        assert db.query(Workspace).one().webhook_url is None


def test_mask_keeps_the_host_and_loses_everything_else():
    """Not even the last few characters: they are the tail of a secret, and
    with one webhook per workspace there is no second one to tell it from."""
    masked = mask(PUBLIC)

    assert masked == "https://hooks.example.com/…"
    assert "SECRETTOKEN"[-4:] not in masked


# --- what actually goes over the wire --------------------------------------


class _Recorder:
    """Stands in for httpx.AsyncClient, and records how it was constructed."""

    instances: list[_Recorder] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls: list[tuple[str, dict]] = []
        _Recorder.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return httpx.Response(200, request=httpx.Request("POST", url))


@pytest.fixture
def recorder(monkeypatch):
    _Recorder.instances = []
    monkeypatch.setattr(httpx, "AsyncClient", _Recorder)
    return _Recorder


def test_redirects_are_not_followed(client: TestClient, resolves_publicly, recorder):
    """The classic bypass: a 302 from a public host to 169.254.169.254
    would undo every address check above in a single hop."""
    register_workspace(client, email="w7@example.com", workspace_name="W7")
    client.put("/notifications/webhook", json={"url": PUBLIC})

    client.post("/notifications/webhook/test")

    assert recorder.instances, "no request was made at all"
    assert recorder.instances[0].kwargs["follow_redirects"] is False


def test_the_request_has_a_bounded_timeout(client: TestClient, resolves_publicly, recorder):
    register_workspace(client, email="w8@example.com", workspace_name="W8")
    client.put("/notifications/webhook", json={"url": PUBLIC})

    client.post("/notifications/webhook/test")

    assert recorder.instances[0].kwargs["timeout"] <= 10


def test_the_payload_identifies_the_alert_and_nobody(client: TestClient, resolves_publicly, recorder):
    """The receiving end is a third party the platform knows nothing about,
    so it is told the alert and not who it belongs to."""
    register_workspace(client, email="w9@example.com", workspace_name="W9")
    client.put("/notifications/webhook", json={"url": PUBLIC})

    with SessionLocal() as db:
        workspace_id = db.query(Workspace).one().id
        asyncio.run(raise_alert(db, workspace_id, COLLECTOR_FAILED.key, title="t", body="b"))

    sent = recorder.instances[-1].calls[-1][1]["json"]
    assert set(sent) == {"type", "title", "body", "sent_at", "source"}
    assert "w9@example.com" not in str(sent)
    assert str(workspace_id) not in str(sent.get("source", ""))


def test_an_alert_that_the_switch_refused_is_not_forwarded(client: TestClient, resolves_publicly, recorder):
    """The webhook must not become a second channel the gate does not
    control — one that did would make the phase's exit criterion false."""
    register_workspace(client, email="w10@example.com", workspace_name="W10")
    client.put("/notifications/webhook", json={"url": PUBLIC})
    client.patch(f"/notifications/preferences/{COLLECTOR_FAILED.key}", json={"enabled": False})

    with SessionLocal() as db:
        workspace_id = db.query(Workspace).one().id
        asyncio.run(raise_alert(db, workspace_id, COLLECTOR_FAILED.key, title="t", body="b"))
        assert db.query(Notification).count() == 0

    assert not [i for i in recorder.instances if i.calls]


def test_a_workspace_with_no_webhook_makes_no_request(client: TestClient, resolves_publicly, recorder):
    register_workspace(client, email="w11@example.com", workspace_name="W11")

    with SessionLocal() as db:
        workspace_id = db.query(Workspace).one().id
        asyncio.run(raise_alert(db, workspace_id, COLLECTOR_FAILED.key, title="t", body="b"))

    assert not [i for i in recorder.instances if i.calls]


def test_a_webhook_failure_does_not_cost_the_alert(client: TestClient, resolves_publicly, monkeypatch):
    """The alert is the product; the webhook is a copy of it."""
    register_workspace(client, email="w12@example.com", workspace_name="W12")
    client.put("/notifications/webhook", json={"url": PUBLIC})

    class _Exploding(_Recorder):
        async def post(self, url, **kwargs):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "AsyncClient", _Exploding)

    with SessionLocal() as db:
        workspace_id = db.query(Workspace).one().id
        asyncio.run(raise_alert(db, workspace_id, COLLECTOR_FAILED.key, title="t", body="b"))
        assert db.query(Notification).count() == 1

    assert client.get("/notifications").json()["unread"] == 1


def test_the_last_attempt_is_recorded_so_the_setup_is_debuggable(client: TestClient, resolves_publicly, recorder):
    register_workspace(client, email="w13@example.com", workspace_name="W13")
    client.put("/notifications/webhook", json={"url": PUBLIC})

    state = client.post("/notifications/webhook/test").json()

    assert state["last_status"] == 200
    assert state["last_attempt_at"] is not None


def test_a_url_that_turns_internal_after_it_was_saved_stops_being_used(
    client: TestClient, resolves_publicly, recorder, monkeypatch
):
    """The check runs again immediately before every send, not only once at
    save time — otherwise a name only has to look public for a moment."""
    register_workspace(client, email="w14@example.com", workspace_name="W14")
    client.put("/notifications/webhook", json={"url": PUBLIC})

    _resolve_to(monkeypatch, "127.0.0.1")
    with SessionLocal() as db:
        workspace_id = db.query(Workspace).one().id
        asyncio.run(raise_alert(db, workspace_id, COLLECTOR_FAILED.key, title="t", body="b"))

    assert not [i for i in recorder.instances if i.calls], "a request was made to an internal address"
