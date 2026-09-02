"""Adding a collection account: why this is **not** done from the bot.

§42 built the opposite of this file — a conversational flow that asked, in
a Telegram chat, for a phone number, then the login code, then the
two-factor password. The reasoning was that the two credentials which made
the idea unthinkable (``TG_API_HASH`` and the session string) never leave
the server, so what remained could be handled with private-chat-only
enforcement and immediate deletion of every sensitive message.

That reasoning was wrong in a way the mitigations could not fix, and this
file is the retraction.

**Deleting a message is not unsending it.** The bot can delete its copy in
the chat. It cannot delete the copy already delivered to every device the
operator has linked, it cannot undo a notification preview that showed the
code on a lock screen, and it cannot reach a forward that was made in the
seconds before the delete landed. §42 documented the delete as
"best-effort" and treated that as a caveat; it is not a caveat, it is the
whole exposure. A one-time code is worthless once spent — but the
two-factor password is not one-time, and it is a durable credential to the
operator's entire Telegram account.

**And a bot must not ask for either.** Telegram's Bot Platform terms name
requesting a Telegram password or login code as a prohibited use. A design
that needs a mitigation to stay inside the rules is a design on the wrong
side of them.

**The safe path already existed the whole time.** The dashboard has done
this since §36: ``POST /channels/accounts/login/start`` then
``/accounts/login/verify``. It is stronger on every axis that matters —
it requires a *browser session* (an API key is explicitly refused, so a
stolen key cannot mint a Telegram credential), it re-checks the account
password on top of that session, it is rate-limited, and the code is typed
into a form rather than into a message history that syncs to every device.

So the command stays and answers; it just stops being the thing that asks.
An operator who types ``/addaccount`` is sent to the place that does it
properly, which is more useful than a command that has silently vanished.
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.orm import Session

from app.bot.shared import NOT_LINKED, resolve_workspace
from app.config import get_settings

logger = logging.getLogger(__name__)

router = Router(name="onboard_account")

HANDOFF = (
    "🔐 <b>إضافة حساب الجمع تتمّ من لوحة التحكّم، لا من هنا.</b>\n\n"
    "الخطوة تحتاج رمز تحقّق تيليجرام — وربّما كلمة التحقّق بخطوتين — "
    "وهذه <b>لا تُكتَب في محادثة</b>: تاريخ المحادثة يتزامن مع كل جهاز مرتبط "
    "بحسابك، والإشعار قد يعرض الرمز على شاشة مقفلة، وحذفي لرسالتك بعد قراءتها "
    "لا يسترجع أيّاً من ذلك.\n\n"
    "افتح <b>لوحة التحكّم ← الجمع ← إضافة حساب جمع</b>. "
    "هناك يُطلَب الرمز في نموذج، خلف جلسة متصفّح وكلمة مرور لوحتك، "
    "ولا يمرّ بأيّ محادثة."
)


def _dashboard_hint() -> str:
    """The handoff, with the actual address when the deployment knows it."""
    base = (get_settings().public_base_url or "").rstrip("/")
    if not base:
        return HANDOFF
    return f'{HANDOFF}\n\n<a href="{base}/dashboard">افتح لوحة التحكّم</a>'


@router.message(Command("addaccount"))
async def handle_add_account(message: Message, db: Session) -> None:
    """Answer, and point at the dashboard. Never collect a credential.

    Still resolves the workspace first: an unlinked chat gets the linking
    instructions it would get from any other command, rather than advice
    about a dashboard it has not proved it may reach.
    """
    workspace_id = resolve_workspace(db, str(message.chat.id))
    if workspace_id is None:
        await message.answer(NOT_LINKED)
        return
    await message.answer(_dashboard_hint(), parse_mode="HTML", disable_web_page_preview=True)
