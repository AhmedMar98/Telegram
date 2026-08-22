"""FastAPI application entrypoint.

Single process, single free Render web service: serves the JSON API, the
server-rendered search UI, and the Telegram bot webhook. There is no
second process to pay for — the collector runs separately on a schedule
(see scripts/collect.py, driven by .github/workflows/collector.yml).
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session
from starlette.middleware.gzip import GZipMiddleware

from app import metrics
from app.classifier import CATEGORIES
from app.classifier.llm import probe as groq_probe
from app.config import get_settings
from app.database import Base, engine, get_db
from app.deps import COOKIE_NAME
from app.errors import ERROR_CODE_HEADER, ErrorCode
from app.routers import auth, bot_router, channels, links, notifications
from app.routers import status as status_router
from app.security import resolve_session

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
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

# Largest request body accepted, in bytes. The biggest legitimate payload
# is a manual paste, capped at 50,000 characters by LinkImportRequest —
# 1 MiB leaves generous room for multi-byte UTF-8 (Arabic is 2 bytes per
# character) plus JSON overhead, while still refusing a body that could
# only be an attempt to exhaust the 512 MB free tier's memory.
MAX_REQUEST_BODY_BYTES = 1024 * 1024

# Idea 273, and the only phase-11 item the payload measurement supported.
#
# Measured at the free tier's storage ceiling (1.2M links, exactly 1.00
# GiB): one page of search results is **17,841 bytes uncompressed and
# 2,155 gzipped — 87.9% smaller**. Nothing else in this project buys that
# much for three lines.
#
# It matters more here than on a paid host. The free web service sleeps
# after 15 minutes and the first request back pays a cold start already;
# adding 16 KB per page over a phone connection to that is the difference
# between slow and abandoned.
#
# minimum_size exists because compressing a 200-byte response costs CPU to
# make it *larger* after gzip framing. 500 bytes is comfortably above the
# break-even and below every list payload this app produces.
#
# What this deliberately does NOT do: idea 272 (truncating raw_text in
# list responses). Measured on the same fixture, truncation saved nothing
# once gzip was applied — repeated text is exactly what a compressor
# removes for free. See docs/37-phase11-measurements.md §3.
GZIP_MINIMUM_BYTES = 500
app.add_middleware(GZipMiddleware, minimum_size=GZIP_MINIMUM_BYTES)


@app.exception_handler(PoolTimeoutError)
async def _pool_exhausted(request: Request, exc: PoolTimeoutError) -> JSONResponse:
    """Answer "come back shortly", not "something is broken".

    SQLAlchemy raises this when every connection in the pool is checked
    out and the wait ran past ``pool_timeout``. Nothing has failed: the
    application is simply serving more concurrent database work than it
    has connections for, and this request lost the race.

    Left unhandled it surfaces as a **500**, which is a lie with
    consequences. A 500 tells a client the request will never work and
    should not be retried; a monitor tells an operator to go looking for a
    defect. The truth is the opposite on both counts — the same request a
    second later usually succeeds.

    So: 503, and a ``Retry-After`` for the same reason the login throttle
    carries one — a client told to back off with no interval attached
    generally retries immediately, which is precisely the load that caused
    this.

    Measured before this existed (docs/39): at 20 concurrent logins
    against a 15-connection pool, the five losing requests each hung for
    the full 30-second default and *then* returned 500.
    """
    logger.warning("connection pool exhausted serving %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "server is busy, please retry shortly"},
        headers={
            "Retry-After": "2",
            ERROR_CODE_HEADER: ErrorCode.SERVER_BUSY,
        },
    )


@app.middleware("http")
async def limit_request_body(request: Request, call_next):
    """Reject oversized bodies before anything tries to parse them.

    Pydantic's ``max_length`` validation only fires *after* FastAPI has
    read and JSON-decoded the whole body, so a 500 MB request would be
    fully buffered in memory first and only then rejected. Checking
    Content-Length up front costs nothing and refuses it at the door.

    A body with no Content-Length (chunked transfer) is not blocked here:
    it cannot be checked without consuming it, and inventing a limit for a
    case this app's clients never produce would be guessing.
    """
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            declared = int(raw_length)
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "invalid Content-Length"})
        if declared > MAX_REQUEST_BODY_BYTES:
            return JSONResponse(
                status_code=413,
                content={"detail": f"request body exceeds {MAX_REQUEST_BODY_BYTES} bytes"},
            )
    return await call_next(request)


# Idea 85. Every directive is closed rather than merely present: the point
# of this header is what it *refuses*, and a policy carrying
# 'unsafe-inline' on script-src permits exactly what XSS does — a header
# that reads as protection while providing none.
#
# Reaching a real policy took removing the inline code first: 49 on*=
# attributes, five <script> blocks and 29 style attributes. Nonces would
# not have helped, because a nonce cannot apply to an on*= attribute at
# all. See app/static/app.js.
#
# 'unsafe-eval' is absent too, so a later dependency that needs eval fails
# loudly here instead of quietly widening the policy.
CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        # data: for the theme emoji and any inlined icon; no remote hosts.
        "img-src 'self' data:",
        "font-src 'self'",
        # The dashboard only ever talks to its own origin.
        "connect-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "object-src 'none'",
    )
)

# Sent alongside it because a CSP does not cover either of these: MIME
# sniffing can turn an uploaded text file into a script, and a referrer
# leaks the page you came from to every site you click through to.
_SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


@app.middleware("http")
async def record_timing(request: Request, call_next):
    """Time every request (idea 185).

    In middleware rather than per-route for the same reason the security
    headers are: a route added later cannot be the one that forgets. The
    cost is two clock reads and a few additions on an in-memory struct —
    no I/O, so this cannot itself become the slow thing it measures.
    """
    started = time.perf_counter()
    response = await call_next(request)
    metrics.record(time.perf_counter() - started, status_code=response.status_code)
    return response


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Attach the security headers to every response.

    Applied in middleware rather than per-route so a route added later
    cannot be the one that forgets — the same reasoning as the auth
    dependency split in ``app/deps.py``.
    """
    response = await call_next(request)
    for name, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


app.include_router(auth.router)
app.include_router(channels.router)
app.include_router(links.router)
app.include_router(notifications.router)
app.include_router(status_router.router)
app.include_router(bot_router.router)


# Serving the page scripts as files is what makes a real CSP possible: with
# 'unsafe-inline' gone, a <script> block in a template would simply not run.
# See app/static/app.js for how the inline on*= handlers were replaced.
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/healthz")
def healthz() -> dict:
    """Liveness: is the process up? Deliberately does not touch the database.

    Render restarts the service when this fails, so it must not go red for
    a transient database blip that the process itself would ride out.
    """
    return {"status": "ok"}


@app.get("/readyz")
def readyz(diagnostics: bool = False, db: Session = Depends(get_db)) -> dict:
    """Readiness: can we actually serve traffic (database reachable)?

    Also reports the applied migration revision. That belongs here and not
    in ``/healthz``: the revision lives in the database, and ``/healthz``
    deliberately never touches it — Render restarts the service when
    liveness fails, so making it depend on the database would turn a
    transient blip into a restart loop. Idea 75 asked for it in
    ``/healthz``; putting it there would have broken that property.

    The revision is read every call rather than cached at import, because
    the useful case is exactly the one where it changed under a running
    process. A read failure degrades to ``None`` — the service is ready if
    the database answers, whether or not this extra fact is available.

    ``?diagnostics=true`` adds a check of the optional Groq tier (idea
    118). It is opt-in, and deliberately so on both counts: the platform's
    own probe calls this endpoint on a schedule and must stay cheap, and
    the result must never change ``status`` or the HTTP code. Groq being
    down is not this service being down — wiring it into readiness would
    have Render restart a healthy process because a third party had an
    outage. See ``app.classifier.llm.probe`` for the rate and timeout
    limits that keep the unauthenticated path safe.
    """
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        logger.warning("readiness check failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database unavailable"
        ) from exc

    try:
        revision = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except SQLAlchemyError:
        # A database created by create_all() rather than by alembic has no
        # such table. That is a normal test/dev shape, not an outage.
        revision = None

    body: dict = {"status": "ready", "schema_version": revision}
    if diagnostics:
        # Nested under "diagnostics" so it reads as what it is, and so a
        # caller checking readiness never trips over it.
        body["diagnostics"] = {"groq": groq_probe()}
    return body


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
