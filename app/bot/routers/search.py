"""Finding links, reading one in full, and saving new ones.

The free-text handler lives here rather than in its own router for a
reason worth stating: a bare phrase *is* a search, and a pasted link is
the shortest possible save. Both are the search surface as a person
actually uses it, so separating them would split one behaviour across two
files to satisfy a taxonomy nobody reads.

This router registers the catch-all ``F.text`` handler, so it must be
included **last** — aiogram tries routers in registration order, and a
catch-all registered first would swallow every command behind it. That
ordering is asserted in tests/test_bot_routers.py rather than left to a
comment.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InaccessibleMessage, Message
from sqlalchemy.orm import Session

from app.bot.shared import NOT_LINKED, answer_results, decode_filter, help_text, resolve_workspace
from app.classifier import extract_urls
from app.ingest import ingest_text, manual_channel
from app.linkquery import filtered_links
from app.models import Link
from app.notify import report_adult_links
from app.vitality import status_category

router = Router(name="search")


@router.message(Command("search"))
async def handle_search(message: Message, command: CommandObject, db: Session) -> None:
    workspace_id = resolve_workspace(db, str(message.chat.id))
    if workspace_id is None:
        await message.answer(NOT_LINKED)
        return

    query = (command.args or "").strip()
    if not query:
        await message.answer("الاستخدام: /search كلمة البحث")
        return

    await answer_results(message, db, workspace_id, q=query, page=0)


@router.message(Command("latest"))
async def handle_latest(message: Message, db: Session) -> None:
    workspace_id = resolve_workspace(db, str(message.chat.id))
    if workspace_id is None:
        await message.answer(NOT_LINKED)
        return
    await answer_results(message, db, workspace_id, q=None, page=0)


@router.message(Command("favorite"))
async def handle_favorite(message: Message, command: CommandObject, db: Session) -> None:
    workspace_id = resolve_workspace(db, str(message.chat.id))
    if workspace_id is None:
        await message.answer(NOT_LINKED)
        return
    await answer_results(message, db, workspace_id, q=(command.args or "").strip() or None, page=0, favorite=True)


@router.message(Command("details"))
async def handle_details(message: Message, command: CommandObject, db: Session) -> None:
    """Full record for one link, addressed by its position in the last list."""
    workspace_id = resolve_workspace(db, str(message.chat.id))
    if workspace_id is None:
        await message.answer(NOT_LINKED)
        return

    raw = (command.args or "").strip()
    if not raw.isdigit() or int(raw) < 1:
        await message.answer("الاستخدام: /details 3 — الرقم كما يظهر في قائمة النتائج.")
        return

    # Positional, not a database id: the number the user sees in a result
    # list is its position. Exposing raw ids would also let one chat probe
    # for another workspace's rows by counting.
    query, _ = filtered_links(db, workspace_id, q=None, category=None)
    link = query.order_by(Link.created_at.desc()).offset(int(raw) - 1).limit(1).first()
    if link is None:
        await message.answer("لا يوجد رابط بهذا الرقم.")
        return

    context = (link.raw_text or "").strip().replace("\n", " ")[:200]
    await message.answer(
        f"{link.url}\n"
        f"التصنيف: {link.category} ({link.classified_by}, {link.confidence * 100:.0f}%)\n"
        f"القاعدة: {link.matched_rule or 'غير مسجّلة'}\n"
        f"الحالة: {status_category(link.http_status, link.is_alive)}\n"
        f"النطاق: {link.domain}\n" + (f"السياق: {context}" if context else "")
    )


@router.callback_query(F.data.startswith("pg:"))
async def handle_page(callback: CallbackQuery, db: Session) -> None:
    """Next/previous on a result list."""
    # callback.message is Message | InaccessibleMessage | None. Telegram
    # sends InaccessibleMessage when the original message is too old for
    # the bot to read back. It is not a Message subclass, and answering
    # through it means an API call about a message the bot cannot access —
    # so the user gets an explanation instead of a button that spins and
    # then fails. Written as an InaccessibleMessage rejection rather than a
    # Message acceptance so the narrowing does not also reject anything
    # that merely behaves like a Message.
    origin = callback.message
    if callback.data is None or origin is None or isinstance(origin, InaccessibleMessage):
        await callback.answer("هذه الرسالة أقدم من أن يصل إليها البوت. أعد البحث من جديد.", show_alert=True)
        return

    workspace_id = resolve_workspace(db, str(origin.chat.id))
    if workspace_id is None:
        await callback.answer(NOT_LINKED, show_alert=True)
        return

    _, page_text, token = callback.data.split(":", 2)
    q, favorite, category = decode_filter(token)
    await answer_results(origin, db, workspace_id, q=q, page=int(page_text), favorite=favorite, category=category)
    # Telegram shows a loading spinner on the button until this is called.
    await callback.answer()


@router.message(F.text)
async def handle_other(message: Message, db: Session) -> None:
    """Anything that is not a command.

    Two useful behaviours instead of one error message: a pasted link is
    treated as "save this", and a bare phrase is treated as a search. Both
    are what a person actually does when they open a chat with a bot.
    """
    workspace_id = resolve_workspace(db, str(message.chat.id))
    text = (message.text or "").strip()

    if workspace_id is None:
        await message.answer(help_text(linked=False))
        return

    urls = extract_urls(text)
    if urls:
        channel = manual_channel(db, workspace_id)
        summary = ingest_text(db, workspace_id=workspace_id, channel_id=channel.id, text=text)
        db.commit()
        await message.answer(
            f"وُجد {summary.total_found} رابط — أُضيف {summary.stored} جديد، و{summary.duplicates} موجود مسبقاً."
        )
        # Redundant-looking on this path — the person is right here and just
        # sent the link — but the alert is what makes the *record* exist in
        # the notification centre, and a path that skipped it would make
        # "every adult link is reported" false depending on how it arrived.
        await report_adult_links(db, workspace_id, summary.adult_urls)
        return

    if text and not text.startswith("/"):
        await answer_results(message, db, workspace_id, q=text, page=0)
        return

    await message.answer(help_text(linked=True))
