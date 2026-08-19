"""FastAPI application entrypoint.

Single process, single free Render web service: serves the JSON API, the
server-rendered search UI, and the Telegram bot webhook. There is no
second process to pay for — the collector runs separately on a schedule
(see scripts/collect.py, driven by .github/workflows/collector.yml).
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.classifier import CATEGORIES
from app.config import get_settings
from app.database import Base, engine, get_db
from app.deps import COOKIE_NAME
from app.routers import auth, bot_router, channels, links
from app.security import resolve_session

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Convenience for local development and the test suite only: it lets
    # `uvicorn app.main:app` work against a throwaway SQLite file with no
    # separate migration step. On Postgres the schema is owned solely by
    # Alembic (run from the Render startCommand) — calling create_all there
    # would race the migration and could half-create tables that Alembic
    # then believes it still has to build.
    if engine.dialect.name == "sqlite":
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
    """Liveness: is the process up? Deliberately does not touch the database.

    Render restarts the service when this fails, so it must not go red for
    a transient database blip that the process itself would ride out.
    """
    return {"status": "ok"}


@app.get("/readyz")
def readyz(db: Session = Depends(get_db)) -> dict:
    """Readiness: can we actually serve traffic (database reachable)?"""
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        logger.warning("readiness check failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database unavailable"
        ) from exc
    return {"status": "ready"}


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
    db: Session = Depends(get_db),
) -> HTMLResponse | RedirectResponse:
    user = resolve_session(db, session)
    if user is None:
        return RedirectResponse(url="/login")
    return templates.TemplateResponse(request, "dashboard.html", {"categories": CATEGORIES})
