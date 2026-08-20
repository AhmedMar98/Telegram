"""Scheduled link vitality checker.

Run as a one-shot script by .github/workflows/vitality.yml on a cron
schedule — same reasoning as scripts/collect.py: Render has no free tier
for a persistent background worker, so this is a scheduled job rather than
a long-running process.

Each run picks a bounded batch of links (never-checked first, then the
ones checked longest ago), issues one HTTP request per link with bounded
concurrency, and records whether it responded with a non-error status.

This is a reachability probe, not a content check: a 200 with a
"deleted / not found" page rendered client-side (a JS single-page app, for
instance) is indistinguishable here from a genuinely live page. It is also
not a guarantee against false negatives — a link can fail a single check
because of a transient network blip or a site that blocks unfamiliar
User-Agents, not because it is actually gone. Treat `is_alive=False` as
"worth a second look", not as ground truth to auto-delete on.

Required environment:
  DATABASE_URL  - same database the web service uses

Optional:
  VITALITY_CHECK_BATCH_LIMIT   - links checked per run (default 300)
  VITALITY_CHECK_CONCURRENCY   - concurrent requests (default 15)
  VITALITY_CHECK_TIMEOUT_SECONDS - per-request timeout (default 10)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import Link  # noqa: E402
from app.timeutil import utcnow  # noqa: E402

logger = logging.getLogger("vitality")

DEFAULT_BATCH_LIMIT = 300
DEFAULT_CONCURRENCY = 15
DEFAULT_TIMEOUT_SECONDS = 10.0

USER_AGENT = "Mozilla/5.0 (compatible; LinkIntelligencePlatform/1.0; +link-vitality-check)"


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        logger.warning("invalid %s, falling back to %d", name, default)
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return max(1.0, float(os.environ.get(name, default)))
    except ValueError:
        logger.warning("invalid %s, falling back to %s", name, default)
        return default


def _select_batch(db: Session, limit: int) -> list[Link]:
    """Never-checked links first, then the ones checked longest ago.

    Two queries rather than one ``ORDER BY last_checked_at NULLS FIRST``
    keeps this portable across SQLite and Postgres without relying on
    dialect-specific NULL ordering.
    """
    never_checked = db.query(Link).filter(Link.last_checked_at.is_(None)).order_by(Link.id).limit(limit).all()
    remaining = limit - len(never_checked)
    if remaining <= 0:
        return never_checked
    stale = (
        db.query(Link)
        .filter(Link.last_checked_at.is_not(None))
        .order_by(Link.last_checked_at.asc())
        .limit(remaining)
        .all()
    )
    return never_checked + stale


async def check_one(client: httpx.AsyncClient, url: str) -> tuple[bool, int | None]:
    """Probe a single URL. Returns (is_alive, http_status)."""
    try:
        response = await client.head(url, follow_redirects=True)
        if response.status_code in (405, 501):
            # Some servers reject HEAD outright; a real GET is the only way
            # to know whether the resource itself is actually there.
            response = await client.get(url, follow_redirects=True)
    except httpx.HTTPError:
        return False, None
    return response.status_code < 400, response.status_code


async def check_vitality(*, transport: httpx.AsyncBaseTransport | None = None) -> int:
    """Run one batch. ``transport`` is a test seam (httpx.MockTransport) —
    production always uses the real network (transport=None)."""
    limit = _int_env("VITALITY_CHECK_BATCH_LIMIT", DEFAULT_BATCH_LIMIT)
    concurrency = _int_env("VITALITY_CHECK_CONCURRENCY", DEFAULT_CONCURRENCY)
    timeout = _float_env("VITALITY_CHECK_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)

    db = SessionLocal()
    try:
        links = _select_batch(db, limit)
        if not links:
            logger.info("no links due for a vitality check")
            return 0

        semaphore = asyncio.Semaphore(concurrency)

        async def _bounded(client: httpx.AsyncClient, url: str) -> tuple[bool, int | None]:
            async with semaphore:
                return await check_one(client, url)

        async with httpx.AsyncClient(
            timeout=timeout, headers={"User-Agent": USER_AGENT}, transport=transport
        ) as client:
            results = await asyncio.gather(*[_bounded(client, link.url) for link in links])

        now = utcnow()
        alive_count = 0
        for link, (is_alive, http_status) in zip(links, results, strict=True):
            link.last_checked_at = now
            link.http_status = http_status
            link.is_alive = is_alive
            alive_count += int(is_alive)
        db.commit()

        logger.info("checked %d link(s): %d alive, %d dead", len(links), alive_count, len(links) - alive_count)
        return len(links)
    finally:
        db.close()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    asyncio.run(check_vitality())


if __name__ == "__main__":
    main()
