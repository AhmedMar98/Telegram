"""The real Telegram connection. **Not exercised by any test.**

Every other module in this package is proved by tests with a fake reader.
This one cannot be: it needs credentials, a real account and a real
network, none of which exist in CI. It is therefore deliberately thin —
translation and nothing else — so that the untested surface is as small as
the design allows, and so "the runtime is tested" never comes to mean
"Telegram is tested".

Its behaviour is UNVERIFIED until it runs against a real account.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

from app.runtime.protocol import IncomingMessage

logger = logging.getLogger(__name__)


def _urls_from_entities(message: Any) -> tuple[str, ...]:
    """Hyperlink targets that never appear in the visible text."""
    urls: list[str] = []
    for entity, _text in getattr(message, "get_entities_text", lambda: [])():
        url = getattr(entity, "url", None)
        if url:
            urls.append(url)
    return tuple(urls)


def _button_urls(message: Any) -> tuple[str, ...]:
    urls: list[str] = []
    for row in getattr(message, "buttons", None) or []:
        for button in row or []:
            url = getattr(button, "url", None)
            if url:
                urls.append(url)
    return tuple(urls)


def to_incoming(message: Any) -> IncomingMessage:
    """Translate a Telethon message into the runtime's own shape."""
    sender_id = getattr(message, "sender_id", None)
    forward = getattr(message, "forward", None)
    return IncomingMessage(
        message_id=int(getattr(message, "id", 0) or 0),
        text=getattr(message, "message", None) or getattr(message, "raw_text", None) or "",
        posted_at=getattr(message, "date", None),
        sender_id=str(sender_id) if sender_id is not None else None,
        sender_username=None,
        sender_name=None,
        hidden_urls=_urls_from_entities(message),
        button_urls=_button_urls(message),
        forwarded_from=str(getattr(forward, "from_name", None)) if forward is not None else None,
    )


class TelethonReader:
    """A ``SourceReader`` backed by a real Telethon client."""

    def __init__(self, session_string: str, api_id: int, api_hash: str) -> None:
        self._session_string = session_string
        self._api_id = api_id
        self._api_hash = api_hash
        self._client: Any = None
        self._handler: Callable[[str, IncomingMessage], object] | None = None

    async def connect(self) -> None:
        from telethon import TelegramClient, events
        from telethon.sessions import StringSession

        self._client = TelegramClient(StringSession(self._session_string), self._api_id, self._api_hash)
        await self._client.connect()

        if self._handler is not None:

            @self._client.on(events.NewMessage())
            async def _on_new(event: Any) -> None:  # pragma: no cover - needs Telegram
                chat_id = getattr(event, "chat_id", None)
                if chat_id is None or self._handler is None:
                    return
                self._handler(str(chat_id), to_incoming(event.message))

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

    @property
    def is_connected(self) -> bool:
        return bool(self._client is not None and self._client.is_connected())

    async def fetch(
        self, source_ref: str, *, min_id: int = 0, max_id: int | None = None, limit: int | None = None
    ) -> AsyncIterator[IncomingMessage]:
        if self._client is None:
            raise RuntimeError("fetch() before connect()")
        entity = await self._client.get_entity(int(source_ref) if source_ref.lstrip("-").isdigit() else source_ref)
        # ``reverse=True`` walks forward from the watermark, oldest first.
        # See SourceReader.fetch for why the other direction loses messages.
        # ``max_id=0`` is Telethon's "no upper bound".
        async for message in self._client.iter_messages(
            entity, min_id=min_id, max_id=max_id or 0, reverse=True, limit=limit
        ):
            yield to_incoming(message)

    def on_message(self, handler: Callable[[str, IncomingMessage], object]) -> None:
        self._handler = handler
