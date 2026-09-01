"""Adding a collection account from a chat, and what stops it going wrong.

Most of this file is about refusals. The feature is defensible only
because of what it will not do — run in a group, run while switched off,
keep the code and the password in the chat history — so those are the
assertions that carry the weight, not the happy path.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from app.bot.routers import onboard_account as flow
from app.config import get_settings
from app.database import SessionLocal
from app.models import BotLink, Workspace


@pytest.fixture(autouse=True)
def clean_flow():
    flow._FLOW.clear()
    yield
    flow._FLOW.clear()


@pytest.fixture
def onboarding_on():
    os.environ["BOT_ACCOUNT_ONBOARDING"] = "true"
    get_settings.cache_clear()
    yield
    os.environ.pop("BOT_ACCOUNT_ONBOARDING", None)
    get_settings.cache_clear()


class _Chat:
    def __init__(self, chat_id, chat_type="private"):
        self.id, self.type = chat_id, chat_type


class _Msg:
    """Records replies, and whether the bot deleted the incoming message."""

    def __init__(self, chat_id, text="", chat_type="private"):
        self.chat = _Chat(chat_id, chat_type)
        self.text = text
        self.sent: list[str] = []
        self.deleted = False

    async def answer(self, text, **kwargs):
        self.sent.append(text)

    async def delete(self):
        self.deleted = True

    @property
    def all_text(self):
        return "\n".join(self.sent)


def _linked(chat_id: int) -> int:
    db = SessionLocal()
    try:
        workspace = Workspace(name="Onboard WS")
        db.add(workspace)
        db.flush()
        db.merge(BotLink(chat_id=str(chat_id), workspace_id=workspace.id))
        db.commit()
        return workspace.id
    finally:
        db.close()


def _run(handler, message, *args):
    db = SessionLocal()
    try:
        asyncio.run(handler(message, *args, db))
    finally:
        db.close()


# --- the refusals ----------------------------------------------------------


def test_the_flow_refuses_to_run_in_a_group(onboarding_on):
    """The code and the two-factor password would be readable by every
    member. Refused outright rather than warned about: a warning in a
    group is read after the secret is already posted."""
    _linked(-100500)
    message = _Msg(-100500, chat_type="supergroup")

    _run(flow.handle_add_account, message)

    assert "مجموعة" in message.all_text
    assert str(-100500) not in flow._FLOW, "a flow was opened in a group"


def test_a_chat_that_becomes_a_group_mid_flow_is_dropped(onboarding_on):
    """A linked private chat can be migrated to a supergroup by Telegram
    itself. Checking only at the start would leave the code being asked
    for in a room that was private when the question was posed."""
    _linked(4242)
    flow._FLOW["4242"] = {"step": flow.ASK_PHONE, "workspace_id": 1, "label": "x"}

    message = _Msg(4242, text="+966500000000", chat_type="supergroup")
    _run(flow.handle_flow_step, message)

    assert "مجموعة" in message.all_text
    assert "4242" not in flow._FLOW


def test_nothing_starts_while_the_feature_is_switched_off():
    _linked(555)
    message = _Msg(555)

    _run(flow.handle_add_account, message)

    assert "غير مفعّلة" in message.all_text
    assert "555" not in flow._FLOW


def test_an_unlinked_chat_cannot_start_the_flow(onboarding_on):
    message = _Msg(999)

    _run(flow.handle_add_account, message)

    assert "غير مرتبطة" in message.all_text
    assert "999" not in flow._FLOW


# --- what the flow does and does not ask for -------------------------------


def test_the_warning_says_the_credentials_that_never_travel(onboarding_on):
    """The refusal this replaced was based on api_hash and the session
    string travelling through a chat. They do not, and the user is told so
    — otherwise the feature looks more dangerous than it is and the safe
    path gets skipped for the wrong reason."""
    _linked(1001)
    message = _Msg(1001)

    _run(flow.handle_add_account, message)

    assert "api_hash" in message.all_text
    assert "لن أطلب" in message.all_text
    assert "اللوحة" in message.all_text, "it must still point at the safer path"


def test_the_code_message_is_deleted_the_moment_it_is_read(onboarding_on, monkeypatch):
    """Telegram keeps chat history. A one-time code left in it is spent,
    but the habit of leaving secrets in a chat is not."""
    workspace_id = _linked(1002)
    flow._FLOW["1002"] = {"step": flow.ASK_CODE, "workspace_id": workspace_id, "label": "x", "token": "t"}

    async def fake_verify(*a, **k):
        raise flow.account_login.LoginError("رمز غير صحيح")

    monkeypatch.setattr(flow.account_login, "verify_login", fake_verify)

    message = _Msg(1002, text="12345")
    _run(flow.handle_flow_step, message)

    assert message.deleted, "the message carrying the one-time code was left in the chat"


def test_the_two_factor_password_message_is_deleted_too(onboarding_on, monkeypatch):
    """The one genuinely durable credential in this flow."""
    workspace_id = _linked(1003)
    flow._FLOW["1003"] = {"step": flow.ASK_PASSWORD, "workspace_id": workspace_id, "label": "x", "token": "t"}

    async def fake_verify(*a, **k):
        raise flow.account_login.LoginError("خطأ")

    monkeypatch.setattr(flow.account_login, "verify_login", fake_verify)

    message = _Msg(1003, text="my-2fa-password")
    _run(flow.handle_flow_step, message)

    assert message.deleted


def test_a_failed_deletion_does_not_abandon_the_login(onboarding_on, monkeypatch):
    """Cleanup is best effort by nature — the message may be too old, or
    permissions may have changed — and a failed delete must not strand a
    half-finished login."""
    workspace_id = _linked(1004)
    flow._FLOW["1004"] = {"step": flow.ASK_CODE, "workspace_id": workspace_id, "label": "x", "token": "t"}

    class _Undeletable(_Msg):
        async def delete(self):
            raise RuntimeError("message too old")

    async def fake_verify(*a, **k):
        raise flow.account_login.NeedsPassword()

    monkeypatch.setattr(flow.account_login, "verify_login", fake_verify)

    message = _Undeletable(1004, text="12345")
    _run(flow.handle_flow_step, message)

    assert flow._FLOW["1004"]["step"] == flow.ASK_PASSWORD, "the flow was abandoned by a failed cleanup"


# --- the flow itself -------------------------------------------------------


def test_a_lost_pending_login_says_what_happened(onboarding_on, monkeypatch):
    """The pending login holds an open MTProto connection in process
    memory, so a restart between the two steps loses it. Saying that beats
    "something went wrong"."""
    workspace_id = _linked(1005)
    flow._FLOW["1005"] = {"step": flow.ASK_CODE, "workspace_id": workspace_id, "label": "x", "token": "gone"}

    async def fake_verify(*a, **k):
        raise KeyError("gone")

    monkeypatch.setattr(flow.account_login, "verify_login", fake_verify)

    message = _Msg(1005, text="12345")
    _run(flow.handle_flow_step, message)

    assert "/addaccount" in message.all_text


def test_a_message_from_a_chat_with_no_flow_is_left_alone(onboarding_on):
    """Otherwise this router's catch-all would swallow every ordinary
    message before the search router ever sees it."""
    message = _Msg(1006, text="ابحث عن هذا")

    _run(flow.handle_flow_step, message)

    assert message.sent == []


def test_cancel_discards_everything(onboarding_on):
    _linked(1007)
    flow._FLOW["1007"] = {"step": flow.ASK_PHONE, "workspace_id": 1, "label": "x"}

    message = _Msg(1007)
    _run(flow.handle_cancel, message)

    assert "1007" not in flow._FLOW
    assert "أُلغيت" in message.all_text


def test_repeated_attempts_are_throttled(onboarding_on):
    """A conversational flow makes account-adding cheap to attempt over
    and over, and each attempt asks Telegram to send a code."""
    _linked(1008)

    for _ in range(flow.MAX_ATTEMPTS + 1):
        db = SessionLocal()
        try:
            from app.security import record_action_event

            workspace_id = db.get(BotLink, "1008").workspace_id
            record_action_event(db, flow.RATE_SCOPE, str(workspace_id))
            db.commit()
        finally:
            db.close()

    message = _Msg(1008)
    _run(flow.handle_add_account, message)

    assert "مرّات كثيرة" in message.all_text
