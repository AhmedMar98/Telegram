"""Run the FastAPI web server only (no workers)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn

from app.config import get_settings
from app.logging_setup import setup_logging


def main() -> None:
    setup_logging()
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.app_debug and not settings.is_production,
        log_level="info",
    )


if __name__ == "__main__":
    main()
