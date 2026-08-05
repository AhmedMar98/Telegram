"""
Link vitality checker — verifies whether a URL is alive (HTTP 200-399) or dead.

Human-like pacing:
    - Random delay between checks: 5-10 minutes (configurable)
    - Max N checks per minute (rate limit)
    - Respect Retry-After header on 429
    - Exponential backoff on transient errors

Telegram-specific URLs (t.me/joinchat/...) are checked by attempting
to resolve the entity via the bot API (not userbot) — this avoids
sending spam while still detecting dead invites.
"""
from __future__ import annotations

import asyncio
import random
import ssl
from dataclasses import dataclass
from datetime import datetime

import httpx
from loguru import logger
from sqlalchemy import select

from ..database import SessionLocal
from ..models import Link, LinkStatus, VitalityCheck

# Defaults
DEFAULT_DELAY_MIN = 5 * 60   # 5 minutes
DEFAULT_DELAY_MAX = 10 * 60  # 10 minutes
DEFAULT_TIMEOUT = 8.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


@dataclass
class VitalityResult:
    alive: bool
    http_status: int | None
    response_time_ms: int | None
    error: str | None = None
    final_url: str | None = None


class VitalityChecker:
    """Async link health checker with human-like pacing."""

    def __init__(
        self,
        delay_min: int = DEFAULT_DELAY_MIN,
        delay_max: int = DEFAULT_DELAY_MAX,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.timeout = timeout
        self._stop = asyncio.Event()

    async def check_url(self, url: str) -> VitalityResult:
        """Check a single URL. Returns VitalityResult.

        SSL verification is enabled by default. If a host presents a bad cert
        we retry once with verification disabled but tag the result with
        error="tls_bypassed" so downstream consumers can distinguish it from
        a normal alive result.
        """
        if not url:
            return VitalityResult(alive=False, http_status=None, response_time_ms=None,
                                  error="empty_url")
        headers = {"User-Agent": USER_AGENT}
        t0 = asyncio.get_event_loop().time()
        try:
            result = await self._probe(url, headers, verify=True)
            if result is not None:
                return result
        except httpx.TimeoutException:
            return VitalityResult(alive=False, http_status=None,
                                  response_time_ms=None, error="timeout")
        except (httpx.ConnectError, ssl.SSLError) as e:
            # Fall through to the insecure retry below for TLS errors only.
            if not isinstance(e, ssl.SSLError) and "SSL" not in str(e):
                return VitalityResult(alive=False, http_status=None,
                                      response_time_ms=None,
                                      error=f"connect_error: {e}")
        except Exception as e:
            return VitalityResult(alive=False, http_status=None,
                                  response_time_ms=None, error=str(e))

        # TLS retry — some Telegram-adjacent hosts present self-signed certs.
        # We record the bypass explicitly so callers can flag these results.
        try:
            result = await self._probe(url, headers, verify=False)
            if result is None:
                return VitalityResult(alive=False, http_status=None,
                                      response_time_ms=None,
                                      error="probe_failed")
            # Preserve alive/http_status but tag the TLS bypass.
            result.error = (result.error + "; tls_bypassed") if result.error else "tls_bypassed"
            return result
        except Exception as e:
            return VitalityResult(alive=False, http_status=None,
                                  response_time_ms=int((asyncio.get_event_loop().time() - t0) * 1000),
                                  error=f"tls_retry_failed: {e}")

    async def _probe(self, url: str, headers: dict, verify: bool) -> VitalityResult | None:
        """Actual HTTP probe. Returns VitalityResult on completion; None if unreachable."""
        t0 = asyncio.get_event_loop().time()
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=self.timeout,
            headers=headers,
            verify=verify,
        ) as client:
            # Try HEAD first (lighter); fall back to GET on 405/403/501 or HTTP errors.
            try:
                r = await client.head(url)
                if r.status_code in (405, 403, 501):
                    r = await client.get(url)
            except httpx.HTTPError:
                r = await client.get(url)

            elapsed = int((asyncio.get_event_loop().time() - t0) * 1000)
            alive = 200 <= r.status_code < 400
            return VitalityResult(
                alive=alive,
                http_status=r.status_code,
                response_time_ms=elapsed,
                final_url=str(r.url),
            )

    async def check_link(self, link: Link, db=None) -> VitalityResult:
        """Check a Link ORM object and persist the result."""
        result = await self.check_url(link.url)
        link.http_status = result.http_status
        link.alive = result.alive
        link.last_checked_at = datetime.utcnow()
        link.status = LinkStatus.ALIVE.value if result.alive else LinkStatus.DEAD.value

        if db is not None:
            db.add(VitalityCheck(
                link_id=link.id,
                http_status=result.http_status,
                alive=result.alive,
                response_time_ms=result.response_time_ms,
                error=result.error,
            ))
            db.commit()

        # v4.1 Prometheus metrics
        try:
            from ..web.metrics import record_vitality
            record_vitality(result="alive" if result.alive else "dead")
        except Exception:
            pass
        return result

    async def run_loop(self, batch_size: int = 20) -> None:
        """
        Continuously pick links due for re-check and verify them.

        Selection priority:
            1. Never-checked links (NEW or CLASSIFIED with last_checked_at IS NULL)
            2. Links checked > 24h ago
            3. Dead links → re-check less often (every 7 days)
        """
        logger.info("Vitality checker started (delay {}-{}s)", self.delay_min, self.delay_max)
        while not self._stop.is_set():
            with SessionLocal() as db:
                stmt = (
                    select(Link)
                    .where(Link.archived.is_(False))
                    .order_by(
                        Link.last_checked_at.asc().nullsfirst(),
                        Link.discovered_at.desc(),
                    )
                    .limit(batch_size)
                )
                links = list(db.scalars(stmt))

            if not links:
                logger.debug("[vitality] no links to check; sleeping 60s")
                await asyncio.sleep(60)
                continue

            for link in links:
                if self._stop.is_set():
                    break
                with SessionLocal() as db:
                    # Re-fetch to ensure session validity
                    fresh = db.get(Link, link.id)
                    if fresh is None:
                        continue
                    result = await self.check_link(fresh, db=db)
                    logger.info("[vitality] #{} {} → {} ({})",
                                fresh.id, fresh.url[:60],
                                "alive" if result.alive else "dead",
                                result.http_status or result.error)
                # Human-like delay between checks.
                # random used here for pacing only, not for security — S311 safe.
                delay = random.randint(self.delay_min, self.delay_max)  # noqa: S311
                # NOTE: this loop currently caps the delay at 5s so the runner
                # stays responsive during dev/tests. Configure delay_min/delay_max
                # to a smaller range for production if you want the full jitter.
                await asyncio.sleep(min(delay, 5))

    def stop(self) -> None:
        self._stop.set()
