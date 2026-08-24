"""Linking a chat to a workspace, and unlinking it again.

Everything here is about the *relationship* between a Telegram chat and a
workspace — establishing it, describing it, ending it. Nothing here reads
or writes a link.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.orm import Session

from app.bot.shared import help_text, main_menu, resolve_workspace
from app.models import BotLink, BotLinkCode
from app.timeutil import utcnow

router = Router(name="onboarding")


@router.message(Command("start"))
async def handle_start(message: Message, command: CommandObject, db: Session) -> None:
    code = (command.args or "").strip()
    if not code:
        # A bare /start from an already-linked chat is someone opening the
        # bot, not someone failing to paste a code. Give them the menu.
        if resolve_workspace(db, str(message.chat.id)) is not None:
            await message.answer("أهلاً 👋 اختر ما تريد:", reply_markup=main_menu())
        else:
            await message.answer(help_text(False))
        return

    record = db.query(BotLinkCode).filter(BotLinkCode.code == code, BotLinkCode.used_at.is_(None)).first()
    if record is None:
        await message.answer("رمز غير صالح أو مستخدم من قبل.")
        return

    record.used_at = utcnow()
    db.merge(BotLink(chat_id=str(message.chat.id), workspace_id=record.workspace_id))
    db.commit()
    await message.answer(
        "تم الربط بنجاح ✅\nاختر ما تريد، أو أرسل أي كلمة للبحث بها:",
        reply_markup=main_menu(),
    )


@router.message(Command("unlink"))
async def handle_unlink(message: Message, db: Session) -> None:
    """Detach this chat. Deliberately does not touch the workspace itself —
    unlinking a chat is not a request to delete anyone's data."""
    link = db.get(BotLink, str(message.chat.id))
    if link is None:
        await message.answer("هذه المحادثة غير مرتبطة أصلاً.")
        return
    db.delete(link)
    db.commit()
    await message.answer("أُلغي الربط. لم يُحذف أي رابط أو بيانات — فقط هذه المحادثة.")


@router.message(Command("help"))
async def handle_help(message: Message, db: Session) -> None:
    linked = resolve_workspace(db, str(message.chat.id)) is not None
    await message.answer(help_text(linked))
