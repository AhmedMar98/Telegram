"""Linking a chat to a workspace, and unlinking it again.

Everything here is about the *relationship* between a Telegram chat and a
workspace — establishing it, describing it, ending it. Nothing here reads
or writes a link.
"""

from __future__ import annotations

from datetime import timedelta

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.orm import Session

from app.bot.shared import help_text, main_menu, resolve_workspace
from app.models import BotLink, BotLinkCode
from app.timeutil import utcnow

router = Router(name="onboarding")

# How long a link code stays usable.
#
# It used to be forever: the only condition was ``used_at IS NULL``. A code
# is a bearer credential — whoever sends it to the bot gets read access to
# the whole workspace's links — and it is generated on a dashboard, copied
# through a clipboard, and often pasted into a chat where it stays visible
# in the history. An unused one from last year still worked.
#
# Fifteen minutes is the span between generating a code and sending it,
# which is the only thing it exists for. Regenerating is one click.
LINK_CODE_TTL = timedelta(minutes=15)


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

    record = (
        db.query(BotLinkCode)
        .filter(
            BotLinkCode.code == code,
            BotLinkCode.used_at.is_(None),
            BotLinkCode.created_at >= utcnow() - LINK_CODE_TTL,
        )
        .first()
    )
    if record is None:
        # One message for invalid, used and expired alike. Distinguishing
        # them would tell someone trying codes which guesses were once real.
        await message.answer("رمز غير صالح أو منتهي الصلاحية. ولّد رمزاً جديداً من اللوحة.")
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
