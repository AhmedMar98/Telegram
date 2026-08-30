"""Live Telegram collection — links land the second they are posted.

The scheduled collector (``scripts/collect.py``, hourly) answers *did we
get everything*. This module answers *how fast*. They are not
alternatives, and this one does not replace that one; the watermark note
below is the whole reason both exist.

Why this runs inside the web process
------------------------------------
Render has no free background-worker plan — that is the constraint that
made the collector a cron job in the first place (see ``render.yaml``).
But Render *does* have a free **web** service, and a web service is a
persistent process. So the listener runs as an asyncio task alongside
uvicorn, inside the one process the deployment already has. No second
service, no second bill, no Docker.

What that buys, and what it honestly costs:

- **Buys:** a link posted to a followed channel is stored in about a
  second, instead of waiting up to an hour for the next cron tick.
- **Costs:** the free web instance sleeps after roughly fifteen minutes
  with no inbound HTTP request, and a sleeping process listens to
  nothing. Live collection is only as continuous as whatever keeps the
  instance awake. ``.github/workflows/keepalive.yml`` is that whatever,
  and ``docs/42-live-collection.md`` states its limits rather than
  implying there are none.

The watermark is deliberately NOT advanced here
-----------------------------------------------
``Channel.last_message_id`` stays owned by the scheduled collector alone.

Advancing it from here is tempting — this path has just seen the newest
message, after all — and it is the one change that could lose data
permanently. Telethon's update stream can gap: a dropped socket, an
update the server never pushed, a handler that raised. If this path set
the watermark to message 5000 having never seen 4998, the cron's
``min_id=last_message_id`` scan would begin above 4998 and that message
would never be read by anything, ever. The loss would be silent and
undetectable, which is the worst property a data loss can have.

Leaving the watermark alone means the cron re-scans ground this path
already covered. That re-scan is cheap, and it is the entire point: the
unique constraint on ``(channel_id, url_hash)`` turns the overlap into
duplicates that are counted and dropped. Belt and braces only work if
they are allowed to overlap.

Telethon is an optional import
------------------------------
``requirements.txt`` ships it, but the import is still guarded. A
deployment that installed the older, leaner requirement set must keep
booting and serving — with live collection off and a log line saying so —
rather than crash-looping on an ImportError at startup.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.config import get_settings
from app.database import SessionLocal
from app.ingest import IngestSummary, ingest_text
from app.models import Channel, TelegramAccount
from app.rls import scope_session_to_workspace

logger = logging.getLogger("live")

# How long a built channel index is trusted before it is rebuilt. A
# channel added on the dashboard starts being watched within this long,
# with no restart. One small query a minute is the price.
INDEX_TTL_SECONDS = 60.0

# Reconnect backoff. Starts fast because most disconnects are a blip, and
# caps well below the free instance's idle timeout so a recovering
# listener never sits out longer than the process is likely to live.
RECONNECT_MIN_SECONDS = 5.0
RECONNECT_MAX_SECONDS = 300.0


# --- observable state -------------------------------------------------------
#
# A listener nobody can see is indistinguishable from a listener that
# died at 3am. Every field here exists to answer one operator question
# from the status endpoint without reading a log.


@dataclass
class LiveState:
    enabled: bool = False
    connected: bool = False
    reason: str | None = None
    channels_watched: int = 0
    messages_seen: int = 0
    links_stored: int = 0
    last_event_at: datetime | None = None
    last_error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "connected": self.connected,
            "reason": self.reason,
            "channels_watched": self.channels_watched,
            "messages_seen": self.messages_seen,
            "links_stored": self.links_stored,
            "last_event_at": self.last_event_at,
            "last_error": self.last_error,
        }


_state = LiveState()


def state() -> LiveState:
    return _state


# --- which channel is this message from? ------------------------------------


def canonical_id(raw: object) -> str | None:
    """One spelling for a Telegram peer id, whichever form it arrived in.

    Telethon reports a channel as ``-1001234567890``; an operator typing
    the id into the dashboard usually pastes ``1234567890``. Both have to
    match the same row.

    The ``-100`` prefix is stripped **only from negative ids**, because
    that minus sign is the marker that the prefix is a peer-type tag
    rather than part of the number. Stripping a leading ``100`` from
    positive ids too would be the plausible-looking version of this
    function that is wrong: a channel genuinely numbered ``1001234``
    would canonicalise to ``1234`` from the dashboard and to ``1001234``
    from Telethon, and would silently never match.
    """
    try:
        text = str(int(str(raw).strip()))
    except (TypeError, ValueError):
        return None
    if text.startswith("-100"):
        return text[4:]
    if text.startswith("-"):
        return text[1:]
    return text


def canonical_username(raw: object) -> str | None:
    """One spelling for a @handle. Telegram handles are case-insensitive."""
    if not isinstance(raw, str):
        return None
    handle = raw.strip().lstrip("@").lower()
    return handle or None


def build_index(channels: list[Channel]) -> dict[str, int]:
    """Every name a watched channel answers to -> its row id.

    Both identities are indexed for every channel, because which one an
    incoming event carries is not ours to decide: a message from a public
    channel usually resolves a username, one from a private channel never
    does.
    """
    index: dict[str, int] = {}
    for channel in channels:
        key = canonical_id(channel.tg_channel_id)
        if key:
            index[f"id:{key}"] = channel.id
        handle = canonical_username(channel.username)
        if handle:
            index[f"@{handle}"] = channel.id
    return index


def lookup(index: dict[str, int], chat_id: object, username: object) -> int | None:
    key = canonical_id(chat_id)
    if key and f"id:{key}" in index:
        return index[f"id:{key}"]
    handle = canonical_username(username)
    if handle:
        return index.get(f"@{handle}")
    return None


# --- storing what arrived ---------------------------------------------------


def store_message(
    workspace_id: int,
    channel_id: int,
    *,
    text: str,
    message_id: int,
    posted_at: datetime | None,
    button_urls: list[str],
    forwarded_from: str | None,
) -> IngestSummary:
    """Ingest one live message. Synchronous, and called in a thread.

    SQLAlchemy here is blocking, and this process is also serving HTTP on
    the same event loop with a single worker. Calling it inline would
    stall every in-flight request for the duration of a classification —
    so the caller hands it to ``asyncio.to_thread``, and this function
    owns a session of its own rather than borrowing one across a thread
    boundary, which SQLAlchemy does not support.

    Note what is absent: no write to ``Channel.last_message_id``. See the
    module docstring; that omission is the design, not an oversight.
    """
    db = SessionLocal()
    try:
        # Every table touched below is under row-level security. Without
        # this the insert is refused and the select returns nothing —
        # quietly, which is how a live listener ends up looking healthy
        # while storing zero links (see app/rls.py).
        scope_session_to_workspace(db, workspace_id)
        summary = ingest_text(
            db,
            workspace_id=workspace_id,
            channel_id=channel_id,
            text=text,
            message_id=message_id,
            posted_at=posted_at,
            button_urls=button_urls,
            forwarded_from=forwarded_from,
        )
        db.commit()
        return summary
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def _announce_adult(workspace_id: int, urls: list[str]) -> None:
    """Same alert the batch collector raises, at live granularity.

    Deliberately after the commit and in its own session: an alert about
    links a rollback took back would be a lie, and the notification path
    must never be able to fail the ingestion that triggered it.
    """
    if not urls:
        return
    from app.notify import report_adult_links

    db = SessionLocal()
    try:
        scope_session_to_workspace(db, workspace_id)
        await report_adult_links(db, workspace_id, urls)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - a failed notice is not a failed ingest
        db.rollback()
        logger.warning("live: could not raise the adult-content notice: %s", exc)
    finally:
        db.close()


# --- the listener itself ----------------------------------------------------


@dataclass
class _Index:
    """A channel index with an expiry, rebuilt on demand."""

    workspace_id: int
    mapping: dict[str, int] = field(default_factory=dict)
    built_at: float = 0.0

    def _load(self) -> dict[str, int]:
        db = SessionLocal()
        try:
            scope_session_to_workspace(db, self.workspace_id)
            channels = (
                db.query(Channel)
                .filter(Channel.workspace_id == self.workspace_id, Channel.is_active.is_(True))
                .order_by(Channel.id)
                .all()
            )
            return build_index(channels)
        finally:
            db.close()

    async def get(self) -> dict[str, int]:
        loop = asyncio.get_running_loop()
        if loop.time() - self.built_at >= INDEX_TTL_SECONDS:
            self.mapping = await asyncio.to_thread(self._load)
            self.built_at = loop.time()
            _state.channels_watched = len(set(self.mapping.values()))
        return self.mapping


def _button_urls(message: object) -> list[str]:
    """Inline-keyboard link targets — same shape the batch collector reads.

    Channels routinely put the actual download link on a button and leave
    the body as marketing copy, so a listener that reads only the text
    misses exactly the link the post exists to share.
    """
    markup = getattr(message, "reply_markup", None)
    urls: list[str] = []
    for row in getattr(markup, "rows", None) or []:
        for button in getattr(row, "buttons", None) or []:
            url = getattr(button, "url", None)
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                urls.append(url)
    return urls


def _forward_origin(message: object) -> str | None:
    forward = getattr(message, "forward", None)
    if forward is None:
        return None
    name = getattr(forward, "from_name", None)
    if isinstance(name, str) and name:
        return name
    chat = getattr(forward, "chat", None) or getattr(forward, "sender", None)
    for attribute in ("title", "username", "first_name"):
        value = getattr(chat, attribute, None)
        if isinstance(value, str) and value:
            return value
    return None


async def handle_event(event: Any, index: _Index) -> int:
    """One incoming message. Returns links stored; never raises.

    Never raises because Telethon runs handlers inside its own update
    loop: an exception escaping here is logged by the library and the
    connection carries on, but the failure would be invisible on the
    status board. Catching it here is what makes it countable.
    """
    try:
        message = getattr(event, "message", event)
        chat = getattr(event, "chat", None)
        channel_id = lookup(await index.get(), getattr(event, "chat_id", None), getattr(chat, "username", None))
        if channel_id is None:
            # Traffic from a chat this workspace does not follow. The
            # account is a real Telegram login with its own DMs and
            # groups; most of what it hears is not ours to store.
            return 0

        _state.messages_seen += 1
        _state.last_event_at = datetime.now(UTC)

        posted = getattr(message, "date", None)
        summary = await asyncio.to_thread(
            store_message,
            index.workspace_id,
            channel_id,
            text=getattr(message, "raw_text", None) or "",
            message_id=int(getattr(message, "id", 0) or 0),
            posted_at=posted.replace(tzinfo=None) if isinstance(posted, datetime) else None,
            button_urls=_button_urls(message),
            forwarded_from=_forward_origin(message),
        )
        _state.links_stored += summary.stored
        if summary.stored:
            logger.info("live: channel %s -> %d new link(s)", channel_id, summary.stored)
        await _announce_adult(index.workspace_id, summary.adult_urls)
        return summary.stored
    except Exception as exc:  # noqa: BLE001 - see docstring
        _state.last_error = f"{type(exc).__name__}: {exc}"
        logger.exception("live: failed to handle an update")
        return 0


def _session_string(workspace_id: int) -> str | None:
    """The collecting account's session, preferring the stored one.

    Order matters. The database row is authoritative because that is what
    ``scripts/add_account.py`` writes and what the dashboard shows; the
    environment variable is only the bootstrap for a workspace that has
    never registered an account. Reading the environment first would mean
    a re-authorised account in the database was silently ignored in
    favour of the revoked string still sitting in Render's config.
    """
    from app.crypto import InvalidToken, decrypt_field

    db = SessionLocal()
    try:
        scope_session_to_workspace(db, workspace_id)
        account = (
            db.query(TelegramAccount)
            .filter(TelegramAccount.workspace_id == workspace_id, TelegramAccount.is_active.is_(True))
            .order_by(TelegramAccount.id)
            .first()
        )
        if account is not None:
            try:
                return decrypt_field(account.session_string)
            except InvalidToken:
                logger.error(
                    "live: account %s could not be decrypted — FIELD_ENCRYPTION_KEY does not match "
                    "the value that stored it; falling back to TG_SESSION_STRING",
                    account.id,
                )
    except Exception as exc:  # noqa: BLE001 - a cold database must not stop the fallback
        logger.warning("live: could not read a stored account (%s); using the environment", exc)
    finally:
        db.close()

    return os.environ.get("TG_SESSION_STRING", "").strip() or None


async def _serve_once(workspace_id: int, api_id: int, api_hash: str) -> None:
    """Connect, listen until the connection drops, disconnect cleanly."""
    from telethon import TelegramClient, events
    from telethon.sessions import StringSession

    session = await asyncio.to_thread(_session_string, workspace_id)
    if not session:
        raise RuntimeError(
            "no collecting account: set TG_SESSION_STRING or register one with scripts/add_account.py"
        )

    index = _Index(workspace_id=workspace_id)
    client = TelegramClient(StringSession(session), api_id, api_hash)

    async def _on_message(event: Any) -> None:
        await handle_event(event, index)

    await client.connect()
    try:
        if not await client.is_user_authorized():
            # A revoked or expired session. Retrying on a timer would
            # never fix it and would look like a network problem in the
            # logs, so it is named for what it is.
            raise RuntimeError("the collecting session is not authorised — re-authorise the account")

        client.add_event_handler(_on_message, events.NewMessage())
        # Prime the index before announcing readiness, so the first
        # message after startup is matched rather than dropped while a
        # lazy first build is still running.
        await index.get()
        _state.connected = True
        _state.reason = None
        _state.last_error = None
        logger.info("live: listening on %d channel(s)", _state.channels_watched)
        await client.run_until_disconnected()
    finally:
        _state.connected = False
        with contextlib.suppress(Exception):
            await client.disconnect()


async def run_forever(workspace_id: int, api_id: int, api_hash: str) -> None:
    """Keep a listener up, with backoff, until the task is cancelled."""
    delay = RECONNECT_MIN_SECONDS
    while True:
        try:
            await _serve_once(workspace_id, api_id, api_hash)
            # A clean return means Telegram dropped us. That is routine;
            # reconnect promptly rather than backing off as if it were a
            # fault.
            delay = RECONNECT_MIN_SECONDS
            logger.info("live: disconnected, reconnecting in %.0fs", delay)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the web service must survive any of it
            _state.last_error = f"{type(exc).__name__}: {exc}"
            logger.error("live: listener stopped (%s); retrying in %.0fs", exc, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, RECONNECT_MAX_SECONDS)


# --- startup wiring ---------------------------------------------------------


def _configuration() -> tuple[int, int, str] | None:
    """The three values a listener cannot start without, or None.

    Returns rather than raises, and records *why* on the state object:
    "live collection is off" and "live collection is misconfigured" look
    identical from the dashboard otherwise, and only one of them is
    something the operator should go and fix.
    """
    settings = get_settings()
    if not settings.live_collector_enabled:
        _state.reason = "off: LIVE_COLLECTOR_ENABLED is not set"
        return None

    raw_api_id = os.environ.get("TG_API_ID", "").strip()
    api_hash = os.environ.get("TG_API_HASH", "").strip()
    raw_workspace = os.environ.get("COLLECTOR_WORKSPACE_ID", "").strip()

    missing = [
        name
        for name, value in (
            ("TG_API_ID", raw_api_id),
            ("TG_API_HASH", api_hash),
            ("COLLECTOR_WORKSPACE_ID", raw_workspace),
        )
        if not value
    ]
    if missing:
        _state.reason = f"misconfigured: {', '.join(missing)} not set"
        return None

    try:
        return int(raw_workspace), int(raw_api_id), api_hash
    except ValueError:
        _state.reason = "misconfigured: TG_API_ID and COLLECTOR_WORKSPACE_ID must be numbers"
        return None


def start() -> asyncio.Task[None] | None:
    """Spawn the listener if it is configured. Returns the task, or None.

    Called from the FastAPI lifespan. Every failure path here returns
    None instead of raising: the web service refusing to boot because
    Telegram is unreachable would trade a delayed link for a dead site.
    """
    _state.enabled = False
    configuration = _configuration()
    if configuration is None:
        logger.info("live collection not started (%s)", _state.reason)
        return None

    try:
        import telethon  # noqa: F401
    except ImportError:
        _state.reason = "telethon is not installed in this environment"
        logger.warning("live collection not started: %s", _state.reason)
        return None

    workspace_id, api_id, api_hash = configuration
    _state.enabled = True
    _state.reason = None
    logger.info("live collection starting for workspace %d", workspace_id)
    return asyncio.create_task(run_forever(workspace_id, api_id, api_hash), name="live-collector")


async def stop(task: asyncio.Task[None] | None) -> None:
    """Cancel the listener and wait for it, so shutdown is not noisy."""
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    _state.connected = False
    _state.enabled = False
