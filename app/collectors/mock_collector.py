"""
Mock collector — generates synthetic links for local development.

Useful for testing the full pipeline (classifier → vitality → search → web)
without a real Telegram account. Set TG_MOCK_MODE=true in .env to activate.
"""
from __future__ import annotations

import asyncio
import random
import string
from datetime import datetime, timedelta
from typing import AsyncIterator

from loguru import logger

from .base import BaseCollector, CollectedMessage

# Sample templates for realistic test data
_SAMPLE_TEMPLATES = [
    "https://t.me/joinchat/ABC{rand} group invite — premium movies channel 🎬",
    "https://chat.whatsapp.com/{rand} join our study circle 📚",
    "https://t.me/{word}_{rand} daily news updates 📰",
    "Check this tool: https://github.com/user/{word}-{rand} 👀",
    "https://wa.me/{phone}?text=hello direct WhatsApp link",
    "Free file: https://example.com/files/{word}_{rand}.pdf 📁",
    "https://t.me/{word} channel — settings & config 📱",
    "https://example.org/news/{word}-{rand} breaking story 🚨",
    "Setting up VPN: https://example.com/vpn-{rand} guide",
    "https://t.me/+{rand} private invite (limited slots)",
]

_WORDS = ["tech", "news", "movies", "study", "code", "design", "ai", "crypto",
          "python", "telegram", "social", "media", "free", "pro", "vip"]


def _rand_token(n: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def _rand_phone() -> str:
    return "1" + "".join(random.choices(string.digits, k=10))


class MockCollector(BaseCollector):
    """Synthetic link generator for development."""

    name = "mock"

    async def fetch_messages(
        self, channel: str, last_message_id: int | None = None
    ) -> AsyncIterator[CollectedMessage]:
        """Generate 5-15 fake messages per call."""
        count = random.randint(5, 15)
        start_id = (last_message_id or 0) + 1
        logger.info("[Mock] Generating {} messages for channel '{}' (start_id={})",
                    count, channel, start_id)

        for i in range(count):
            if self._stop.is_set():
                break
            template = random.choice(_SAMPLE_TEMPLATES)
            text = template.format(
                rand=_rand_token(),
                word=random.choice(_WORDS),
                phone=_rand_phone(),
            )
            msg = CollectedMessage(
                channel=channel,
                message_id=start_id + i,
                text=text,
                timestamp=datetime.utcnow() - timedelta(minutes=i * 3),
                has_media=random.random() > 0.7,
            )
            # Simulate network latency
            await asyncio.sleep(random.uniform(0.05, 0.2))
            yield msg
