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
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from sqlalchemy import or_  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import Link  # noqa: E402
from app.ssrf import check_url as ssrf_check  # noqa: E402
from app.timeutil import utcnow  # noqa: E402
from app.vitality import (  # noqa: E402
    TRANSIENT_STATUSES,
    UNREACHABLE_DEATH_THRESHOLD,
)

logger = logging.getLogger("vitality")


# Redirect hops followed before giving up. Each hop is re-validated
# against app/ssrf.py, so this also bounds how many DNS lookups one
# hostile link can cost. Five covers every legitimate chain seen in
# practice (http->https, apex->www, shortener->target).
MAX_REDIRECT_HOPS = 5
DEFAULT_BATCH_LIMIT = 300
DEFAULT_CONCURRENCY = 15
DEFAULT_TIMEOUT_SECONDS = 10.0

USER_AGENT = "Mozilla/5.0 (compatible; LinkIntelligencePlatform/1.0; +link-vitality-check)"

# Sent on every probe. A number of servers answer an Accept-Language-less
# request with a 403 or a consent interstitial, which the checker would
# otherwise read as the link being gone. Arabic first because that is what
# the collections here are mostly made of.
ACCEPT_LANGUAGE = "ar,en;q=0.8,*;q=0.5"

# A link that has failed this many checks in a row is re-probed on this
# slower cadence instead of every run. The scheduled job has a fixed
# per-run budget, and spending it re-confirming links that have been dead
# for a week means the fresh ones wait.
BACKOFF_AFTER_FAILURES = 3
BACKOFF_DAYS = 3


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


def _select_batch(db: Session, limit: int, *, now=None) -> list[Link]:
    """Pick this run's links, most-deserving first.

    Three passes in priority order rather than one clever ``ORDER BY``:

    1. **Favourites.** A link someone starred is the one they will actually
       be annoyed to find broken, so it never queues behind ten thousand
       links nobody looked at.
    2. **Never checked.** A link with no answer at all is worth more than
       re-confirming one that already has a recent one.
    3. **Stalest first**, subject to the backoff below.

    Separate queries also keep this portable: a single
    ``ORDER BY last_checked_at NULLS FIRST`` relies on dialect-specific
    NULL ordering that differs between SQLite and Postgres.

    The backoff drops links that have failed ``BACKOFF_AFTER_FAILURES``
    times in a row and were probed within the last ``BACKOFF_DAYS`` days.
    Each run has a fixed budget; spending it re-confirming week-old
    corpses means the fresh links wait.
    """
    now = now or utcnow()
    backoff_cutoff = now - timedelta(days=BACKOFF_DAYS)
    selected: list[Link] = []
    seen: set[int] = set()

    def take(query) -> None:
        remaining = limit - len(selected)
        if remaining <= 0:
            return
        for link in query.limit(remaining).all():
            if link.id not in seen:
                seen.add(link.id)
                selected.append(link)

    # Backed-off links are excluded from every pass, favourites included:
    # starring a link does not make a server that has been down for a week
    # answer any faster.
    not_backed_off = or_(
        Link.consecutive_failures < BACKOFF_AFTER_FAILURES,
        Link.last_checked_at.is_(None),
        Link.last_checked_at < backoff_cutoff,
    )

    take(
        db.query(Link)
        .filter(Link.is_favorite.is_(True), not_backed_off)
        .order_by(Link.last_checked_at.is_(None).desc(), Link.last_checked_at.asc(), Link.id)
    )
    take(db.query(Link).filter(Link.last_checked_at.is_(None), not_backed_off).order_by(Link.id))
    take(
        db.query(Link)
        .filter(Link.last_checked_at.is_not(None), not_backed_off)
        .order_by(Link.last_checked_at.asc())
    )
    return selected


@dataclass(frozen=True)
class ProbeResult:
    """The outcome of one probe.

    Three outcomes, not two. ``unreachable`` is the one that was missing:
    a 429 or a 503 says the *server* could not answer right now, which is
    not evidence that the link is gone. Collapsing it into "dead" meant one
    popular host rate-limiting the checker marked every link on it dead in
    a single run.
    """

    outcome: Literal["alive", "dead", "unreachable"]
    http_status: int | None


async def _probe(client: httpx.AsyncClient, url: str) -> httpx.Response | ProbeResult:
    """One HEAD (falling back to GET), following redirects hop by hop.

    ``follow_redirects=True`` is deliberately **not** used. httpx would
    then chase a redirect for us, and a URL that resolves publicly is free
    to answer 302 pointing at 127.0.0.1 — so validating only the URL we
    started with would protect nothing. Each hop is re-validated by
    app/ssrf.py before it is requested.

    Returns the final response, or a ProbeResult when the chain was
    refused or ran too long.
    """
    current = url
    for _ in range(MAX_REDIRECT_HOPS):
        reason = await ssrf_check(current)
        if reason is not None:
            logger.warning("refusing %s: %s", current, reason)
            # "dead", not "unreachable": a link that points into private
            # space is not a resource this deployment should ever fetch,
            # and marking it unreachable would retry it on every run.
            return ProbeResult("dead", None)

        response = await client.head(current, follow_redirects=False)
        if response.status_code in (405, 501):
            # Some servers reject HEAD outright; a real GET is the only way
            # to know whether the resource itself is actually there.
            response = await client.get(current, follow_redirects=False)

        if not response.is_redirect:
            return response
        location = response.headers.get("location")
        if not location:
            return response
        current = str(response.url.join(location))

    logger.warning("redirect chain too long for %s", url)
    return ProbeResult("unreachable", None)


async def check_one(client: httpx.AsyncClient, url: str) -> ProbeResult:
    """Probe a single URL."""
    try:
        outcome = await _probe(client, url)
        if isinstance(outcome, ProbeResult):
            return outcome
        response = outcome
    except httpx.HTTPError:
        # A connection error is genuinely ambiguous — a retired domain and
        # a momentary DNS failure look identical from here. It is counted as
        # a failure but is not on its own a death sentence; only a repeated
        # streak of these ends up marking the link dead.
        return ProbeResult("unreachable", None)

    status = response.status_code
    if status < 400:
        return ProbeResult("alive", status)
    if status in TRANSIENT_STATUSES or status >= 500:
        return ProbeResult("unreachable", status)
    return ProbeResult("dead", status)


def apply_probe(link: Link, result: ProbeResult, now) -> None:
    """Fold one probe into a link's stored vitality state.

    Kept separate from the network code so the state machine — which is the
    part with the subtle rules — is testable without any HTTP at all.
    """
    link.last_checked_at = now
    link.http_status = result.http_status

    if result.outcome == "alive":
        link.is_alive = True
        link.last_alive_at = now
        link.consecutive_failures = 0
        return

    link.consecutive_failures = (link.consecutive_failures or 0) + 1

    if result.outcome == "dead":
        link.is_alive = False
        return

    # Unreachable. A link previously known alive keeps that status for now;
    # only a sustained streak is treated as evidence that it is really gone.
    if link.consecutive_failures >= UNREACHABLE_DEATH_THRESHOLD:
        link.is_alive = False


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

        async def _bounded(client: httpx.AsyncClient, url: str) -> ProbeResult:
            async with semaphore:
                return await check_one(client, url)

        async with httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept-Language": ACCEPT_LANGUAGE},
            transport=transport,
        ) as client:
            results = await asyncio.gather(*[_bounded(client, link.url) for link in links])

        now = utcnow()
        tally = {"alive": 0, "dead": 0, "unreachable": 0}
        for link, result in zip(links, results, strict=True):
            apply_probe(link, result, now)
            tally[result.outcome] += 1
        db.commit()

        logger.info(
            "checked %d link(s): %d alive, %d dead, %d unreachable (status unchanged unless the streak reached %d)",
            len(links),
            tally["alive"],
            tally["dead"],
            tally["unreachable"],
            UNREACHABLE_DEATH_THRESHOLD,
        )
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
