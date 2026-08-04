"""
Real Telegram userbot collector based on Telethon.

IMPORTANT — Anti-spam guarantees:
    1. This collector NEVER sends messages to other users' groups/channels.
    2. It only READS messages from configured monitored channels.
    3. FloodWait is respected strictly (sleeps the requested seconds + jitter).
    4. Only the Main account may create groups (max 3) — enforced in the web layer.

Setup:
    1. Get api_id + api_hash from https://my.telegram.org → API development tools
    2. Fill TG_API_ID, TG_API_HASH, TG_PHONE in .env
    3. Set TG_MOCK_MODE=false
    4. First run: Telethon will prompt for a login code sent to your Telegram.
       After successful login, a <session_name>.session file is created and reused.
"""
from __future__ import annotations

from datetime import datetime
from typing import AsyncIterator, Optional

from loguru import logger

from .base import BaseCollector, CollectedMessage


class TelethonCollector(BaseCollector):
    """Real Telethon-based collector."""

    name = "telethon"

    def __init__(self) -> None:
        super().__init__()
        self._client = None
        self._started = False

    async def _ensure_client(self):
        """Lazy-import and start Telethon client (avoids import errors when mock mode)."""
        if self._client is not None:
            return self._client

        try:
            from telethon import TelegramClient
            from telethon.errors import FloodWaitError
        except ImportError as e:
            raise RuntimeError(
                "telethon not installed. Run: pip install telethon cryptg"
            ) from e

        s = self.settings
        if not s.tg_api_id or not s.tg_api_hash:
            raise RuntimeError(
                "TG_API_ID and TG_API_HASH must be set when TG_MOCK_MODE=false. "
                "Get them from https://my.telegram.org"
            )

        self._client = TelegramClient(
            s.tg_session_name,
            int(s.tg_api_id),
            s.tg_api_hash,
        )

        # Bind FloodWait handler
        self._FloodWaitError = FloodWaitError

        if not self._started:
            logger.info("Starting Telethon client (session={}, phone={})",
                        s.tg_session_name, s.tg_phone)
            await self._client.start(phone=s.tg_phone)
            self._started = True
            me = await self._client.get_me()
            logger.info("Telethon login OK: id={}, name={}, username={}",
                        me.id,
                        f"{me.first_name or ''} {me.last_name or ''}".strip(),
                        me.username)
        return self._client

    async def fetch_messages(
        self, channel: str, last_message_id: Optional[int] = None
    ) -> AsyncIterator[CollectedMessage]:
        """
        Iterate messages from `channel` (username or numeric ID) newer than last_message_id.

        Respects FloodWait and applies human-like pacing.
        """
        client = await self._ensure_client()

        # Resolve entity
        try:
            entity = await client.get_entity(channel)
        except Exception as e:
            logger.error("Cannot resolve channel '{}': {}", channel, e)
            return

        logger.info("Fetching messages from '{}' (last_message_id={})", channel, last_message_id)

        try:
            async for msg in client.iter_messages(
                entity,
                min_id=last_message_id or 0,
                limit=50,
                reverse=True,  # oldest first so min_id filtering makes sense
            ):
                if self._stop.is_set():
                    break
                if msg.text is None and not msg.media:
                    continue
                yield CollectedMessage(
                    channel=channel,
                    message_id=msg.id,
                    text=msg.text or "",
                    timestamp=msg.date.replace(tzinfo=None),
                    has_media=msg.media is not None,
                )
        except self._FloodWaitError as fw:  # type: ignore
            await self.handle_flood_wait(fw.seconds)
            # Retry once after flood wait
            async for msg in client.iter_messages(entity, min_id=last_message_id or 0, limit=50):
                if self._stop.is_set():
                    break
                if msg.text is None and not msg.media:
                    continue
                yield CollectedMessage(
                    channel=channel,
                    message_id=msg.id,
                    text=msg.text or "",
                    timestamp=msg.date.replace(tzinfo=None),
                    has_media=msg.media is not None,
                )
        except Exception as e:
            logger.exception("Error fetching from '{}': {}", channel, e)

    async def close(self) -> None:
        if self._client and self._started:
            await self._client.disconnect()
            self._started = False
            logger.info("Telethon client disconnected")
