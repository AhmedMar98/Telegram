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
from aiogram.types import (
    CallbackQuery,
    InaccessibleMessage,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.classifier import CATEGORIES, extract_urls
from app.config import get_settings
from app.database import SessionLocal
from app.ingest import ingest_text, manual_channel
from app.linkquery import filtered_links
from app.models import BotLink, BotLinkCode, Channel, Link
from app.notify import report_adult_links
from app.timeutil import utcnow
from app.vitality import status_category

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


PAGE_SIZE = 5

# The one place that says "you are not linked yet", so the wording cannot
# drift between commands.
NOT_LINKED = "هذه المحادثة غير مرتبطة بمساحة عمل بعد. أرسل /start ثم رمز الربط."


def _format_link(link: Link, index: int) -> str:
    """One result line. The index is the number /details takes."""
    marks = []
    if link.is_favorite:
        marks.append("★")
    if link.is_alive is False:
        marks.append("💀")
    suffix = (" " + "".join(marks)) if marks else ""
    return f"{index}. [{link.category}] {link.url}{suffix}"


async def _answer_results(
    message: Message,
    db: Session,
    workspace_id: int,
    *,
    q: str | None,
    page: int,
    favorite: bool | None = None,
    category: str | None = None,
) -> None:
    """Render one page of results, with buttons only when there is a next page.

    Goes through ``filtered_links`` — the same builder the web API uses —
    so a search typed in Telegram and the same search typed in the browser
    cannot disagree.
    """
    query, _ = filtered_links(db, workspace_id, q=q, category=category, favorite=favorite)
    total = query.count()
    if not total:
        await message.answer("لا نتائج.")
        return

    rows = query.order_by(Link.created_at.desc()).offset(page * PAGE_SIZE).limit(PAGE_SIZE).all()
    start = page * PAGE_SIZE
    lines = [_format_link(link, start + i + 1) for i, link in enumerate(rows)]
    header = f"النتائج {start + 1}–{start + len(rows)} من {total}"
    body = header + "\n" + "\n".join(lines)

    await message.answer(body, reply_markup=_pager(q, page, total, favorite, category))


def _pager(q: str | None, page: int, total: int, favorite: bool | None, category: str | None):
    """Previous/next buttons, omitted entirely when there is one page.

    The callback payload carries the whole filter, because a Telegram
    callback arrives with no memory of what produced it and the bot keeps
    no per-chat state — state on a free web service that sleeps would be
    lost between messages anyway.
    """
    buttons = []
    token = _encode_filter(q, favorite, category)
    if page > 0:
        buttons.append(InlineKeyboardButton(text="« السابق", callback_data=f"pg:{page - 1}:{token}"))
    if (page + 1) * PAGE_SIZE < total:
        buttons.append(InlineKeyboardButton(text="التالي »", callback_data=f"pg:{page + 1}:{token}"))
    if not buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def _encode_filter(q: str | None, favorite: bool | None, category: str | None) -> str:
    # Telegram caps callback_data at 64 bytes, so the query is truncated
    # rather than silently producing an update Telegram refuses to send.
    parts = [(q or "")[:30], "1" if favorite else "", category or ""]
    return "|".join(parts)


def _decode_filter(token: str) -> tuple[str | None, bool | None, str | None]:
    parts = (token.split("|") + ["", "", ""])[:3]
    return (parts[0] or None), (True if parts[1] == "1" else None), (parts[2] or None)


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
        await message.answer(NOT_LINKED)
        return

    query = (command.args or "").strip()
    if not query:
        await message.answer("الاستخدام: /search كلمة البحث")
        return

    await _answer_results(message, db, workspace_id, q=query, page=0)


@dispatcher.message(Command("stats"))
async def handle_stats(message: Message, db: Session) -> None:
    workspace_id = _resolve_workspace(db, str(message.chat.id))
    if workspace_id is None:
        await message.answer(NOT_LINKED)
        return

    total_links = db.query(Link).filter(Link.workspace_id == workspace_id).count()
    total_channels = db.query(Channel).filter(Channel.workspace_id == workspace_id).count()
    await message.answer(f"عدد الروابط: {total_links}\nعدد القنوات: {total_channels}")


@dispatcher.message(Command("latest"))
async def handle_latest(message: Message, db: Session) -> None:
    workspace_id = _resolve_workspace(db, str(message.chat.id))
    if workspace_id is None:
        await message.answer(NOT_LINKED)
        return
    await _answer_results(message, db, workspace_id, q=None, page=0)


@dispatcher.message(Command("favorite"))
async def handle_favorite(message: Message, command: CommandObject, db: Session) -> None:
    workspace_id = _resolve_workspace(db, str(message.chat.id))
    if workspace_id is None:
        await message.answer(NOT_LINKED)
        return
    await _answer_results(message, db, workspace_id, q=(command.args or "").strip() or None, page=0, favorite=True)


@dispatcher.message(Command("channels"))
async def handle_channels(message: Message, db: Session) -> None:
    workspace_id = _resolve_workspace(db, str(message.chat.id))
    if workspace_id is None:
        await message.answer(NOT_LINKED)
        return

    channels = db.query(Channel).filter(Channel.workspace_id == workspace_id).order_by(Channel.id).limit(30).all()
    if not channels:
        await message.answer("لا قنوات مضافة بعد.")
        return

    lines = [f"• {c.username or c.tg_channel_id}{'' if c.is_active else ' (معطّلة)'}" for c in channels]
    await message.answer("القنوات:\n" + "\n".join(lines))


@dispatcher.message(Command("vitality"))
async def handle_vitality(message: Message, db: Session) -> None:
    workspace_id = _resolve_workspace(db, str(message.chat.id))
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


@dispatcher.message(Command("details"))
async def handle_details(message: Message, command: CommandObject, db: Session) -> None:
    """Full record for one link, addressed by its position in the last list."""
    workspace_id = _resolve_workspace(db, str(message.chat.id))
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


@dispatcher.message(Command("unlink"))
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


@dispatcher.message(Command("help"))
async def handle_help(message: Message, db: Session) -> None:
    linked = _resolve_workspace(db, str(message.chat.id)) is not None
    await message.answer(_help_text(linked))


@dispatcher.callback_query(F.data.startswith("pg:"))
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

    workspace_id = _resolve_workspace(db, str(origin.chat.id))
    if workspace_id is None:
        await callback.answer(NOT_LINKED, show_alert=True)
        return

    _, page_text, token = callback.data.split(":", 2)
    q, favorite, category = _decode_filter(token)
    await _answer_results(origin, db, workspace_id, q=q, page=int(page_text), favorite=favorite, category=category)
    # Telegram shows a loading spinner on the button until this is called.
    await callback.answer()


@dispatcher.message(F.text)
async def handle_other(message: Message, db: Session) -> None:
    """Anything that is not a command.

    Two useful behaviours instead of one error message: a pasted link is
    treated as "save this", and a bare phrase is treated as a search. Both
    are what a person actually does when they open a chat with a bot.
    """
    workspace_id = _resolve_workspace(db, str(message.chat.id))
    text = (message.text or "").strip()

    if workspace_id is None:
        await message.answer(_help_text(linked=False))
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
        await _answer_results(message, db, workspace_id, q=text, page=0)
        return

    await message.answer(_help_text(linked=True))


def _help_text(linked: bool) -> str:
    """Built from one list so a command added below cannot go undocumented."""
    if not linked:
        return (
            "أهلاً 👋\n"
            "لربط هذه المحادثة بمساحة عملك:\n"
            "١. افتح لوحة التحكم على الموقع.\n"
            "٢. من قسم «البوت» اضغط «توليد رمز ربط».\n"
            "٣. أرسل هنا: /start ثم الرمز.\n\n"
            "بعد الربط أرسل /help لعرض كل الأوامر."
        )
    lines = [f"{name} — {description}" for name, description in COMMANDS]
    return (
        "الأوامر:\n"
        + "\n".join(lines)
        + "\n\nيمكنك أيضاً إرسال رابط مباشرة لحفظه، أو كلمة مجرّدة للبحث بها."
        + "\nالتصنيفات: "
        + ", ".join(CATEGORIES)
    )


# Single source for /help and for BotFather's /setcommands list. A command
# handler added without a line here shows up in neither, which is the kind
# of drift that leaves a feature undiscoverable.
COMMANDS: tuple[tuple[str, str], ...] = (
    ("/search", "ابحث في روابطك — يدعم استبعاد كلمة بـ«-كلمة»"),
    ("/latest", "آخر الروابط المضافة"),
    ("/favorite", "ابحث ضمن المفضّلة فقط"),
    ("/details", "تفاصيل رابط برقمه في القائمة، مثل: /details 3"),
    ("/stats", "ملخّص عدد الروابط والقنوات"),
    ("/vitality", "كم رابطاً حيّ وكم ميت"),
    ("/channels", "القنوات المتابَعة"),
    ("/unlink", "إلغاء ربط هذه المحادثة"),
    ("/help", "هذه القائمة"),
)
