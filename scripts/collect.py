"""Scheduled Telegram collector.

Run as a one-shot script by .github/workflows/collector.yml on a cron
schedule — deliberately **not** a long-running process, because Render
has no free tier for a persistent background worker (see
docs/01-critical-analysis.md, Appendix C). GitHub Actions minutes are
free for public repos and budgeted (2,000 min/month) for private ones,
which comfortably covers a job that runs for well under a minute per
hour.

Required environment (set as GitHub Actions secrets):
  TG_API_ID              - from https://my.telegram.org
  TG_API_HASH            - from https://my.telegram.org
  TG_SESSION_STRING      - a Telethon StringSession for the collecting
                            account (generate once locally with
                            scripts/make_session_string.py, store only
                            in GitHub Secrets, never in the repo)
  DATABASE_URL            - the same Render Postgres URL the web service uses
  COLLECTOR_WORKSPACE_ID  - the workspace this collector feeds (see README)

Optional:
  COLLECTOR_MESSAGE_LIMIT - messages scanned per channel per run (default 200)
  GROQ_API_KEY             - enables the optional free LLM classification tier
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from telethon import TelegramClient  # noqa: E402
from telethon.errors import FloodWaitError  # noqa: E402
from telethon.sessions import StringSession  # noqa: E402
from telethon.tl.custom.message import Message  # noqa: E402

from app.audit import record as audit_record  # noqa: E402
from app.classifier import classify_link, extract_urls, hash_url  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import Channel, TelegramAccount  # noqa: E402
from app.models import Link as LinkModel  # noqa: E402

MESSAGE_LIMIT = int(os.environ.get("COLLECTOR_MESSAGE_LIMIT", "200"))


def _domain_of(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
    except ValueError:
        return url[:300]
    return (netloc[4:] if netloc.startswith("www.") else netloc) or url[:300]


async def _collect_channel(client: TelegramClient, db: Session, channel: Channel) -> int:
    entity_ref: str | int = channel.username or int(channel.tg_channel_id)
    try:
        entity = await client.get_entity(entity_ref)
    except (ValueError, FloodWaitError) as exc:
        print(f"[collector] skip channel {channel.id} ({channel.username or channel.tg_channel_id}): {exc}")
        return 0

    new_watermark = channel.last_message_id
    collected = 0

    message: Message
    async for message in client.iter_messages(entity, min_id=channel.last_message_id, limit=MESSAGE_LIMIT):
        new_watermark = max(new_watermark, message.id)
        text = message.raw_text or ""
        posted_at = message.date.replace(tzinfo=None) if message.date else None

        for url in extract_urls(text):
            result = classify_link(url, text)
            row = LinkModel(
                workspace_id=channel.workspace_id,
                channel_id=channel.id,
                message_id=message.id,
                url=url,
                url_hash=hash_url(url),
                domain=_domain_of(url),
                category=result.category,
                confidence=result.confidence,
                classified_by="llm" if result.matched_rule.startswith("llm") else "rules",
                raw_text=text[:2000],
                posted_at=posted_at,
            )
            db.add(row)
            try:
                db.flush()
                collected += 1
            except IntegrityError:
                db.rollback()  # already collected this URL for this channel

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
    print(f"[collector] channel {channel.id}: {collected} new link(s), watermark -> {new_watermark}")
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
            print("[collector] no active channels configured for this workspace; nothing to do")
            return

        client = TelegramClient(StringSession(session_string), api_id, api_hash)
        await client.start()
        try:
            total = 0
            for channel in channels:
                total += await _collect_channel(client, db, channel)
            print(f"[collector] done: {total} new link(s) across {len(channels)} channel(s)")
        finally:
            await client.disconnect()
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(collect())
