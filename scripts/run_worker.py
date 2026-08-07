"""Run a single worker: collect / classify / vitality / all."""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.logging_setup import setup_logging


async def run_worker(name: str) -> None:
    if name == "collect":
        from app.workers.collect_worker import run_loop

        await run_loop(interval_sec=60)
    elif name == "classify":
        from app.workers.classify_worker import run_loop

        await run_loop(interval_sec=30)
    elif name == "vitality":
        from app.workers.vitality_worker import run_loop

        await run_loop()
    elif name == "all":
        from app.run_all import main

        await main()
    else:
        print(f"Unknown worker: {name}")
        print("Choose from: collect | classify | vitality | all")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a background worker")
    parser.add_argument(
        "worker",
        choices=["collect", "classify", "vitality", "all"],
        help="Which worker to run",
    )
    args = parser.parse_args()

    setup_logging()
    try:
        asyncio.run(run_worker(args.worker))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
