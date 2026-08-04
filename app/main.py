"""Main FastAPI application factory."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from loguru import logger

from .config import get_settings
from .logging_setup import setup_logging
from .web.routes import api_router, router


# Global orchestrator (initialized on startup)
_orchestrator = None


def get_orchestrator():
    """Lazy accessor for the LLM orchestrator."""
    global _orchestrator
    if _orchestrator is None:
        from .llm.orchestrator import LLMOrchestrator
        _orchestrator = LLMOrchestrator()
    return _orchestrator


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup & shutdown hooks."""
    setup_logging()
    settings = get_settings()
    logger.info("Link Intelligence Platform v4.1 starting (env={})", settings.app_env)

    # Init DB
    from .database import init_db
    init_db()
    logger.info("Database initialized")

    # v4.1 Start WebSocket log broadcaster
    from .web.websocket_logs import get_broadcaster
    bc = get_broadcaster()
    await bc.start()

    # Pre-warm LLM orchestrator
    orch = get_orchestrator()
    available = orch.available_providers()
    logger.info("LLM providers available: {}", available or "none")

    # v4.1 Start daily summary worker (best-effort, runs at midnight)
    summary_task = None
    try:
        from .workers.summary_worker import DailySummaryWorker
        summary_worker = DailySummaryWorker(run_hour=0, run_minute=5)
        summary_task = __import__("asyncio").create_task(summary_worker.run_loop())
        logger.info("Daily summary worker scheduled for 00:05 daily")
    except Exception as e:
        logger.warning("Daily summary worker not started: {}", e)

    # Start Telegram bot in background (best-effort)
    bot_task = None
    if settings.tg_bot_token:
        from .bot.telegram_bot import TelegramSearchBot
        import asyncio

        bot = TelegramSearchBot(search_engine=None)  # uses default
        bot_task = asyncio.create_task(bot.start())
        logger.info("Telegram bot started in background")

    yield

    # Cleanup
    if bot_task:
        bot_task.cancel()
        try:
            await bot_task
        except Exception:
            pass
    if summary_task:
        summary_task.cancel()
        try:
            await summary_task
        except Exception:
            pass
    await bc.stop()


def create_app() -> FastAPI:
    """Build and return the FastAPI app."""
    settings = get_settings()
    app = FastAPI(
        title="Link Intelligence Platform",
        version="4.1.0",
        description="Distributed platform for collecting, classifying, and verifying Telegram links.",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # Static + templates
    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Routers — web panel + REST API + WebSocket + metrics
    app.include_router(router)
    app.include_router(api_router)
    # v4.1 routers
    from .web.websocket_logs import router as ws_router
    from .web.metrics import router as metrics_router
    app.include_router(ws_router)
    app.include_router(metrics_router)
    # v5.0 routers (lazy, only if installed)
    try:
        from .billing.routes import router as billing_router
        app.include_router(billing_router)
    except ImportError:
        pass

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "version": "4.1.0",
            "features": [
                "semantic_memory", "multi_tenant", "daily_summary",
                "websocket_logs", "prometheus_metrics",
            ],
        }

    return app


# Module-level app instance (for uvicorn: app.main:app)
app = create_app()
