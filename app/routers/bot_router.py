"""Telegram webhook receiver + web-side bot-link-code issuance."""

from __future__ import annotations

from aiogram.types import Update
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.bot.telegram_bot import deep_link, dispatcher, generate_link_code, get_bot
from app.config import get_settings
from app.database import get_db
from app.deps import get_current_user
from app.models import User

router = APIRouter(tags=["bot"])


@router.post("/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request) -> dict:
    settings = get_settings()
    if not settings.bot_webhook_secret or secret != settings.bot_webhook_secret:
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
