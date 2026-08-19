"""Tests for the Telegram bot's message handlers.

These call the actual handler coroutines directly against a real database
session — the same functions aiogram's dispatcher would invoke on a real
webhook update — rather than standing up the whole aiogram machinery.
That is enough to exercise every real code path (link generation, chat
resolution, the DB queries) without a live Telegram connection, and it
covers a module that previously had zero test coverage despite writing
to the database on every command.
"""

from __future__ import annotations

import asyncio

from app.bot import telegram_bot as bot
from app.database import SessionLocal
from app.ingest import get_or_create_channel, ingest_text
from app.models import BotLink, BotLinkCode, Workspace


class FakeChat:
    def __init__(self, chat_id: int):
        self.id = chat_id


class FakeMessage:
    """Records every call to .answer() instead of hitting the Telegram API."""

    def __init__(self, chat_id: int):
        self.chat = FakeChat(chat_id)
        self.sent: list[str] = []

    async def answer(self, text: str) -> None:
        self.sent.append(text)


class FakeCommand:
    def __init__(self, args: str | None):
        self.args = args


def _workspace() -> int:
    db = SessionLocal()
    try:
        workspace = Workspace(name="Bot WS")
        db.add(workspace)
        db.commit()
        return workspace.id
    finally:
        db.close()


def test_start_without_code_shows_instructions():
    db = SessionLocal()
    try:
        message = FakeMessage(chat_id=111)
        asyncio.run(bot.handle_start(message, FakeCommand(args=None), db))
    finally:
        db.close()
    assert "رمز الربط" in message.sent[0] or "/link" in message.sent[0] or "CODE" in message.sent[0]


def test_start_with_valid_code_links_the_chat():
    workspace_id = _workspace()
    db = SessionLocal()
    try:
        code = bot.generate_link_code(db, workspace_id)
        message = FakeMessage(chat_id=222)
        asyncio.run(bot.handle_start(message, FakeCommand(args=code), db))
    finally:
        db.close()

    assert "نجاح" in message.sent[0] or "✅" in message.sent[0]
    db = SessionLocal()
    try:
        link = db.get(BotLink, "222")
        assert link is not None
        assert link.workspace_id == workspace_id
        # The code is single-use.
        record = db.query(BotLinkCode).filter(BotLinkCode.code == code).one()
        assert record.used_at is not None
    finally:
        db.close()


def test_start_with_invalid_code_is_rejected():
    db = SessionLocal()
    try:
        message = FakeMessage(chat_id=333)
        asyncio.run(bot.handle_start(message, FakeCommand(args="not-a-real-code"), db))
    finally:
        db.close()
    assert "غير صالح" in message.sent[0]


def test_start_with_already_used_code_is_rejected():
    workspace_id = _workspace()
    db = SessionLocal()
    try:
        code = bot.generate_link_code(db, workspace_id)
        asyncio.run(bot.handle_start(FakeMessage(chat_id=444), FakeCommand(args=code), db))
        # Reusing the same code from a different chat must fail.
        second = FakeMessage(chat_id=555)
        asyncio.run(bot.handle_start(second, FakeCommand(args=code), db))
    finally:
        db.close()
    assert "غير صالح" in second.sent[0]


def test_search_without_linked_chat_prompts_to_link():
    db = SessionLocal()
    try:
        message = FakeMessage(chat_id=666)
        asyncio.run(bot.handle_search(message, FakeCommand(args="anything"), db))
    finally:
        db.close()
    assert "غير مرتبطة" in message.sent[0]


def test_search_without_query_shows_usage():
    workspace_id = _workspace()
    db = SessionLocal()
    try:
        db.merge(BotLink(chat_id="777", workspace_id=workspace_id))
        db.commit()
        message = FakeMessage(chat_id=777)
        asyncio.run(bot.handle_search(message, FakeCommand(args=""), db))
    finally:
        db.close()
    assert "الاستخدام" in message.sent[0]


def test_search_finds_matching_links():
    workspace_id = _workspace()
    db = SessionLocal()
    try:
        db.merge(BotLink(chat_id="888", workspace_id=workspace_id))
        channel = get_or_create_channel(db, workspace_id=workspace_id, tg_channel_id="bot-test")
        ingest_text(
            db, workspace_id=workspace_id, channel_id=channel.id, text="https://example.com/python-guide.pdf"
        )
        db.commit()
        message = FakeMessage(chat_id=888)
        asyncio.run(bot.handle_search(message, FakeCommand(args="python"), db))
    finally:
        db.close()
    assert "python-guide" in message.sent[0]


def test_search_reports_no_results():
    workspace_id = _workspace()
    db = SessionLocal()
    try:
        db.merge(BotLink(chat_id="999", workspace_id=workspace_id))
        db.commit()
        message = FakeMessage(chat_id=999)
        asyncio.run(bot.handle_search(message, FakeCommand(args="nothing-will-match-this"), db))
    finally:
        db.close()
    assert "لا نتائج" in message.sent[0]


def test_search_is_scoped_to_the_linked_workspace():
    """A chat linked to workspace A must never see workspace B's links."""
    workspace_a = _workspace()
    workspace_b = _workspace()
    db = SessionLocal()
    try:
        db.merge(BotLink(chat_id="1001", workspace_id=workspace_a))
        channel_b = get_or_create_channel(db, workspace_id=workspace_b, tg_channel_id="other-ws")
        ingest_text(db, workspace_id=workspace_b, channel_id=channel_b.id, text="https://example.com/secret.pdf")
        db.commit()
        message = FakeMessage(chat_id=1001)
        asyncio.run(bot.handle_search(message, FakeCommand(args="secret"), db))
    finally:
        db.close()
    assert "لا نتائج" in message.sent[0]


def test_stats_without_linked_chat_prompts_to_link():
    db = SessionLocal()
    try:
        message = FakeMessage(chat_id=1111)
        asyncio.run(bot.handle_stats(message, db))
    finally:
        db.close()
    assert "غير مرتبطة" in message.sent[0]


def test_stats_reports_counts():
    workspace_id = _workspace()
    db = SessionLocal()
    try:
        db.merge(BotLink(chat_id="1212", workspace_id=workspace_id))
        channel = get_or_create_channel(db, workspace_id=workspace_id, tg_channel_id="stats-test")
        ingest_text(db, workspace_id=workspace_id, channel_id=channel.id, text="https://example.com/a.apk")
        db.commit()
        message = FakeMessage(chat_id=1212)
        asyncio.run(bot.handle_stats(message, db))
    finally:
        db.close()
    assert "1" in message.sent[0]


def test_unknown_text_shows_help():
    message = FakeMessage(chat_id=1313)
    asyncio.run(bot.handle_other(message))
    assert "/search" in message.sent[0]
    assert "/stats" in message.sent[0]


def test_get_bot_returns_none_without_token(monkeypatch):
    settings = bot.get_settings()
    monkeypatch.setattr(settings, "bot_token", None)
    assert bot.get_bot() is None


def test_get_bot_returns_a_bot_instance_when_configured(monkeypatch):
    settings = bot.get_settings()
    monkeypatch.setattr(settings, "bot_token", "123456:fake-token-for-testing")
    instance = bot.get_bot()
    assert instance is not None


def test_generate_link_code_is_unique_per_call():
    workspace_id = _workspace()
    db = SessionLocal()
    try:
        first = bot.generate_link_code(db, workspace_id)
        second = bot.generate_link_code(db, workspace_id)
        assert first != second
    finally:
        db.close()
