"""Telegram webhook receiver + web-side bot-link-code issuance."""

from __future__ import annotations

from aiogram.types import Update
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.bot.telegram_bot import (
    WEBHOOK_SECRET_HEADER,
    deep_link,
    dispatcher,
    generate_link_code,
    get_bot,
    webhook_token,
)
from app.botdiag import diagnose
from app.config import get_settings
from app.database import get_db
from app.deps import get_current_user
from app.models import User
from app.security import constant_time_equals

router = APIRouter(tags=["bot"])


@router.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> dict:
    """Receive one update. The secret travels in a header, not the path.

    It used to be a path segment — ``/telegram/webhook/{secret}`` — and
    that was wrong twice over. A base64 secret contains ``/``, which
    ``{secret}`` cannot match because a path parameter stops at a
    separator, so real deliveries from Telegram returned 404 while the
    bot looked simply unresponsive. And a URL is written to every access
    log it passes through, so the credential was printed in clear text on
    every single delivery.

    Telegram's own ``secret_token`` mechanism solves both: the value goes
    in ``X-Telegram-Bot-Api-Secret-Token`` and the path is constant.

    Still 404 rather than 401 on a mismatch: an unauthenticated caller
    should not be able to learn that this path exists.
    """
    settings = get_settings()
    expected = settings.bot_webhook_secret
    presented = request.headers.get(WEBHOOK_SECRET_HEADER, "")
    if not expected or not constant_time_equals(presented, webhook_token(expected)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    bot = get_bot()
    if bot is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="bot not configured")

    payload = await request.json()
    update = Update.model_validate(payload)
    await dispatcher.feed_update(bot, update)
    return {"ok": True}


@router.post("/bot/link-code")
def create_link_code(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)) -> dict:
    """Issue a single-use code, and a link that spends it in one tap.

    ``deep_link`` is the path that should normally be used; ``instructions``
    stays for the case where startup never reached Telegram and the
    username is unknown, and as something a user on another device can
    read out. Both describe the same code — the link is not a second
    credential.
    """
    code = generate_link_code(db, current_user.workspace_id)
    return {
        "code": code,
        "deep_link": deep_link(code),
        "instructions": "أرسل للبوت: /start " + code,
    }


@router.get("/bot/diagnostics")
def bot_diagnostics(current_user: User = Depends(get_current_user)) -> dict:
    """Why is the bot silent? Answered from the dashboard, with no shell.

    ``scripts/check_bot.py`` answers the same question, and on any host
    with a terminal it is the better tool. This exists because Render's
    free plan — the plan this project is built for — provides no Shell, so
    on the deployment that actually ships, the script cannot be run at
    all. A diagnostic the operator cannot reach is not a diagnostic.

    Authenticated, because it reports on the deployment's configuration.
    Nothing here returns the token, and the webhook secret is masked
    before the payload is built: it is that endpoint's only
    authentication.
    """
    settings = get_settings()
    return diagnose(settings.bot_token, settings.public_base_url, settings.bot_webhook_secret)
