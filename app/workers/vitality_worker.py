"""
Vitality worker — wrapper around VitalityChecker that runs as a service.
"""
from __future__ import annotations

from loguru import logger

from ..vitality.checker import VitalityChecker


async def run_loop() -> None:
    """Run the vitality checker forever."""
    checker = VitalityChecker()
    try:
        await checker.run_loop()
    except KeyboardInterrupt:
        checker.stop()
    except Exception as e:
        logger.exception("[vitality] fatal: {}", e)
