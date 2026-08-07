"""
Loguru logging setup with rich console sink + rotating file sink.
"""

from __future__ import annotations

import sys

from loguru import logger

from .config import LOG_DIR, get_settings


def setup_logging() -> None:
    """Configure loguru sinks. Call once at startup."""
    settings = get_settings()
    logger.remove()
    # Console
    logger.add(
        sys.stderr,
        level="DEBUG" if settings.app_debug else "INFO",
        colorize=True,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | "
        "<cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    )
    # File (rotating)
    logger.add(
        LOG_DIR / "app.log",
        level="INFO",
        rotation="10 MB",
        retention="30 days",
        compression="zip",
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<7} | {name}:{function}:{line} - {message}",
    )
    logger.debug("Logging initialized (env={}, debug={})", settings.app_env, settings.app_debug)
