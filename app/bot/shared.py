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
    link = db.get(BotLink, str(chat_id))
    return link.workspace_id if link else None


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


def pager(q: str | None, page: int, total: int, favorite: bool | None, category: str | None):
    """Previous/next buttons, omitted entirely when there is one page.

    The callback payload carries the whole filter, because a Telegram
    callback arrives with no memory of what produced it and the bot keeps
    no per-chat state — state on a free web service that sleeps would be
    lost between messages anyway.
    """
    buttons = []
    token = encode_filter(q, favorite, category)
    if page > 0:
        buttons.append(InlineKeyboardButton(text="« السابق", callback_data=f"pg:{page - 1}:{token}"))
    if (page + 1) * PAGE_SIZE < total:
        buttons.append(InlineKeyboardButton(text="التالي »", callback_data=f"pg:{page + 1}:{token}"))
    if not buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


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
    if not total:
        await message.answer("لا نتائج.")
        return

    rows = query.order_by(Link.created_at.desc()).offset(page * PAGE_SIZE).limit(PAGE_SIZE).all()
    start = page * PAGE_SIZE
    lines = [format_link(link, start + i + 1) for i, link in enumerate(rows)]
    header = f"النتائج {start + 1}–{start + len(rows)} من {total}"
    body = header + "\n" + "\n".join(lines)

    await message.answer(body, reply_markup=pager(q, page, total, favorite, category))


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
