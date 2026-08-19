"""Scheduled Telegram collector.

Run as a one-shot script by .github/workflows/collector.yml on a cron
schedule — deliberately **not** a long-running process, because Render
has no free tier for a persistent background worker (see
docs/01-critical-analysis.md, Appendix C). GitHub Actions minutes are
free for public repos and budgeted (2,000 min/month) for private ones,
which comfortably covers a job that runs for well under a minute per
hour.

Required environment (set as GitHub Actions secrets):
  TG_API_ID               - from https://my.telegram.org
  TG_API_HASH             - from https://my.telegram.org
  TG_SESSION_STRING       - a Telethon StringSession for the collecting
                            account (generate once locally with
                            scripts/make_session_string.py, store only
                            in GitHub Secrets, never in the repo)
  DATABASE_URL            - the same Render Postgres URL the web service uses
  COLLECTOR_WORKSPACE_ID  - the workspace this collector feeds (see README)

Optional:
  COLLECTOR_MESSAGE_LIMIT - messages scanned per channel per run (default 200)
  GROQ_API_KEY            - enables the optional free LLM classification tier
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from telethon import TelegramClient  # noqa: E402
from telethon.errors import FloodWaitError, RPCError  # noqa: E402
from telethon.sessions import StringSession  # noqa: E402

from app.audit import record as audit_record  # noqa: E402
from app.classifier import classify_link, extract_urls, hash_url  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import Channel, TelegramAccount  # noqa: E402
from app.models import Link as LinkModel  # noqa: E402

logger = logging.getLogger("collector")

DEFAULT_MESSAGE_LIMIT = 200


def _message_limit() -> int:
    """Read the per-channel scan limit at call time, not import time."""
    try:
        return max(1, int(os.environ.get("COLLECTOR_MESSAGE_LIMIT", DEFAULT_MESSAGE_LIMIT)))
    except ValueError:
        logger.warning("invalid COLLECTOR_MESSAGE_LIMIT, falling back to %d", DEFAULT_MESSAGE_LIMIT)
        return DEFAULT_MESSAGE_LIMIT


def _domain_of(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
    except ValueError:
        return url[:300]
    return (netloc[4:] if netloc.startswith("www.") else netloc) or url[:300]


def _entity_ref(channel: Channel) -> str | int:
    """Resolve how to address a channel, preferring its @username.

    ``tg_channel_id`` is free text on the model because a user may type
    either a numeric id or a handle, so the int() conversion has to be
    guarded: an unconvertible value is a bad row, not a reason to abort
    the entire collection run.
    """
    if channel.username:
        return channel.username
    return int(channel.tg_channel_id)


def _store_link(db: Session, channel: Channel, message_id: int, url: str, text: str, posted_at) -> bool:
    """Insert one link, returning whether it was new.

    The insert runs inside a SAVEPOINT so that hitting the
    (channel_id, url_hash) uniqueness constraint rolls back *only this
    row*. A plain ``db.rollback()`` here would discard the entire
    channel's uncommitted work — every link collected so far in this run
    plus the watermark update — turning a routine duplicate into silent
    data loss.
    """
    result = classify_link(url, text)
    row = LinkModel(
        workspace_id=channel.workspace_id,
        channel_id=channel.id,
        message_id=message_id,
        url=url,
        url_hash=hash_url(url),
        domain=_domain_of(url),
        category=result.category,
        confidence=result.confidence,
        classified_by="llm" if result.matched_rule.startswith("llm") else "rules",
        raw_text=text[:2000],
        posted_at=posted_at,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        return False  # already collected this URL for this channel
    return True


async def _collect_channel(client: TelegramClient, db: Session, channel: Channel) -> int:
    label = channel.username or channel.tg_channel_id
    try:
        entity = await client.get_entity(_entity_ref(channel))
    except (ValueError, TypeError, RPCError) as exc:
        logger.warning("skipping channel %s (%s): %s", channel.id, label, exc)
        return 0

    new_watermark = channel.last_message_id
    collected = 0

    # reverse=True walks *forward* from the watermark (oldest unseen first).
    # Telethon's default is newest-first, which combined with a limit would
    # advance the watermark past messages that were never scanned, silently
    # dropping them on a busy channel. Ascending order keeps the watermark
    # contiguous, so a capped run simply resumes where it stopped next time.
    try:
        async for message in client.iter_messages(
            entity, min_id=channel.last_message_id, reverse=True, limit=_message_limit()
        ):
            text = message.raw_text or ""
            posted_at = message.date.replace(tzinfo=None) if message.date else None

            for url in extract_urls(text):
                if _store_link(db, channel, message.id, url, text, posted_at):
                    collected += 1

            new_watermark = max(new_watermark, message.id)
    except FloodWaitError as exc:
        # Keep whatever was collected before the rate limit and resume from
        # the contiguous watermark on the next scheduled run.
        logger.warning("flood wait on channel %s (%s): %s", channel.id, label, exc)

    channel.last_message_id = new_watermark
    audit_record(
        db,
        workspace_id=channel.workspace_id,
        user_id=None,
        action="collector.run",
        target_type="channel",
        target_id=str(channel.id),
        detail=f"{collected} new link(s)",
    )
    db.commit()
    logger.info("channel %s (%s): %d new link(s), watermark -> %d", channel.id, label, collected, new_watermark)
    return collected


async def collect() -> None:
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    session_string = os.environ["TG_SESSION_STRING"]
    workspace_id = int(os.environ["COLLECTOR_WORKSPACE_ID"])

    db = SessionLocal()
    try:
        account = (
            db.query(TelegramAccount)
            .filter(TelegramAccount.workspace_id == workspace_id, TelegramAccount.is_active.is_(True))
            .first()
        )
        if account is None:
            account = TelegramAccount(workspace_id=workspace_id, label="primary", session_string=session_string)
            db.add(account)
            db.commit()

        channels = (
            db.query(Channel).filter(Channel.workspace_id == workspace_id, Channel.is_active.is_(True)).all()
        )
        if not channels:
            logger.info("no active channels configured for this workspace; nothing to do")
            return

        client = TelegramClient(StringSession(session_string), api_id, api_hash)
        await client.start()
        try:
            total = 0
            for channel in channels:
                total += await _collect_channel(client, db, channel)
            logger.info("done: %d new link(s) across %d channel(s)", total, len(channels))
        finally:
            await client.disconnect()
    finally:
        db.close()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    asyncio.run(collect())


if __name__ == "__main__":
    main()
