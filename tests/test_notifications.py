"""Alerts, and the switches that decide whether they are ever sent.

Phase 9. The phase's exit criterion — every alert individually disableable
— is the gate, not one requirement among fifteen: an alerting system whose
switches arrive later has already sent the message somebody did not want,
and no later setting takes that back. So the preference tests come first
and the delivery tests assume them.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.alerts import ALERT_TYPES, COLLECTOR_FAILED, NEW_DEVICE, WEEKLY_DIGEST, default_for
from app.database import SessionLocal
from app.errors import ERROR_CODE_HEADER, ErrorCode
from app.models import AuthSession, BotLink, Notification, NotificationPreference, User
from app.notify import is_enabled, raise_alert
from tests.conftest import register_workspace

PASSWORD = "j8Kd0-slwQ2x"


def _workspace(client: TestClient) -> int:
    return client.get("/auth/me").json()["workspace_id"]


# --- the gate: every alert can be switched off -----------------------------


def test_every_alert_type_is_listed_and_individually_switchable(client: TestClient):
    """The exit criterion, tested against the catalogue rather than a
    hand-written list: a type added later without a switch fails here."""
    register_workspace(client, email="n1@example.com", workspace_name="N1")

    prefs = client.get("/notifications/preferences").json()

    assert {p["key"] for p in prefs} == {a.key for a in ALERT_TYPES}
    for pref in prefs:
        response = client.patch(f"/notifications/preferences/{pref['key']}", json={"enabled": False})
        assert response.status_code == 200, pref["key"]
        assert response.json()["enabled"] is False

    assert all(p["enabled"] is False for p in client.get("/notifications/preferences").json())


def test_the_catalogue_is_returned_whole_not_just_stored_rows(client: TestClient):
    """A switch you cannot see is a switch you cannot turn off."""
    register_workspace(client, email="n2@example.com", workspace_name="N2")

    with SessionLocal() as db:
        assert db.query(NotificationPreference).count() == 0

    assert len(client.get("/notifications/preferences").json()) == len(ALERT_TYPES)


def test_defaults_split_operational_alerts_from_digests(client: TestClient):
    """Two policies, because one would be wrong for half the list: silence
    is the failure an operational alert exists to fix, while a weekly
    summary nobody asked for is exactly the proactive sending the
    governing principle is about."""
    register_workspace(client, email="n3@example.com", workspace_name="N3")

    prefs = {p["key"]: p for p in client.get("/notifications/preferences").json()}

    assert prefs[COLLECTOR_FAILED.key]["enabled"] is True
    assert prefs[NEW_DEVICE.key]["enabled"] is True
    assert prefs[WEEKLY_DIGEST.key]["enabled"] is False
    assert all(p["is_default"] for p in prefs.values())


def test_an_explicit_choice_is_marked_as_one(client: TestClient):
    """ "On because I chose it" and "on because it ships that way" are
    different, and only the second is worth revisiting."""
    register_workspace(client, email="n4@example.com", workspace_name="N4")

    client.patch(f"/notifications/preferences/{WEEKLY_DIGEST.key}", json={"enabled": True})
    prefs = {p["key"]: p for p in client.get("/notifications/preferences").json()}

    assert prefs[WEEKLY_DIGEST.key] == {
        **prefs[WEEKLY_DIGEST.key],
        "enabled": True,
        "is_default": False,
    }


def test_an_unknown_alert_type_is_refused(client: TestClient):
    """A typo must not silently create a preference for a type nothing
    sends — nor become a new channel for unrequested messages."""
    register_workspace(client, email="n5@example.com", workspace_name="N5")

    response = client.patch("/notifications/preferences/made_up", json={"enabled": True})

    assert response.status_code == 422
    assert response.headers[ERROR_CODE_HEADER] == ErrorCode.UNKNOWN_ALERT_TYPE
    assert default_for("made_up") is False


def test_switching_an_alert_off_stops_it_being_recorded_at_all(client: TestClient):
    """Not merely undelivered — not raised. A disabled alert that still
    accumulated rows would be a log of messages you asked not to receive."""
    register_workspace(client, email="n6@example.com", workspace_name="N6")
    workspace_id = _workspace(client)
    client.patch(f"/notifications/preferences/{COLLECTOR_FAILED.key}", json={"enabled": False})

    with SessionLocal() as db:
        result = asyncio.run(raise_alert(db, workspace_id, COLLECTOR_FAILED.key, title="t", body="b"))
        assert result is None
        assert db.query(Notification).filter(Notification.workspace_id == workspace_id).count() == 0


def test_preference_changes_are_audited(client: TestClient):
    register_workspace(client, email="n7@example.com", workspace_name="N7")

    client.patch(f"/notifications/preferences/{WEEKLY_DIGEST.key}", json={"enabled": True})
    actions = [r["action"] for r in client.get("/auth/me/export").json()["audit_log"]]

    assert "notification.preference" in actions


# --- the centre, the log and the strip are one table -----------------------


def test_an_alert_is_recorded_even_when_nothing_could_receive_it(client: TestClient):
    """ "The platform noticed" and "you were told" are different facts, and
    the gap between them is the interesting one — a workspace with no bot
    linked still needs the record."""
    register_workspace(client, email="n8@example.com", workspace_name="N8")
    workspace_id = _workspace(client)

    with SessionLocal() as db:
        note = asyncio.run(raise_alert(db, workspace_id, COLLECTOR_FAILED.key, title="توقّف", body="تفاصيل"))
        assert note is not None

    listed = client.get("/notifications").json()
    assert listed["total"] == 1
    assert listed["unread"] == 1
    assert listed["items"][0]["delivered_count"] == 0
    assert listed["items"][0]["title"] == "توقّف"


def test_delivery_count_records_how_many_chats_were_reached(client: TestClient, monkeypatch):
    register_workspace(client, email="n9@example.com", workspace_name="N9")
    workspace_id = _workspace(client)

    with SessionLocal() as db:
        db.add(BotLink(chat_id="555", workspace_id=workspace_id))
        db.commit()

    sent: list[dict] = []

    class RecordingBot:
        async def send_message(self, **kwargs):
            sent.append(kwargs)

    import app.bot.telegram_bot as bot_module

    monkeypatch.setattr(bot_module, "get_bot", lambda: RecordingBot())

    with SessionLocal() as db:
        asyncio.run(raise_alert(db, workspace_id, COLLECTOR_FAILED.key, title="ت", body="ب"))

    assert len(sent) == 1
    assert client.get("/notifications").json()["items"][0]["delivered_count"] == 1


def test_marking_read_clears_the_badge_without_losing_the_record(client: TestClient):
    """The audit value of idea 161 is that acknowledging does not erase."""
    register_workspace(client, email="n10@example.com", workspace_name="N10")
    workspace_id = _workspace(client)

    with SessionLocal() as db:
        asyncio.run(raise_alert(db, workspace_id, COLLECTOR_FAILED.key, title="ت", body="ب"))

    note_id = client.get("/notifications").json()["items"][0]["id"]
    assert client.post(f"/notifications/{note_id}/read").status_code == 200

    after = client.get("/notifications").json()
    assert after["unread"] == 0
    assert after["total"] == 1
    assert after["items"][0]["read_at"] is not None


def test_read_all_clears_everything_outstanding(client: TestClient):
    register_workspace(client, email="n11@example.com", workspace_name="N11")
    workspace_id = _workspace(client)

    with SessionLocal() as db:
        for n in range(3):
            asyncio.run(raise_alert(db, workspace_id, COLLECTOR_FAILED.key, title=f"ت{n}", body="ب"))

    assert client.get("/notifications/unread-count").json()["unread"] == 3
    assert client.post("/notifications/read-all").status_code == 204
    assert client.get("/notifications/unread-count").json()["unread"] == 0
    assert client.get("/notifications").json()["total"] == 3


def test_one_workspace_never_sees_anothers_alerts(client: TestClient):
    register_workspace(client, email="n12a@example.com", workspace_name="N12A")
    victim_workspace = _workspace(client)
    with SessionLocal() as db:
        asyncio.run(raise_alert(db, victim_workspace, COLLECTOR_FAILED.key, title="سرّي", body="سرّي"))
    victim_id = client.get("/notifications").json()["items"][0]["id"]
    client.post("/auth/logout")

    register_workspace(client, email="n12b@example.com", workspace_name="N12B")

    assert client.get("/notifications").json()["total"] == 0
    assert "سرّي" not in client.get("/notifications").text
    assert client.post(f"/notifications/{victim_id}/read").status_code == 404


def test_notifications_need_a_session_not_a_key(client: TestClient):
    """An API key that could switch alerts off would be a way to silence
    the very warnings that report a compromise."""
    register_workspace(client, email="n13@example.com", workspace_name="N13")
    key = client.post("/auth/api-keys", json={"name": "k"}).json()["key"]
    client.cookies.clear()

    headers = {"Authorization": f"Bearer {key}"}
    assert client.get("/notifications", headers=headers).status_code == 403
    assert client.get("/notifications/preferences", headers=headers).status_code == 403
    assert (
        client.patch(
            f"/notifications/preferences/{COLLECTOR_FAILED.key}",
            json={"enabled": False},
            headers=headers,
        ).status_code
        == 403
    )


# --- the alert that used to bypass all of this -----------------------------


def test_the_new_device_alert_obeys_the_switch_like_every_other(client: TestClient, monkeypatch):
    """Shipped in phase 8c before preferences existed, and talked to the
    bot directly. A single caller doing that makes the exit criterion
    false, so it now goes through the same gate."""
    register_workspace(client, email="n14@example.com", workspace_name="N14")
    workspace_id = _workspace(client)

    with SessionLocal() as db:
        db.add(BotLink(chat_id="777", workspace_id=workspace_id))
        user = db.query(User).filter(User.email == "n14@example.com").one()
        for s in db.query(AuthSession).filter(AuthSession.user_id == user.id):
            s.ip_address = "203.0.113.5"
            s.user_agent = "Firefox/140"
        db.commit()

    sent: list[dict] = []

    class RecordingBot:
        async def send_message(self, **kwargs):
            sent.append(kwargs)

    import app.bot.telegram_bot as bot_module

    monkeypatch.setattr(bot_module, "get_bot", lambda: RecordingBot())

    client.patch(f"/notifications/preferences/{NEW_DEVICE.key}", json={"enabled": False})
    client.post("/auth/logout")
    client.post(
        "/auth/login",
        json={"email": "n14@example.com", "password": PASSWORD},
        headers={"X-Forwarded-For": "198.51.100.9", "User-Agent": "Safari/18"},
    )

    assert sent == [], "a disabled alert still reached a chat"


def test_is_enabled_reads_the_default_at_call_time(client: TestClient):
    """Defaults are not materialised, so a default the project later
    reconsiders applies to everyone who never expressed a choice."""
    register_workspace(client, email="n15@example.com", workspace_name="N15")
    workspace_id = _workspace(client)

    with SessionLocal() as db:
        assert is_enabled(db, workspace_id, COLLECTOR_FAILED.key) is True
        assert is_enabled(db, workspace_id, WEEKLY_DIGEST.key) is False
        assert db.query(NotificationPreference).count() == 0


@pytest.mark.parametrize("alert", ALERT_TYPES, ids=lambda a: a.key)
def test_no_alert_type_lacks_a_label_or_description(alert):
    """These are the words somebody reads while deciding whether to switch
    something off. A blank one makes that decision impossible."""
    assert alert.label.strip()
    assert alert.description.strip()
    assert alert.key.islower()
