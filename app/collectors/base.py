"""
Abstract collector interface + shared dataclasses.

A collector's ONLY job: gather messages from a source and extract links.
It does NOT classify, send, or persist (beyond handing off the data).

Human-like behavior is enforced here so all implementations inherit it.
"""
from __future__ import annotations

import asyncio
import random
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator, Iterable
from urllib.parse import urlparse

from loguru import logger

from ..config import get_settings

# ============================================================
# Regex patterns
# ============================================================

URL_RE = re.compile(
    r"https?://[^\s<>\]\)\"']+",
    re.IGNORECASE,
)
# Telegram-specific patterns
TG_JOIN_RE = re.compile(r"(?:t(?:elegram)?\.me|t\.me)/([a-zA-Z0-9_]+)/?([a-zA-Z0-9_]*)")
TG_INVITE_RE = re.compile(r"(?:t(?:elegram)?\.me|t\.me)\?(?:join|invite)=([a-zA-Z0-9_-]+)")
WA_GROUP_RE = re.compile(r"(?:chat\.whatsapp\.com|wa\.me)/([a-zA-Z0-9_-]+)")


@dataclass
class LinkExtraction:
    """A single extracted link with source metadata."""

    url: str
    source_channel: str
    source_message_id: int | None = None
    discovered_at: datetime = field(default_factory=datetime.utcnow)
    # Raw text around the link (helps classification)
    context: str = ""

    @property
    def host(self) -> str:
        try:
            return urlparse(self.url).netloc.lower()
        except Exception:
            return ""


@dataclass
class CollectedMessage:
    """A raw message pulled from a source."""

    channel: str
    message_id: int | None
    text: str
    timestamp: datetime
    has_media: bool = False


# ============================================================
# Base collector
# ============================================================

class BaseCollector(ABC):
    """
    Abstract base class for all collectors.

    Enforces human-like behavior:
        - Random delay between fetches (8-25s by default)
        - Active-hours window (10-16h suggested, configurable)
        - FloodWait handling
        - Never sends messages to other people's groups
    """

    name: str = "base"

    def __init__(self) -> None:
        self.settings = get_settings()
        self._stop = asyncio.Event()

    @abstractmethod
    async def fetch_messages(
        self, channel: str, last_message_id: int | None = None
    ) -> AsyncIterator[CollectedMessage]:
        """Yield messages from a channel newer than last_message_id."""
        ...
        # Make this an async generator
        yield  # pragma: no cover

    async def collect_links(
        self, channel: str, last_message_id: int | None = None
    ) -> AsyncIterator[LinkExtraction]:
        """
        Fetch messages, extract links, yield LinkExtraction objects.

        Enforces human-like pacing between message batches.
        """
        async for msg in self.fetch_messages(channel, last_message_id):
            if self._stop.is_set():
                logger.info("Collector {} received stop signal", self.name)
                break
            for url in self.extract_urls(msg.text):
                yield LinkExtraction(
                    url=url,
                    source_channel=channel,
                    source_message_id=msg.message_id,
                    discovered_at=msg.timestamp,
                    context=msg.text[:500],
                )
            # Human-like delay between messages
            await self._human_delay()

    # ------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------

    @staticmethod
    def extract_urls(text: str) -> Iterable[str]:
        """Extract unique URLs from text, normalizing common Telegram formats."""
        if not text:
            return []
        seen = []
        for m in URL_RE.finditer(text):
            url = m.group(0).rstrip(".,);:'\"")
            if url not in seen:
                seen.append(url)
        # Catch bare t.me/xxx links without protocol
        for m in re.finditer(r"(?<!\S)(t\.me/[a-zA-Z0-9_/?=~-]+)", text):
            url = "https://" + m.group(1)
            if url not in seen:
                seen.append(url)
        return seen

    async def _human_delay(self) -> None:
        """Random sleep to mimic human reading behavior."""
        delay = random.uniform(
            self.settings.collect_min_delay_sec,
            self.settings.collect_max_delay_sec,
        )
        await asyncio.sleep(delay)

    def _is_within_active_hours(self) -> bool:
        """Check if current hour is within the active window."""
        from datetime import datetime as _dt
        hour = _dt.now().hour
        start = self.settings.collect_active_hours_start
        end = self.settings.collect_active_hours_end
        if start <= end:
            return start <= hour < end
        # Overnight window (e.g. 22 → 6)
        return hour >= start or hour < end

    async def handle_flood_wait(self, wait_seconds: int) -> None:
        """Respect Telegram's FloodWait by sleeping the requested duration + small jitter."""
        jitter = random.randint(1, 5)
        total = wait_seconds + jitter
        logger.warning(
            "FloodWait: sleeping {}s ({}s requested + {}s jitter) before retrying",
            total, wait_seconds, jitter,
        )
        await asyncio.sleep(total)

    def stop(self) -> None:
        """Signal the collector to stop after the current batch."""
        self._stop.set()


def make_collector() -> BaseCollector:
    """Factory: returns Mock or Telethon collector based on settings."""
    from .mock_collector import MockCollector
    from .telethon_collector import TelethonCollector

    settings = get_settings()
    if settings.tg_mock_mode or not settings.tg_api_id:
        logger.info("Using MockCollector (set TG_MOCK_MODE=false to use real Telegram)")
        return MockCollector()
    return TelethonCollector()
