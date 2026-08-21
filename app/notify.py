"""Telling the account owner something happened, through the bot.

Idea 89 asks for an alert on a sign-in from an unfamiliar device. Every
other delivery channel fails this project's constraints: email needs an
SMTP provider and its credentials, push needs a service, and both need
something running to send them. **The Telegram bot already exists, is
already linked to the workspace, and costs nothing** — so it is not merely
the convenient channel here, it is the only one that fits.

Three rules keep an alert from becoming a liability:

- **It never blocks the login.** A notification failure is not an
  authentication failure. Everything here is best-effort and swallows its
  own errors, exactly like the optional Groq tier.
- **It is only ever sent to a chat the workspace linked itself.** There is
  no address in the payload and no way to aim one elsewhere.
- **It says what happened without quoting attacker-controlled text.** A
  User-Agent is supplied by whoever made the request; it is summarised to
  a short, escaped fragment rather than relayed verbatim into a chat.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models import AuthSession, BotLink

logger = logging.getLogger(__name__)

# How much of a client-supplied User-Agent is worth showing. Enough to
# recognise your own browser, short enough that nobody can paste an essay
# into someone else's chat.
_UA_SUMMARY_LENGTH = 60


def _linked_chat_ids(db: Session, workspace_id: int) -> list[str]:
    return [row.chat_id for row in db.query(BotLink).filter(BotLink.workspace_id == workspace_id)]


def is_familiar_device(db: Session, user_id: int, *, ip_address: str | None, user_agent: str | None) -> bool:
    """Whether this account has signed in from this combination before.

    Deliberately coarse: matching on IP *or* User-Agent rather than both.
    Home addresses change constantly, and a stricter rule would alert on
    every reconnection until people stop reading the alerts — which is the
    real failure mode for a notification like this.

    A brand-new account with no prior sessions counts as familiar, because
    alerting someone about the login they are currently performing teaches
    them the alert means nothing.
    """
    previous = db.query(AuthSession).filter(AuthSession.user_id == user_id).all()
    if not previous:
        return True

    return any(
        (ip_address is not None and s.ip_address == ip_address)
        or (user_agent is not None and s.user_agent == user_agent)
        for s in previous
    )


def _escape(value: str) -> str:
    """Neutralise Telegram markup in a value the client supplied."""
    for char in ("_", "*", "[", "]", "`"):
        value = value.replace(char, "")
    return value


def describe_device(*, ip_address: str | None, user_agent: str | None) -> str:
    parts = []
    if ip_address:
        parts.append(_escape(ip_address)[:45])
    if user_agent:
        parts.append(_escape(user_agent)[:_UA_SUMMARY_LENGTH])
    return " · ".join(parts) or "جهاز غير معروف"


def new_device_message(*, ip_address: str | None, user_agent: str | None) -> str:
    return (
        "🔐 تسجيل دخول من جهاز جديد\n"
        f"{describe_device(ip_address=ip_address, user_agent=user_agent)}\n\n"
        "إن لم تكن أنت: غيّر كلمة المرور فوراً من لوحة التحكم، "
        "ثم «إنهاء كل الجلسات»."
    )


async def send_to_workspace(db: Session, workspace_id: int, text: str) -> int:
    """Deliver to every chat this workspace linked. Never raises.

    Returns how many chats were reached, which is what makes the behaviour
    testable without a live Telegram connection.
    """
    from app.bot.telegram_bot import get_bot

    bot = get_bot()
    if bot is None:
        return 0

    delivered = 0
    for chat_id in _linked_chat_ids(db, workspace_id):
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            delivered += 1
        except Exception as exc:  # noqa: BLE001 - a chat that blocked the bot must not break the caller
            logger.info("could not notify chat %s: %s", chat_id, exc)
    return delivered
