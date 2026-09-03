"""What the runtime needs from Telegram, and nothing else.

A narrow interface, for two reasons that both matter more than elegance:

1. **The runtime's rules are testable without Telegram.** Ownership
   revalidation, watermark monotonicity, crash recovery and backpressure
   are properties of this system, not of Telethon, and proving them must
   not require credentials that CI does not have.
2. **What is unproven stays visible.** ``TelethonReader`` below is the
   only part of this package that talks to Telegram, and it is the only
   part no test exercises. Keeping it small and separate is what stops
   "the runtime is tested" from quietly meaning "talking to Telegram is
   tested" — it is not. See the phase report's UNVERIFIED section.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class IncomingMessage:
    """One message, reduced to what collection actually stores.

    Deliberately not a Telethon object: the runtime should not be able to
    reach for a field nobody decided to keep, and a frozen dataclass makes
    the storage contract visible in one place.
    """

    message_id: int
    text: str
    posted_at: datetime | None = None
    sender_id: str | None = None
    sender_username: str | None = None
    sender_name: str | None = None
    #: URLs that are in the message but not in its visible text.
    hidden_urls: tuple[str, ...] = ()
    #: URLs behind inline-keyboard buttons.
    button_urls: tuple[str, ...] = ()
    forwarded_from: str | None = None


class SourceReader(Protocol):
    """One account's connection to Telegram, as the runtime uses it."""

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    @property
    def is_connected(self) -> bool: ...

    def fetch(
        self, source_ref: str, *, min_id: int = 0, max_id: int | None = None, limit: int | None = None
    ) -> AsyncIterator[IncomingMessage]:
        """Messages after ``min_id``, oldest first.

        Declared without ``async`` on purpose: an implementation is an
        async *generator*, so calling it returns the iterator directly
        rather than a coroutine that has to be awaited first. Writing
        ``async def`` here would type the call as ``Coroutine[...,
        AsyncIterator]`` and make every ``async for`` over it a type
        error — which is how this was caught.

        Oldest-first is not a preference. Walking newest-first and taking
        the maximum id advances the watermark past messages that were
        never read on a busy channel, and they are then skipped by every
        future run — the exact silent gap this runtime exists to prevent.
        """
        ...

    def on_message(self, handler: Callable[[str, IncomingMessage], object]) -> None:
        """Register the live handler. Called with ``(source_ref, message)``."""
        ...
