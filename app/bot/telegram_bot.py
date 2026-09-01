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

**This module is the composition root, not the handlers.** Those live in
``app/bot/routers/``, grouped by responsibility. The split is deliberately
*routers* and not separate bots: separate bots would mean a token and a
webhook each — more bearer credentials to protect and more endpoints to
defend — to buy an organisational separation that a single Dispatcher
already provides for free. One token, one webhook, one process, exactly
as before.

Handler names are re-exported below because they are this module's public
surface: the test suite and any future caller address them here, and
moving a function between routers must not become a rename for everyone
who imports it.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.types import TelegramObject
from sqlalchemy.orm import Session

from app.bot.routers.onboarding import handle_help, handle_start, handle_unlink
from app.bot.routers.onboarding import router as onboarding_router
from app.bot.routers.search import (
    handle_details,
    handle_details_button,
    handle_favorite,
    handle_favourite_button,
    handle_latest,
    handle_menu_button,
    handle_other,
    handle_page,
    handle_search,
)
from app.bot.routers.search import router as search_router
from app.bot.routers.status import handle_channels, handle_stats, handle_vitality
from app.bot.routers.status import router as status_router
from app.bot.shared import COMMANDS, NOT_LINKED, PAGE_SIZE, decode_filter, encode_filter
from app.config import get_settings
from app.database import SessionLocal
from app.models import BotLinkCode

logger = logging.getLogger(__name__)

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


def allowed_chat_ids() -> frozenset[str]:
    """The configured allowlist, or an empty set meaning "everyone".

    Read per call rather than captured at import: the tests change the
    setting between cases, and a value frozen at import time would make
    every one of them test the first case's configuration.
    """
    raw = get_settings().bot_allowed_chat_ids or ""
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def _chat_id_of(event: TelegramObject) -> str | None:
    """The chat an update concerns, across the update shapes this bot sees.

    Returns None when the update carries no chat at all, which is not the
    same as "a chat that is not allowed" — see the guard below.
    """
    message = getattr(event, "message", None) or getattr(event, "edited_message", None)
    if message is None:
        callback = getattr(event, "callback_query", None)
        message = getattr(callback, "message", None) if callback is not None else None
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    return None if chat_id is None else str(chat_id)


class AllowlistMiddleware(BaseMiddleware):
    """Drop updates from chats that are not on the allowlist.

    A middleware rather than a check inside each handler, for the reason
    that decides most middleware questions: there are fourteen handlers
    and a fifteenth will be added by someone who has not read this file.
    A guard each handler must remember is a guard that will be forgotten
    exactly once, in the handler that matters.

    Silent by design. An "you are not authorised" reply confirms the bot
    exists and is guarded, which is information a stranger has no reason
    to be given.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        allowed = allowed_chat_ids()
        if not allowed:
            return await handler(event, data)

        chat_id = _chat_id_of(event)
        if chat_id is None:
            # An update with no chat cannot be attributed to anyone, so it
            # cannot be shown to be allowed. Dropped rather than passed:
            # with an allowlist configured, "I could not tell" must fail
            # closed or the allowlist is advisory.
            logger.info("update with no identifiable chat dropped (allowlist active)")
            return None

        if chat_id not in allowed:
            logger.info("update from chat %s dropped (not on the allowlist)", chat_id)
            return None

        return await handler(event, data)


# Ordered before the session middleware on purpose: a rejected update must
# not open a database connection to be rejected.
dispatcher.update.middleware(AllowlistMiddleware())
dispatcher.update.middleware(DbSessionMiddleware())

# Order matters and is not cosmetic: aiogram offers an update to routers in
# registration order, and the search router ends with a catch-all F.text
# handler. Registered first, that catch-all would swallow every command
# behind it — /stats would be answered as a search for the word "/stats".
# tests/test_bot_routers.py pins this rather than trusting the comment.
dispatcher.include_router(onboarding_router)
dispatcher.include_router(status_router)
dispatcher.include_router(search_router)


def get_bot() -> Bot | None:
    settings = get_settings()
    if not settings.bot_token:
        return None
    return Bot(token=settings.bot_token)


# Telegram's own header name for the webhook secret. Its value is a
# bearer credential for /telegram/webhook: anything holding it can post
# forged updates into this deployment.
WEBHOOK_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


def webhook_token(secret: str) -> str:
    """A Telegram-legal token derived from whatever BOT_WEBHOOK_SECRET is.

    Two independent reasons this cannot be the raw secret:

    1. **Telegram rejects most of it.** ``secret_token`` accepts only
       ``A-Z a-z 0-9 _ -``. Render's ``generateValue: true`` produces
       base64, which routinely contains ``/`` and ``=``.

    2. **The raw secret used to sit in the URL, and a URL is not a
       secret.** The route was ``/telegram/webhook/{secret}``, so every
       delivery wrote the credential into the access log in clear text —
       observed on a real deployment:

           POST /telegram/webhook/bLRINbE/hEYeSol/YElT…%3D 404 Not Found

       That line is also the bug this function fixes: ``{secret}`` matches
       ONE path segment, a base64 secret containing ``/`` spans three, no
       route matched, and Telegram's deliveries 404'd. From the outside it
       looked exactly like a bot that ignored valid messages.

    SHA-256 hex is always 64 characters from ``[0-9a-f]`` — inside
    Telegram's alphabet by construction, whatever the operator or the
    platform supplies, and no shorter than the entropy behind it.
    """
    import hashlib

    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


# The bot's @username, learned once at startup from getMe.
#
# It exists so the dashboard can offer a deep link (t.me/<name>?start=<code>)
# instead of an instruction to type. That distinction is not cosmetic: the
# code is a hex string, the command needs a space before it, and the
# dashboard renders right-to-left, so "/start c1d555fe" displays with the
# slash on the far side and reads as if it belonged at the end. A real
# deployment sent eight variations of that line — including the correct one
# — and got nothing back. A link cannot be mistyped.
#
# Cached rather than fetched per request: the username changes only when
# the operator renames the bot in BotFather, which already requires a
# redeploy to matter, and a Telegram round trip has no place in rendering
# a page.
_bot_username: str | None = None


def set_bot_username(username: str | None) -> None:
    """Record the username getMe returned. Called once, from the lifespan."""
    global _bot_username
    _bot_username = username


def bot_username() -> str | None:
    """The @username, or None if startup never reached Telegram."""
    return _bot_username


def deep_link(code: str) -> str | None:
    """A one-tap link that opens the chat with the code already applied.

    None when the username is unknown — the caller keeps the typed
    instruction as a fallback rather than rendering a broken link.
    """
    if not _bot_username:
        return None
    return f"https://t.me/{_bot_username}?start={code}"


def generate_link_code(db: Session, workspace_id: int) -> str:
    code = secrets.token_hex(4)
    db.add(BotLinkCode(workspace_id=workspace_id, code=code))
    db.commit()
    return code


# The filter codec kept its original underscore-prefixed names here even
# though it now lives in app/bot/shared.py under public ones. Renaming it
# would have meant editing the test suite in the same commit that claims
# to change nothing but structure — and a refactor that has to touch its
# own tests can no longer prove it preserved behaviour. The aliases are
# the evidence: tests/test_bot_handlers.py is byte-identical before and
# after this change.
_encode_filter = encode_filter
_decode_filter = decode_filter


__all__ = [
    "COMMANDS",
    "NOT_LINKED",
    "PAGE_SIZE",
    "AllowlistMiddleware",
    "DbSessionMiddleware",
    "allowed_chat_ids",
    "dispatcher",
    "generate_link_code",
    "get_bot",
    "get_settings",
    "handle_channels",
    "handle_details",
    "handle_details_button",
    "handle_favorite",
    "handle_favourite_button",
    "handle_help",
    "handle_latest",
    "handle_menu_button",
    "handle_other",
    "handle_page",
    "handle_search",
    "handle_start",
    "handle_stats",
    "handle_unlink",
    "handle_vitality",
    "onboarding_router",
    "search_router",
    "status_router",
]
