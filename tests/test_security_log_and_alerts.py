"""The security record, and the alert for a sign-in you did not make.

Ideas 88 and 89. Both answer the same question — "what happened to my
account?" — at the two moments it gets asked: while reviewing, and the
instant it happens.
"""

from __future__ import annotations

import asyncio

import pyotp
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.models import AuthSession, BotLink, User
from app.notify import describe_device, is_familiar_device, new_device_message, send_to_workspace
from app.totp import decrypt_secret
from tests.conftest import register_workspace

PASSWORD = "j8Kd0-slwQ2x"


# --- the security-only export (idea 88) ------------------------------------


def test_the_security_export_carries_no_collected_content(client: TestClient):
    """The whole reason it is separate: it is narrow enough to hand to
    someone helping you investigate, which the full export is not."""
    register_workspace(client, email="sl1@example.com", workspace_name="SL1")
    client.post("/links", json={"text": "https://private.example/secret-research.pdf"})

    body = client.get("/auth/me/security-export").json()

    assert "links" not in body
    assert "channels" not in body
    assert "secret-research" not in str(body)


def test_it_reports_sessions_attempts_and_second_factor_state(client: TestClient):
    register_workspace(client, email="sl2@example.com", workspace_name="SL2")

    body = client.get("/auth/me/security-export").json()

    assert body["account"]["email"] == "sl2@example.com"
    assert body["two_factor"] == {"enabled": False, "recovery_codes_remaining": 0}
    assert len(body["active_sessions"]) == 1
    assert isinstance(body["recent_failed_sign_ins"], list)
    assert any(event["action"] == "user.register" for event in body["security_events"])


def test_security_events_exclude_routine_collection_activity(client: TestClient):
    """A log full of "link added" is a log nobody reads."""
    register_workspace(client, email="sl3@example.com", workspace_name="SL3")
    client.post("/links", json={"text": "https://example.com/a.pdf"})
    link_id = client.get("/links").json()["items"][0]["id"]
    client.patch(f"/links/{link_id}", json={"category": "games"})

    actions = {e["action"] for e in client.get("/auth/me/security-export").json()["security_events"]}

    assert "user.register" in actions
    assert not any(a.startswith("link.") for a in actions)


def test_second_factor_changes_appear_in_the_security_log(client: TestClient):
    register_workspace(client, email="sl4@example.com", workspace_name="SL4")
    client.post("/auth/totp/setup")
    with SessionLocal() as db:
        secret = decrypt_secret(db.query(User).filter(User.email == "sl4@example.com").one().totp_secret or "")
    assert secret is not None
    client.post("/auth/totp/enable", json={"code": pyotp.TOTP(secret).now()})

    actions = {e["action"] for e in client.get("/auth/me/security-export").json()["security_events"]}

    assert "totp.enabled" in actions


def test_the_export_records_its_own_requester_address(client: TestClient):
    """Reading the security log is itself a security event."""
    register_workspace(client, email="sl5@example.com", workspace_name="SL5")

    client.get("/auth/me/security-export", headers={"X-Forwarded-For": "203.0.113.44"})
    events = client.get("/auth/me/security-export").json()["security_events"]

    exports = [e for e in events if e["action"] == "workspace.security_export"]
    assert exports
    assert exports[0]["ip_address"] == "203.0.113.44"


def test_one_workspace_cannot_read_anothers_security_log(client: TestClient):
    register_workspace(client, email="sl6a@example.com", workspace_name="SL6A")
    client.post("/auth/logout")
    register_workspace(client, email="sl6b@example.com", workspace_name="SL6B")

    body = client.get("/auth/me/security-export").json()

    assert body["account"]["email"] == "sl6b@example.com"
    assert "sl6a@example.com" not in str(body)


def test_the_documented_security_actions_are_ones_the_code_emits(client: TestClient):
    """The vocabulary is an explicit list, so a renamed action would drop
    out of the export silently. This is the check that notices."""
    from app.audit import AUDITED_SECURITY_ACTIONS

    register_workspace(client, email="sl7@example.com", workspace_name="SL7")
    client.post("/auth/api-keys", json={"name": "k"})
    client.get("/auth/me/export")

    emitted = {row["action"] for row in client.get("/auth/me/export").json()["audit_log"]}
    security_emitted = {a for a in emitted if not a.startswith("link.")}

    assert security_emitted <= set(AUDITED_SECURITY_ACTIONS), (
        f"emitted but not in the security vocabulary: {sorted(security_emitted - set(AUDITED_SECURITY_ACTIONS))}"
    )


# --- new-device detection (idea 89) ----------------------------------------


def test_a_brand_new_account_is_not_alerted_about_its_own_first_login(client: TestClient):
    """Alerting someone about the login they are performing teaches them
    the alert means nothing."""
    register_workspace(client, email="nd1@example.com", workspace_name="ND1")

    with SessionLocal() as db:
        user = db.query(User).filter(User.email == "nd1@example.com").one()
        db.query(AuthSession).filter(AuthSession.user_id == user.id).delete()
        db.commit()
        assert is_familiar_device(db, user.id, ip_address="203.0.113.1", user_agent="Firefox") is True


def test_a_returning_device_is_recognised(client: TestClient):
    register_workspace(client, email="nd2@example.com", workspace_name="ND2")

    with SessionLocal() as db:
        user = db.query(User).filter(User.email == "nd2@example.com").one()
        session = db.query(AuthSession).filter(AuthSession.user_id == user.id).first()
        assert session is not None
        session.ip_address = "203.0.113.7"
        session.user_agent = "Firefox/140"
        db.commit()

        assert is_familiar_device(db, user.id, ip_address="203.0.113.7", user_agent="Chrome/1") is True
        assert is_familiar_device(db, user.id, ip_address="198.51.100.9", user_agent="Firefox/140") is True
        assert is_familiar_device(db, user.id, ip_address="198.51.100.9", user_agent="Chrome/1") is False


def test_a_user_agent_cannot_smuggle_markup_into_the_chat(client: TestClient):
    """The value is supplied by whoever made the request. Relaying it
    verbatim into a chat message is an injection surface for no gain."""
    described = describe_device(
        ip_address="203.0.113.1", user_agent="*bold* _italic_ [link](http://evil.example) `code`"
    )

    for char in ("*", "_", "[", "]", "`"):
        assert char not in described


def test_a_long_user_agent_is_summarised_not_pasted(client: TestClient):
    described = describe_device(ip_address=None, user_agent="X" * 500)

    assert len(described) < 100


def test_the_alert_says_what_to_do_about_it(client: TestClient):
    """An alert that reports a problem without a next step is noise."""
    message = new_device_message(ip_address="203.0.113.1", user_agent="Firefox")

    assert "203.0.113.1" in message
    assert "كلمة المرور" in message


def test_delivery_is_a_no_op_when_no_bot_is_configured(client: TestClient):
    """The bot is optional, so the alert must degrade rather than fail."""
    register_workspace(client, email="nd3@example.com", workspace_name="ND3")
    workspace_id = client.get("/auth/me").json()["workspace_id"]

    with SessionLocal() as db:
        delivered = asyncio.run(send_to_workspace(db, workspace_id, "hello"))

    assert delivered == 0


def test_a_chat_that_blocked_the_bot_does_not_break_the_login(client: TestClient, monkeypatch):
    """A notification failure is not an authentication failure."""
    register_workspace(client, email="nd4@example.com", workspace_name="ND4")
    workspace_id = client.get("/auth/me").json()["workspace_id"]

    with SessionLocal() as db:
        db.add(BotLink(chat_id="42", workspace_id=workspace_id))
        db.commit()

    class ExplodingBot:
        async def send_message(self, **kwargs):
            raise RuntimeError("bot was blocked by the user")

    import app.bot.telegram_bot as bot_module

    monkeypatch.setattr(bot_module, "get_bot", lambda: ExplodingBot())

    with SessionLocal() as db:
        delivered = asyncio.run(send_to_workspace(db, workspace_id, "hello"))

    assert delivered == 0


def test_an_unfamiliar_login_reaches_the_linked_chat(client: TestClient, monkeypatch):
    """The end-to-end path, with the bot faked at the transport only."""
    register_workspace(client, email="nd5@example.com", workspace_name="ND5")
    workspace_id = client.get("/auth/me").json()["workspace_id"]

    with SessionLocal() as db:
        db.add(BotLink(chat_id="99", workspace_id=workspace_id))
        user = db.query(User).filter(User.email == "nd5@example.com").one()
        for s in db.query(AuthSession).filter(AuthSession.user_id == user.id):
            s.ip_address = "203.0.113.1"
            s.user_agent = "Firefox/140"
        db.commit()

    sent: list[dict] = []

    class RecordingBot:
        async def send_message(self, **kwargs):
            sent.append(kwargs)

    import app.bot.telegram_bot as bot_module

    monkeypatch.setattr(bot_module, "get_bot", lambda: RecordingBot())

    client.post("/auth/logout")
    response = client.post(
        "/auth/login",
        json={"email": "nd5@example.com", "password": PASSWORD},
        headers={"X-Forwarded-For": "198.51.100.55", "User-Agent": "Safari/18"},
    )

    assert response.status_code == 200
    assert len(sent) == 1
    assert sent[0]["chat_id"] == "99"
    assert "198.51.100.55" in sent[0]["text"]


def test_a_familiar_login_sends_nothing(client: TestClient, monkeypatch):
    register_workspace(client, email="nd6@example.com", workspace_name="ND6")
    workspace_id = client.get("/auth/me").json()["workspace_id"]

    with SessionLocal() as db:
        db.add(BotLink(chat_id="98", workspace_id=workspace_id))
        user = db.query(User).filter(User.email == "nd6@example.com").one()
        for s in db.query(AuthSession).filter(AuthSession.user_id == user.id):
            s.ip_address = "203.0.113.2"
            s.user_agent = "Firefox/140"
        db.commit()

    sent: list[dict] = []

    class RecordingBot:
        async def send_message(self, **kwargs):
            sent.append(kwargs)

    import app.bot.telegram_bot as bot_module

    monkeypatch.setattr(bot_module, "get_bot", lambda: RecordingBot())

    client.post("/auth/logout")
    client.post(
        "/auth/login",
        json={"email": "nd6@example.com", "password": PASSWORD},
        headers={"X-Forwarded-For": "203.0.113.2", "User-Agent": "Firefox/140"},
    )

    assert sent == []
