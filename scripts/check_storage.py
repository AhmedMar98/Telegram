"""Warn before the database fills up, rather than after writes start failing.

Idea 153. The failure this exists for has no early symptom: everything
works normally until it abruptly does not, and by then the collection has
already stopped growing.

    python scripts/check_storage.py --workspace 1

Run from a scheduled workflow. Reports and exits 0 either way — this is a
notifier, not a gate, and failing the workflow would turn a warning into a
red build that says nothing extra.

**The threshold is an assumption, and the alert says so.** Render's
published limits could not be verified from this project's development
environment (docs/02 §5 records the same gap for the database's lifetime),
so ``STORAGE_LIMIT_BYTES`` is a configured figure rather than a measured
one. Presenting it as Render's number would be the kind of claim this
project does not make.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.alerts import STORAGE_HIGH  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import Workspace  # noqa: E402
from app.notify import raise_alert  # noqa: E402
from app.storage import database_bytes, largest_table  # noqa: E402

logger = logging.getLogger("storage")


def _human(size: int) -> str:
    megabytes = size / (1024 * 1024)
    if megabytes >= 1024:
        return f"{megabytes / 1024:.2f} غيغابايت"
    return f"{megabytes:.0f} ميغابايت"


async def run(workspace_id: int, *, force: bool = False) -> int:
    """Returns the used byte count, or -1 when the size cannot be read."""
    settings = get_settings()
    db = SessionLocal()
    try:
        if db.get(Workspace, workspace_id) is None:
            sys.exit(f"error: no workspace with id {workspace_id}")

        used = database_bytes(db)
        if used is None:
            # SQLite, or a Postgres role without permission. Reporting an
            # invented number would be worse than reporting nothing.
            logger.info("database size is not readable here; nothing to report")
            return -1

        limit = settings.storage_limit_bytes
        fraction = used / limit if limit else 0.0
        logger.info("using %s of a configured %s limit (%.1f%%)", _human(used), _human(limit), fraction * 100)

        if fraction < settings.storage_alert_fraction and not force:
            return used

        table = largest_table(db)
        await raise_alert(
            db,
            workspace_id,
            STORAGE_HIGH.key,
            title="⚠️ اقتراب حدّ التخزين",
            body=(
                f"القاعدة تستخدم {_human(used)} من حدّ مضبوط قدره {_human(limit)} "
                f"({fraction * 100:.0f}٪)."
                + (f"\nأكبر جدول: {table}." if table else "")
                + "\n\nالحدّ إعداد قابل للتغيير (STORAGE_LIMIT_BYTES) لا رقم مؤكَّد من Render — "
                "راجع `docs/13-retention.md` لخيارات التقليص."
            ),
        )
        return used
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", type=int, required=True, help="workspace id to report to")
    parser.add_argument(
        "--force", action="store_true", help="raise the alert regardless of the threshold (for testing)"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(run(args.workspace, force=args.force))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
