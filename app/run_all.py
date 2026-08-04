"""
Main runner — starts all workers + web server in a single process.

For production, run each worker as a separate process:
    python scripts/run_worker.py collect
    python scripts/run_worker.py classify
    python scripts/run_worker.py vitality
    python scripts/run_web.py
"""
from __future__ import annotations

import asyncio
import signal

from loguru import logger

from .config import get_settings
from .logging_setup import setup_logging
from .workers import collect_worker, classify_worker, vitality_worker


async def main() -> None:
    """Run web server + all workers concurrently."""
    setup_logging()
    settings = get_settings()
    logger.info("Starting Link Intelligence Platform v4.0 (env={})", settings.app_env)

    # Init DB
    from .database import init_db
    init_db()

    # Start workers as background tasks
    tasks = [
        asyncio.create_task(collect_worker.run_loop(interval_sec=60), name="collect"),
        asyncio.create_task(classify_worker.run_loop(interval_sec=30), name="classify"),
        asyncio.create_task(vitality_worker.run_loop(), name="vitality"),
    ]

    # Start web server
    if settings.app_host:
        import uvicorn
        config = uvicorn.Config(
            "app.main:app",
            host=settings.app_host,
            port=settings.app_port,
            log_level="info",
            reload=False,
        )
        server = uvicorn.Server(config)
        tasks.append(asyncio.create_task(server.serve(), name="web"))

    # Handle graceful shutdown
    stop_event = asyncio.Event()

    def _signal_handler(*_):
        logger.info("Shutdown signal received")
        stop_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    await stop_event.wait()

    # Cancel all tasks
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("All tasks stopped; goodbye")


if __name__ == "__main__":
    asyncio.run(main())
