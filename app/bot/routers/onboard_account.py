"""Adding a collection account from a Telegram chat.

This was refused once, and the refusal was too broad. What actually
travels through a chat under this flow is worth listing, because the
answer decides the whole design:

===================  ==========================================
what                 exposure
===================  ==========================================
``TG_API_ID``        **never typed** — a server environment variable
``TG_API_HASH``      **never typed** — a server environment variable
session string       **never typed** — generated server-side, encrypted at rest
label                none
phone number         the operator's own, moderate
one-time code        single-use, expires in minutes, worthless once spent
two-factor password  **a durable credential** — the real risk here
===================  ==========================================

So the blanket "never" was wrong: the two things that made it unthinkable
never leave the server. What remains is one genuinely dangerous field and
one moderately sensitive one, and both are handled rather than waved at:

1. **Private chats only.** In a group the code and the password would be
   readable by every member. Refused outright, not warned about.
2. **Every sensitive message is deleted the moment it is read.** Telegram
   keeps chat history; the Bot API lets a bot delete incoming messages in
   a private chat, so the code and the password do not survive the reply
   that consumes them.
3. **Off by default.** ``BOT_ACCOUNT_ONBOARDING`` must be set. An operator
   should choose this, not discover it.
4. **Throttled**, because a conversational flow makes account-adding
   cheap to attempt repeatedly.

One honest limitation, stated where it will be read rather than found:
``app.account_login`` keeps the half-finished login — including a live
Telethon connection — in process memory, because an open MTProto socket
cannot be written to a database. Both steps must therefore reach the same
process. On the single free instance this deployment runs on, they do. If
it is ever scaled to two, the second step will land on a process that has
never heard of the first, and the flow says so instead of failing oddly.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.orm import Session

from app import account_login
from app.bot.shared import NOT_LINKED, resolve_workspace
from app.config import get_settings
from app.security import is_action_rate_limited, record_action_event

logger = logging.getLogger(__name__)

router = Router(name="onboard_account")

# Where each chat is in the flow, and what it is waiting for. In process,
# like the Telethon client it accompanies — putting the step in the
# database while the connection it belongs to lives in memory would create
# two halves of one state that can disagree.
_FLOW: dict[str, dict] = {}

ASK_LABEL = "label"
ASK_PHONE = "phone"
ASK_CODE = "code"
ASK_PASSWORD = "password"

RATE_SCOPE = "bot_account_add"
MAX_ATTEMPTS = 5
WINDOW_MINUTES = 60

OFF_MESSAGE = (
    "إضافة حسابات الجمع من البوت غير مفعّلة في هذا النشر.\n"
    "فعّلها بضبط BOT_ACCOUNT_ONBOARDING=true، أو أضِف الحساب من «الجمع» في اللوحة."
)

GROUP_MESSAGE = (
    "⛔ لا أفعل هذا في مجموعة.\n\n"
    "الخطوات القادمة تتضمّن رمز تحقّق — وربما كلمة تحقّق بخطوتين — "
    "وكل عضو في هذه المجموعة سيقرأهما. راسلني في محادثة خاصّة."
)

WARNING = (
    "قبل أن نبدأ، اعرف ما الذي يمرّ من هنا:\n\n"
    "• <b>لن أطلب</b> api_id ولا api_hash ولا سلسلة الجلسة — هذه لا تغادر الخادم إطلاقاً.\n"
    "• سأطلب: تسمية، رقم هاتف، ثم رمز التحقّق، وكلمة التحقّق بخطوتين إن كان الحساب محميّاً بها.\n"
    "• <b>أحذف كل رسالة تحتوي رمزاً أو كلمة مرور فور قراءتها</b>، لكن الحذف من طرفي لا يضمن "
    "عدم بقاء نسخة على جهازك.\n"
    "• الأأمن يبقى إضافة الحساب من اللوحة على متصفّح.\n\n"
    "أرسل الآن <b>تسمية</b> لهذا الحساب (مثال: هاتفي الثاني)، أو /cancel للإلغاء."
)


async def _scrub(message: Message) -> None:
    """Delete a message carrying a secret, and never let that fail the flow.

    A bot may delete incoming messages in a private chat. It can still
    fail — the message is too old, or permissions changed — and a failed
    cleanup must not abandon a half-finished login, so the failure is
    logged and the flow continues. The user is told the deletion is
    best-effort in the warning above rather than promised it.
    """
    try:
        await message.delete()
    except Exception as exc:  # noqa: BLE001 - cleanup is best effort by nature
        logger.info("could not delete a sensitive message: %s", exc)


def _is_private(message: Message) -> bool:
    return getattr(getattr(message, "chat", None), "type", None) == "private"


@router.message(Command("addaccount"))
async def handle_add_account(message: Message, db: Session) -> None:
    if not get_settings().bot_account_onboarding:
        await message.answer(OFF_MESSAGE)
        return

    if not _is_private(message):
        await message.answer(GROUP_MESSAGE)
        return

    workspace_id = resolve_workspace(db, str(message.chat.id))
    if workspace_id is None:
        await message.answer(NOT_LINKED)
        return

    if is_action_rate_limited(
        db, RATE_SCOPE, str(workspace_id), limit=MAX_ATTEMPTS, window_minutes=WINDOW_MINUTES
    ):
        await message.answer("حاولتَ مرّات كثيرة خلال ساعة. انتظر قليلاً ثم أعد المحاولة.")
        return

    _FLOW[str(message.chat.id)] = {"step": ASK_LABEL, "workspace_id": workspace_id}
    await message.answer(WARNING, parse_mode="HTML")


@router.message(Command("cancel"))
async def handle_cancel(message: Message, db: Session) -> None:
    if _FLOW.pop(str(message.chat.id), None) is None:
        await message.answer("لا توجد عملية جارية.")
        return
    await message.answer("أُلغيت العملية. لم يُحفظ أي شيء.")


@router.message(F.text)
async def handle_flow_step(message: Message, db: Session) -> None:
    """The next answer in an active flow.

    Registered in this router and this router is included **before** the
    search router, whose own ``F.text`` handler is a catch-all. Ordering is
    what keeps a phone number from being treated as a search term — and it
    is asserted in the tests rather than trusted to this comment.

    Returns without answering when there is no flow, so the update falls
    through to the catch-all exactly as before.
    """
    chat_id = str(message.chat.id)
    flow = _FLOW.get(chat_id)
    if flow is None:
        return

    if not _is_private(message):  # a linked chat could have become a group
        _FLOW.pop(chat_id, None)
        await message.answer(GROUP_MESSAGE)
        return

    text = (message.text or "").strip()
    if text.startswith("/"):
        return  # a command mid-flow belongs to its own handler

    step = flow["step"]
    if step == ASK_LABEL:
        flow["label"] = text[:100]
        flow["step"] = ASK_PHONE
        await message.answer("أرسل الآن رقم الهاتف بالصيغة الدولية، مثل ‎+9665xxxxxxxx")
        return

    if step == ASK_PHONE:
        await _start_login(message, db, flow, phone=text)
        return

    if step == ASK_CODE:
        await _scrub(message)
        await _verify(message, db, flow, code=text, password=None)
        return

    if step == ASK_PASSWORD:
        await _scrub(message)
        await _verify(message, db, flow, code=None, password=text)
        return


async def _start_login(message: Message, db: Session, flow: dict, *, phone: str) -> None:
    record_action_event(db, RATE_SCOPE, str(flow["workspace_id"]))
    db.commit()
    try:
        token = await account_login.start_login(db, flow["workspace_id"], flow["label"], phone)
    except account_login.LoginError as exc:
        _FLOW.pop(str(message.chat.id), None)
        await message.answer(f"تعذّر البدء: {exc}")
        return

    flow["token"] = token
    flow["step"] = ASK_CODE
    await message.answer(
        "أرسلتُ رمز التحقّق إلى حسابك في تيليجرام. أرسله هنا.\n<i>سأحذف رسالتك فور قراءتها.</i>",
        parse_mode="HTML",
    )


async def _verify(message: Message, db: Session, flow: dict, *, code: str | None, password: str | None) -> None:
    chat_id = str(message.chat.id)
    try:
        account = await account_login.verify_login(db, flow["workspace_id"], flow["token"], code, password)
    except account_login.NeedsPassword:
        flow["step"] = ASK_PASSWORD
        await message.answer(
            "هذا الحساب محميّ بالتحقّق بخطوتين. أرسل كلمة التحقّق.\n"
            "<i>سأحذف رسالتك فور قراءتها — ومع ذلك، إضافة الحساب من اللوحة أأمن لهذه الخطوة.</i>",
            parse_mode="HTML",
        )
        return
    except KeyError:
        # The pending login lives in process memory alongside its open
        # MTProto connection. A restart between the two steps — or a second
        # instance — loses it, and saying so beats "something went wrong".
        _FLOW.pop(chat_id, None)
        await message.answer("انتهت صلاحية هذه المحاولة (أُعيد تشغيل الخادم غالباً). أرسل /addaccount من جديد.")
        return
    except account_login.LoginError as exc:
        _FLOW.pop(chat_id, None)
        await message.answer(f"فشل التحقّق: {exc}")
        return

    _FLOW.pop(chat_id, None)
    await message.answer(
        f"✅ أُضيف الحساب «{account.label}».\n"
        "الجلسة مشفّرة في القاعدة ولم تُعرَض هنا ولا في أيّ مكان.\n"
        "سيبدأ الجمع منه في التشغيلة القادمة."
    )
