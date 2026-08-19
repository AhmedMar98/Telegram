"""Telegram Bot API integration, webhook mode only.

Deliberately **not** long-polling: a polling bot needs a permanently
running process, which is exactly the paid "background worker" tier
Render does not offer for free (see docs/01-critical-analysis.md,
Appendix C). A webhook, by contrast, is just one more route on the same
free web service that already serves the search UI — Telegram pushes
updates to us instead of us pulling them, so there is no second process
and no extra cost. The tradeoff, accepted explicitly: the first webhook
call after 15 minutes of idle traffic pays Render's free-tier cold-start
delay (~1 minute).
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, TelegramObject
from sqlalchemy.orm import Session

from app.classifier import CATEGORIES
from app.config import get_settings
from app.database import SessionLocal
from app.models import BotLink, BotLinkCode, Channel, Link
from app.timeutil import utcnow

dispatcher = Dispatcher()


class DbSessionMiddleware(BaseMiddleware):
    """Opens one SQLAlchemy session per Telegram update and closes it after."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        db = SessionLocal()
        try:
            data["db"] = db
            return await handler(event, data)
        finally:
            db.close()


dispatcher.update.middleware(DbSessionMiddleware())


def get_bot() -> Bot | None:
    settings = get_settings()
    if not settings.bot_token:
        return None
    return Bot(token=settings.bot_token)


def generate_link_code(db: Session, workspace_id: int) -> str:
    code = secrets.token_hex(4)
    db.add(BotLinkCode(workspace_id=workspace_id, code=code))
    db.commit()
    return code


def _resolve_workspace(db: Session, chat_id: str) -> int | None:
    link = db.get(BotLink, str(chat_id))
    return link.workspace_id if link else None


@dispatcher.message(Command("start"))
async def handle_start(message: Message, command: CommandObject, db: Session) -> None:
    code = (command.args or "").strip()
    if not code:
        await message.answer("أهلاً! أرسل: /link CODE — احصل على الرمز من صفحة الإعدادات في الموقع.")
        return

    record = db.query(BotLinkCode).filter(BotLinkCode.code == code, BotLinkCode.used_at.is_(None)).first()
    if record is None:
        await message.answer("رمز غير صالح أو مستخدم من قبل.")
        return

    record.used_at = utcnow()
    db.merge(BotLink(chat_id=str(message.chat.id), workspace_id=record.workspace_id))
    db.commit()
    await message.answer("تم الربط بنجاح ✅ يمكنك الآن استخدام /search و /stats.")


@dispatcher.message(Command("search"))
async def handle_search(message: Message, command: CommandObject, db: Session) -> None:
    workspace_id = _resolve_workspace(db, str(message.chat.id))
    if workspace_id is None:
        await message.answer("هذه المحادثة غير مرتبطة بمساحة عمل بعد. أرسل /start ثم رمز الربط.")
        return

    query = (command.args or "").strip()
    if not query:
        await message.answer("الاستخدام: /search كلمة البحث")
        return

    like = f"%{query}%"
    results = (
        db.query(Link)
        .filter(Link.workspace_id == workspace_id)
        .filter((Link.url.ilike(like)) | (Link.raw_text.ilike(like)))
        .order_by(Link.created_at.desc())
        .limit(5)
        .all()
    )
    if not results:
        await message.answer("لا نتائج.")
        return

    lines = [f"• [{link.category}] {link.url}" for link in results]
    await message.answer("\n".join(lines))


@dispatcher.message(Command("stats"))
async def handle_stats(message: Message, db: Session) -> None:
    workspace_id = _resolve_workspace(db, str(message.chat.id))
    if workspace_id is None:
        await message.answer("هذه المحادثة غير مرتبطة بمساحة عمل بعد. أرسل /start ثم رمز الربط.")
        return

    total_links = db.query(Link).filter(Link.workspace_id == workspace_id).count()
    total_channels = db.query(Channel).filter(Channel.workspace_id == workspace_id).count()
    await message.answer(f"عدد الروابط: {total_links}\nعدد القنوات: {total_channels}")


@dispatcher.message(F.text)
async def handle_other(message: Message) -> None:
    await message.answer(
        "الأوامر المتاحة: /start CODE, /search الكلمة, /stats\nالتصنيفات: " + ", ".join(CATEGORIES)
    )
