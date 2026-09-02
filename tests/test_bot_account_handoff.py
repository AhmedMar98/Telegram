"""The bot points at the dashboard; it never asks for a credential.

This file replaces ``test_bot_account_onboarding.py``, which tested a flow
that has been withdrawn. That flow asked, in a Telegram chat, for a phone
number, then the login code, then the two-factor password, and defended
itself with private-chat-only enforcement plus deletion of each sensitive
message. §45 records why the defence was insufficient: a delete removes the
bot's copy, not the copy already synced to every linked device, not a
lock-screen notification preview, and not a forward made before the delete
landed — and Telegram's Bot Platform terms name asking for a password or
login code as a prohibited use.

The tests below pin the *absence* of that capability, which is the part a
future change could quietly undo.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

from app.bot.routers import onboard_account as handoff
from app.database import SessionLocal
from app.models import BotLink, Workspace


class _Message:
    """Enough of an aiogram Message to drive the handler, and a recorder."""

    def __init__(self, chat_id: int, text: str = "/addaccount"):
        self.chat = SimpleNamespace(id=chat_id, type="private")
        self.text = text
        self.replies: list[str] = []
        self.deleted = False

    async def answer(self, text: str, **kwargs: object) -> None:
        self.replies.append(text)

    async def delete(self) -> None:  # pragma: no cover - must never be needed
        self.deleted = True


def _linked_chat(chat_id: int) -> int:
    db = SessionLocal()
    try:
        workspace = Workspace(name=f"WS-{chat_id}")
        db.add(workspace)
        db.flush()
        db.add(BotLink(chat_id=str(chat_id), workspace_id=workspace.id))
        db.commit()
        return workspace.id
    finally:
        db.close()


def _run(message: _Message) -> None:
    db = SessionLocal()
    try:
        asyncio.run(handoff.handle_add_account(message, db))
    finally:
        db.close()


def test_the_command_answers_with_a_handoff_not_a_question():
    _linked_chat(7001)
    message = _Message(7001)

    _run(message)

    assert len(message.replies) == 1
    reply = message.replies[0]
    assert "لوحة التحكّم" in reply, "it must name where the job is actually done"
    assert "؟" not in reply.split("\n")[0], "the first line answers, it does not ask"


def test_the_bot_never_asks_for_a_code_or_a_password():
    """The invariant this file exists for.

    Sabotage: reintroduce any prompt that collects a login code or a
    two-factor password and this fails — the module must contain no
    handler that reads a credential out of a message.
    """
    source = inspect.getsource(handoff)
    for forbidden in ("account_login", "start_login", "verify_login", "ASK_CODE", "ASK_PASSWORD", "_FLOW"):
        assert forbidden not in source, f"the withdrawn flow is back: {forbidden}"

    handlers = [name for name, obj in vars(handoff).items() if inspect.iscoroutinefunction(obj)]
    assert handlers == ["handle_add_account"], f"an extra handler appeared: {handlers}"


def test_no_message_needs_deleting_because_none_is_sensitive():
    """The old flow deleted every incoming credential message. Nothing here
    reads one, so nothing needs erasing — and that is the improvement: the
    safest handling of a secret is never receiving it."""
    _linked_chat(7002)
    message = _Message(7002)

    _run(message)

    assert message.deleted is False
    assert "delete" not in inspect.getsource(handoff.handle_add_account)


def test_an_unlinked_chat_is_told_to_link_first():
    """A chat that has not proved which workspace it speaks for gets the
    linking instructions, not advice about a dashboard it may not reach."""
    message = _Message(7003)

    _run(message)

    assert len(message.replies) == 1
    assert "لوحة التحكّم ← الجمع" not in message.replies[0]


def test_the_handoff_carries_the_real_address_when_the_deployment_knows_it(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.onrender.com")
    get_settings.cache_clear()
    try:
        assert "https://example.onrender.com/dashboard" in handoff._dashboard_hint()
    finally:
        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
        get_settings.cache_clear()


def test_the_web_flow_that_replaces_it_still_exists():
    """A retraction that leaves no path is a removal, not a redirection.

    Read off the OpenAPI document rather than ``app.routes``: this FastAPI
    version keeps an included router as a single wrapper object with no
    ``path``, so walking ``app.routes`` finds four framework routes and
    concludes the entire API is missing.
    """
    from app.main import app

    paths = app.openapi()["paths"]
    assert "/channels/accounts/login/start" in paths
    assert "/channels/accounts/login/verify" in paths
