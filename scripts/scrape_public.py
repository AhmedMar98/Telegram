"""Read every public source that needs no account, on a schedule.

Runs beside ``scripts/collect.py``, never inside it. Two reasons, and both
are operational rather than aesthetic:

1. **A userbot failure must not stop this, and this must not stop a
   userbot run.** They fail for completely unrelated reasons — a revoked
   session on one side, a renamed channel on the other — and one script
   would make either failure look like the other.
2. **They have different risk profiles.** ``collect.py`` holds ten
   Telegram account credentials and a FloodWait there costs a run. This
   holds no credentials at all and can be retried freely, so it does not
   need the same caution or the same schedule.

Usage:  python -m scripts.scrape_public
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal  # noqa: E402
from app.dialogs import SOURCE_PUBLIC  # noqa: E402
from app.models import Channel  # noqa: E402
from app.publicsource import collect_public_channel  # noqa: E402
from app.rls import scope_session_to_workspace  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("scrape_public")

DEFAULT_MAX_SOURCES = 50


def _max_sources() -> int:
    try:
        return max(1, int(os.environ.get("SCRAPER_MAX_SOURCES", DEFAULT_MAX_SOURCES)))
    except ValueError:
        return DEFAULT_MAX_SOURCES


async def run() -> int:
    db = SessionLocal()
    total = 0
    try:
        # Least recently collected first, never-collected before everything
        # — the same rotation the userbot collector uses, for the same
        # reason: a fixed prefix means the sources past the cap are never
        # read at all, not "read later".
        sources = (
            db.query(Channel)
            .filter(Channel.source == SOURCE_PUBLIC, Channel.is_active.is_(True))
            .order_by(
                Channel.last_collected_at.is_(None).desc(),
                Channel.last_collected_at.asc(),
                Channel.id,
            )
            .limit(_max_sources())
            .all()
        )

        if not sources:
            logger.info("no public sources registered")
            return 0

        for channel in sources:
            if not channel.username:
                # A public source with no username cannot be addressed. It
                # should be impossible to create one, so this is logged
                # rather than skipped silently.
                logger.warning("public source %s has no username — skipped", channel.id)
                continue

            # Row-level security is scoped per workspace, and these rows
            # span workspaces. Scoping before each one is what keeps
            # ingest's writes inside the right tenant.
            scope_session_to_workspace(db, channel.workspace_id)
            try:
                stored = await collect_public_channel(db, channel)
            except Exception as exc:  # noqa: BLE001 - one bad source must not end the run
                logger.error("public source %s (%s) failed: %s", channel.id, channel.username, exc)
                db.rollback()
                continue

            total += stored
            logger.info("public source %s: %d new link(s)", channel.username, stored)

        logger.info("public scrape finished: %d new link(s) from %d source(s)", total, len(sources))
        return total
    finally:
        db.close()


def main() -> int:
    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
