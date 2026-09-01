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

from contextlib import suppress

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InaccessibleMessage, Message
from sqlalchemy.orm import Session

from app.bot.routers.status import stats_text
from app.bot.shared import (
    CB_DETAILS,
    CB_FAVOURITE,
    CB_MENU,
    NOT_LINKED,
    TOO_OLD,
    answer_results,
    decode_filter,
    help_text,
    ordered,
    recall_results,
    resolve_workspace,
    result_actions,
)
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
    #
    # Resolved against the SAME filter the number was printed under. It used
    # to re-query with no filter at all, so after any search "3." on screen
    # and "/details 3" were two different links, and nothing said so.
    #
    # No page arithmetic: the printed number is already absolute within the
    # filter (page 2 prints 6–10), so the offset is the number minus one on
    # every page.
    q, favorite, category = recall_results(db, str(message.chat.id))
    query, _ = filtered_links(db, workspace_id, q=q, category=category, favorite=favorite)
    link = ordered(query).offset(int(raw) - 1).limit(1).first()
    if link is None:
        await message.answer("لا يوجد رابط بهذا الرقم.")
        return

    await message.answer(_details_text(link), disable_web_page_preview=True)


def _details_text(link: Link) -> str:
    """One rendering, shared by the /details command and the button."""
    context = (link.raw_text or "").strip().replace("\n", " ")[:200]
    return (
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
        await callback.answer(TOO_OLD, show_alert=True)
        return

    workspace_id = resolve_workspace(db, str(origin.chat.id))
    if workspace_id is None:
        await callback.answer(NOT_LINKED, show_alert=True)
        return

    # split(":", 2) then unpack into three names used to raise ValueError on
    # a two-part payload — which is exactly what this handler now sends, and
    # what an older client replaying a button could send either way.
    parts = callback.data.split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        await callback.answer(TOO_OLD, show_alert=True)
        return

    page = int(parts[1])
    if len(parts) > 2:
        # A button from before the filter moved into bot_links. Its token is
        # still the best available description of what the user is looking
        # at, so honour it rather than silently paging a different search.
        q, favorite, category = decode_filter(parts[2])
    else:
        q, favorite, category = recall_results(db, str(origin.chat.id))

    await answer_results(origin, db, workspace_id, q=q, page=page, favorite=favorite, category=category)
    # Telegram shows a loading spinner on the button until this is called.
    await callback.answer()


def _origin(callback: CallbackQuery):
    """The message a callback came from, or None if it is out of reach.

    Telegram sends InaccessibleMessage when the original is too old for the
    bot to read back. It is not a Message subclass, so answering through it
    means an API call about a message the bot cannot access — the user gets
    an explanation instead of a button that spins and then fails.

    Factored out when the second and third callback handlers needed the
    same seven lines. Written as an InaccessibleMessage rejection rather
    than a Message acceptance, so the narrowing does not also reject
    anything that merely behaves like a Message.
    """
    origin = callback.message
    if origin is None or isinstance(origin, InaccessibleMessage):
        return None
    return origin


@router.callback_query(F.data.startswith(f"{CB_DETAILS}:"))
async def handle_details_button(callback: CallbackQuery, db: Session) -> None:
    """The "تفاصيل" button under a result — so no id is ever retyped."""
    origin = _origin(callback)
    if callback.data is None or origin is None:
        await callback.answer(TOO_OLD, show_alert=True)
        return

    workspace_id = resolve_workspace(db, str(origin.chat.id))
    if workspace_id is None:
        await callback.answer(NOT_LINKED, show_alert=True)
        return

    id_text = callback.data.split(":", 1)[1]
    if not id_text.isdigit():
        # int() on client-supplied text raised ValueError out of the handler,
        # which aiogram turns into a 500 on the webhook and a delivery
        # Telegram then retries. The payload is client-supplied — the same
        # reason the query below is workspace-scoped — so it gets parsed as
        # untrusted input, not assumed well-formed.
        await callback.answer(TOO_OLD, show_alert=True)
        return

    link_id = int(id_text)
    # Scoped to the workspace, not just fetched by id: a callback payload is
    # client-supplied, and a link id from one workspace must not resolve in
    # another just because someone edited a button's data.
    link = db.query(Link).filter(Link.id == link_id, Link.workspace_id == workspace_id).first()
    if link is None:
        await callback.answer("لم يعد هذا الرابط موجوداً.", show_alert=True)
        return

    await origin.answer(_details_text(link), disable_web_page_preview=True)
    await callback.answer()


@router.callback_query(F.data.startswith(f"{CB_FAVOURITE}:"))
async def handle_favourite_button(callback: CallbackQuery, db: Session) -> None:
    """Star or unstar from the result itself.

    The payload carries the WANTED state, not "toggle": two chats acting on
    the same link would otherwise each send a toggle and cancel each other.
    """
    origin = _origin(callback)
    if callback.data is None or origin is None:
        await callback.answer(TOO_OLD, show_alert=True)
        return

    workspace_id = resolve_workspace(db, str(origin.chat.id))
    if workspace_id is None:
        await callback.answer(NOT_LINKED, show_alert=True)
        return

    # Same reasoning as the details button: a short payload used to raise
    # ValueError on the unpack, and a non-numeric id on the int().
    parts = callback.data.split(":", 2)
    if len(parts) != 3 or not parts[1].isdigit():
        await callback.answer(TOO_OLD, show_alert=True)
        return
    _, id_text, want = parts

    link = db.query(Link).filter(Link.id == int(id_text), Link.workspace_id == workspace_id).first()
    if link is None:
        await callback.answer("لم يعد هذا الرابط موجوداً.", show_alert=True)
        return

    link.is_favorite = want == "1"
    db.commit()

    # Repaint the button in place, so its label matches what just happened
    # rather than describing the state it was in before the tap.
    with suppress(TelegramBadRequest):
        await origin.edit_reply_markup(reply_markup=result_actions(link.id, bool(link.is_favorite)))
    await callback.answer("أُضيف للمفضّلة ⭐" if link.is_favorite else "أُزيل من المفضّلة")


@router.callback_query(F.data.startswith(f"{CB_MENU}:"))
async def handle_menu_button(callback: CallbackQuery, db: Session) -> None:
    """The /start menu: latest, favourites, stats, help."""
    origin = _origin(callback)
    if callback.data is None or origin is None:
        await callback.answer(TOO_OLD, show_alert=True)
        return

    workspace_id = resolve_workspace(db, str(origin.chat.id))
    if workspace_id is None:
        await callback.answer(NOT_LINKED, show_alert=True)
        return

    choice = callback.data.split(":", 1)[1]
    if choice == "latest":
        await answer_results(origin, db, workspace_id, q=None, page=0)
    elif choice == "favorite":
        await answer_results(origin, db, workspace_id, q=None, page=0, favorite=True)
    elif choice == "stats":
        await origin.answer(stats_text(db, workspace_id))
    else:
        await origin.answer(help_text(True))
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
