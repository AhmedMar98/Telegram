"""What every bot router needs, in one place.

The bot's handlers are split across ``app/bot/routers/`` by responsibility.
Everything they *share* lives here rather than in any one of them, for a
specific reason: a helper that lives in the router that happened to need
it first becomes an import from "the search router" inside the status
router, and the split stops meaning anything.

Nothing here talks to Telegram. These are formatting, filter encoding and
workspace resolution — pure functions plus two database reads — so a
router can be read without chasing helpers through three files.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.orm import Session

from app.classifier import CATEGORIES
from app.linkquery import filtered_links
from app.models import BotLink, Link
from app.rls import scope_session_to_workspace

PAGE_SIZE = 5

# The one place that says "you are not linked yet", so the wording cannot
# drift between commands.
NOT_LINKED = "هذه المحادثة غير مرتبطة بمساحة عمل بعد. أرسل /start ثم رمز الربط."

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


def resolve_workspace(db: Session, chat_id: str) -> int | None:
    """Which workspace this chat is linked to, and bind the session to it.

    ``bot_links`` is deliberately outside row-level security — it is read
    to *establish* the tenant, so a policy on it would mean the bot could
    never answer anybody (see app/rls.py). Everything the handlers touch
    afterwards is inside it, though: ``/channels``, ``/stats`` and
    ``/vitality`` all read ``channels``.

    So this is the bot's equivalent of ``app/deps.py``'s scoping step, and
    it belongs here rather than in each handler for the same reason: it is
    the one point where the chat's identity becomes known, and a handler
    that forgot would read zero channels and answer "you have none" to a
    workspace that has twenty.
    """
    link = db.get(BotLink, str(chat_id))
    if link is None:
        return None
    scope_session_to_workspace(db, link.workspace_id)
    return link.workspace_id


def format_link(link: Link, index: int) -> str:
    """One result line. The index is the number /details takes."""
    marks = []
    if link.is_favorite:
        marks.append("★")
    if link.is_alive is False:
        marks.append("💀")
    suffix = (" " + "".join(marks)) if marks else ""
    return f"{index}. [{link.category}] {link.url}{suffix}"


def encode_filter(q: str | None, favorite: bool | None, category: str | None) -> str:
    # Telegram caps callback_data at 64 bytes, so the query is truncated
    # rather than silently producing an update Telegram refuses to send.
    parts = [(q or "")[:30], "1" if favorite else "", category or ""]
    return "|".join(parts)


def decode_filter(token: str) -> tuple[str | None, bool | None, str | None]:
    parts = (token.split("|") + ["", "", ""])[:3]
    return (parts[0] or None), (True if parts[1] == "1" else None), (parts[2] or None)


# Every callback payload this bot sends. Telegram caps callback_data at 64
# BYTES, and Arabic is 2 bytes per character in UTF-8, so the prefixes are
# terse on purpose — a truncated payload is a button that silently does
# nothing.
CB_PAGE = "pg"
CB_DETAILS = "d"
CB_FAVOURITE = "f"
CB_MENU = "m"

# Telegram stops letting a bot read back a message once it is old enough,
# and every callback handler has to say so. One string, so three buttons
# cannot drift into three different explanations of the same thing.
TOO_OLD = "هذه الرسالة أقدم من أن يصل إليها البوت. أعد البحث من جديد."


def main_menu() -> InlineKeyboardMarkup:
    """The four things people actually do, as buttons.

    The bot has ten commands and, before this, two buttons in the entire
    system — previous and next. Everything else had to be typed from
    memory, including `/details 42`, where the 42 is only visible if you
    still have the search results on screen.

    That is not a hypothetical complaint about ergonomics. The linking
    flow asked for `/start <code>`: a slash, a space, and eight hex
    characters, rendered right-to-left so the slash appears on the far
    side. A real user sent eight variants of that line and linked nothing.
    Typing is the failure mode; buttons are the fix.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🆕 الأحدث", callback_data=f"{CB_MENU}:latest"),
                InlineKeyboardButton(text="⭐ المفضّلة", callback_data=f"{CB_MENU}:favorite"),
            ],
            [
                InlineKeyboardButton(text="📊 إحصاءات", callback_data=f"{CB_MENU}:stats"),
                InlineKeyboardButton(text="❓ مساعدة", callback_data=f"{CB_MENU}:help"),
            ],
        ]
    )


def result_actions(link_id: int, is_favorite: bool) -> InlineKeyboardMarkup:
    """Per-result buttons, so no id is ever read off a screen and retyped.

    The star's label reflects the CURRENT state and the callback carries
    the INTENT, not a toggle: two people acting on the same link from two
    chats would both send "toggle" and cancel each other out.
    """
    star = "☆ إزالة من المفضّلة" if is_favorite else "⭐ إضافة للمفضّلة"
    want = "0" if is_favorite else "1"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔍 تفاصيل", callback_data=f"{CB_DETAILS}:{link_id}"),
                InlineKeyboardButton(text=star, callback_data=f"{CB_FAVOURITE}:{link_id}:{want}"),
            ]
        ]
    )


def pager(q: str | None, page: int, total: int, favorite: bool | None, category: str | None):
    """Previous/next buttons, omitted entirely when there is one page.

    The payload carries the page number and nothing else. It used to carry
    the whole filter, because a Telegram callback arrives with no memory of
    what produced it — but ``callback_data`` is capped at 64 BYTES, so the
    search term was truncated to 30 characters to fit. Page 2 of a longer
    search was a page of a *different* search, and it failed silently.

    The filter now lives on the chat's ``bot_links`` row, where its length
    is not a wire-format problem. The filter arguments stay in the
    signature because they still decide whether a next page exists.
    """
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton(text="« السابق", callback_data=f"{CB_PAGE}:{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        buttons.append(InlineKeyboardButton(text="التالي »", callback_data=f"{CB_PAGE}:{page + 1}"))
    if not buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def remember_results(db: Session, chat_id: str, *, q: str | None, favorite, category) -> None:
    """Record what this chat is currently looking at.

    Called from one place — ``answer_results`` — so a new entry point into
    the result list cannot forget to update the context and leave
    ``/details`` answering about the previous search.
    """
    link = db.get(BotLink, str(chat_id))
    if link is None:
        return
    # Truncated to the column, not to a wire format: a 255-character search
    # term is already far past what anyone types, and unlike the old
    # 30-byte callback limit this cannot cut a realistic query in half.
    link.last_query = (q or None) and q[:255]
    link.last_category = category
    link.last_favorite = favorite
    db.commit()


def recall_results(db: Session, chat_id: str) -> tuple[str | None, bool | None, str | None]:
    """The filter this chat was last shown: query, favourite flag, category.

    Not the page. The number printed beside a result is absolute within the
    filter — page 2 prints 6 to 10 — so the position needs no page to
    resolve, and storing one would be a second source of truth for the same
    thing.

    A chat that has not searched yet gets the unfiltered list, which is
    what ``/latest`` shows, so ``/details 1`` right after linking still
    means "the newest link".
    """
    link = db.get(BotLink, str(chat_id))
    if link is None:
        return None, None, None
    return link.last_query, link.last_favorite, link.last_category


def ordered(query):
    """Newest first, with ``id`` breaking ties.

    Without the tiebreaker two links stored in the same microsecond have no
    defined order between them, and the database is free to return them
    differently for the page-1 query and the page-2 query — which shows up
    as a result that appears twice, or never.
    """
    return query.order_by(Link.created_at.desc(), Link.id.desc())


async def answer_results(
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

    # Recorded even when there are no results, and before the early return.
    # Otherwise a search that matches nothing leaves the previous search's
    # context in place, and the next /details answers about a list the user
    # is no longer looking at.
    remember_results(db, str(message.chat.id), q=q, favorite=favorite, category=category)

    if not total:
        await message.answer("لا نتائج.")
        return

    rows = ordered(query).offset(page * PAGE_SIZE).limit(PAGE_SIZE).all()
    start = page * PAGE_SIZE

    # One message per result rather than one block of numbered lines.
    # Telegram attaches a keyboard to a MESSAGE, not to a line inside one,
    # so a single block can only ever carry page controls — which is why
    # acting on a result meant reading its number off the screen and
    # typing "/details 42" by hand.
    await message.answer(f"النتائج {start + 1}–{start + len(rows)} من {total}")
    for i, link in enumerate(rows):
        await message.answer(
            format_link(link, start + i + 1),
            reply_markup=result_actions(link.id, bool(link.is_favorite)),
            disable_web_page_preview=True,
        )

    # The pager rides the last message, so "next" is where the eye already
    # is after reading the page.
    controls = pager(q, page, total, favorite, category)
    if controls is not None:
        await message.answer(f"صفحة {page + 1} من {(total + PAGE_SIZE - 1) // PAGE_SIZE}", reply_markup=controls)


def help_text(linked: bool) -> str:
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
