"""FastAPI application entrypoint.

Single process, single free Render web service: serves the JSON API, the
server-rendered search UI, and the Telegram bot webhook. There is no
second process to pay for — the collector runs separately on a schedule
(see scripts/collect.py, driven by .github/workflows/collector.yml).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Cookie, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.classifier import CATEGORIES
from app.config import get_settings
from app.database import Base, engine
from app.deps import COOKIE_NAME
from app.routers import auth, bot_router, channels, links
from app.security import resolve_session

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Safety net alongside Alembic migrations: harmless no-op if the
    # schema already exists (e.g. Alembic already ran in the deploy
    # startCommand), and lets `uvicorn app.main:app` work standalone for
    # local development without a separate migration step.
    Base.metadata.create_all(bind=engine)

    settings = get_settings()
    if settings.bot_token and settings.bot_webhook_secret and settings.public_base_url:
        from app.bot.telegram_bot import get_bot

        bot = get_bot()
        if bot is not None:
            webhook_url = f"{settings.public_base_url.rstrip('/')}/telegram/webhook/{settings.bot_webhook_secret}"
            try:
                await bot.set_webhook(webhook_url)
            except Exception:  # noqa: BLE001 - never let a Telegram outage block startup
                logger.exception("failed to set telegram webhook")
            finally:
                await bot.session.close()

    yield


app = FastAPI(title=get_settings().app_name, lifespan=lifespan)
app.include_router(auth.router)
app.include_router(channels.router)
app.include_router(links.router)
app.include_router(bot_router.router)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> RedirectResponse:
    return RedirectResponse(url="/dashboard" if session else "/login")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {})


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request) -> HTMLResponse:
    settings = get_settings()
    return templates.TemplateResponse(request, "register.html", {"invite_required": bool(settings.invite_code)})


@app.get("/dashboard", response_class=HTMLResponse, response_model=None)
def dashboard_page(
    request: Request,
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> HTMLResponse | RedirectResponse:
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        user = resolve_session(db, session)
    finally:
        db.close()

    if user is None:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(request, "dashboard.html", {"categories": CATEGORIES})
