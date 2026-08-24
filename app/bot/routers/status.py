"""Read-only summaries: how much was collected, from where, how much is alive.

Nothing here writes anything. That is the shared property that puts these
three commands together rather than the fact that each returns a number.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.bot.shared import NOT_LINKED, resolve_workspace
from app.models import Channel, Link

router = Router(name="status")


@router.message(Command("stats"))
async def handle_stats(message: Message, db: Session) -> None:
    workspace_id = resolve_workspace(db, str(message.chat.id))
    if workspace_id is None:
        await message.answer(NOT_LINKED)
        return

    await message.answer(stats_text(db, workspace_id))


def stats_text(db: Session, workspace_id: int) -> str:
    """Shared by /stats and the menu's stats button.

    A second copy for the button is how the two would eventually report
    different numbers for the same workspace.
    """
    total_links = db.query(Link).filter(Link.workspace_id == workspace_id).count()
    total_channels = db.query(Channel).filter(Channel.workspace_id == workspace_id).count()
    return f"عدد الروابط: {total_links}\nعدد القنوات: {total_channels}"


@router.message(Command("channels"))
async def handle_channels(message: Message, db: Session) -> None:
    workspace_id = resolve_workspace(db, str(message.chat.id))
    if workspace_id is None:
        await message.answer(NOT_LINKED)
        return

    channels = db.query(Channel).filter(Channel.workspace_id == workspace_id).order_by(Channel.id).limit(30).all()
    if not channels:
        await message.answer("لا قنوات مضافة بعد.")
        return

    lines = [f"• {c.username or c.tg_channel_id}{'' if c.is_active else ' (معطّلة)'}" for c in channels]
    await message.answer("القنوات:\n" + "\n".join(lines))


@router.message(Command("vitality"))
async def handle_vitality(message: Message, db: Session) -> None:
    workspace_id = resolve_workspace(db, str(message.chat.id))
    if workspace_id is None:
        await message.answer(NOT_LINKED)
        return

    rows = (
        db.query(Link.is_alive, func.count(Link.id))
        .filter(Link.workspace_id == workspace_id)
        .group_by(Link.is_alive)
        .all()
    )
    counts = {row[0]: row[1] for row in rows}
    await message.answer(
        f"🟢 حيّة: {counts.get(True, 0)}\n🔴 ميتة: {counts.get(False, 0)}\n⚪ لم تُفحص: {counts.get(None, 0)}"
    )
