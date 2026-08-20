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
  FIELD_ENCRYPTION_KEY    - encrypts the session string before it is stored
                            in TelegramAccount (app/crypto.py). Must match
                            whatever value any future consumer of that row
                            uses to decrypt it; changing it strands
                            previously-stored rows. Generate with:
                            python -c "from cryptography.fernet import \
                            Fernet; print(Fernet.generate_key().decode())"

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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.orm import Session  # noqa: E402
from telethon import TelegramClient  # noqa: E402
from telethon.errors import FloodWaitError, RPCError  # noqa: E402
from telethon.sessions import StringSession  # noqa: E402

from app.audit import record as audit_record  # noqa: E402
from app.crypto import InvalidToken, decrypt_field, encrypt_field  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.ingest import IngestSummary, ingest_text  # noqa: E402
from app.models import Channel, TelegramAccount  # noqa: E402

logger = logging.getLogger("collector")

DEFAULT_MESSAGE_LIMIT = 200


def _message_limit() -> int:
    """Read the per-channel scan limit at call time, not import time."""
    try:
        return max(1, int(os.environ.get("COLLECTOR_MESSAGE_LIMIT", DEFAULT_MESSAGE_LIMIT)))
    except ValueError:
        logger.warning("invalid COLLECTOR_MESSAGE_LIMIT, falling back to %d", DEFAULT_MESSAGE_LIMIT)
        return DEFAULT_MESSAGE_LIMIT


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


async def _collect_channel(client: TelegramClient, db: Session, channel: Channel) -> int:
    label = channel.username or channel.tg_channel_id
    try:
        entity = await client.get_entity(_entity_ref(channel))
    except (ValueError, TypeError, RPCError) as exc:
        logger.warning("skipping channel %s (%s): %s", channel.id, label, exc)
        return 0

    new_watermark = channel.last_message_id
    summary = IngestSummary()

    # reverse=True walks *forward* from the watermark (oldest unseen first).
    # Telethon's default is newest-first, which combined with a limit would
    # advance the watermark past messages that were never scanned, silently
    # dropping them on a busy channel. Ascending order keeps the watermark
    # contiguous, so a capped run simply resumes where it stopped next time.
    try:
        async for message in client.iter_messages(
            entity, min_id=channel.last_message_id, reverse=True, limit=_message_limit()
        ):
            ingest_text(
                db,
                workspace_id=channel.workspace_id,
                channel_id=channel.id,
                text=message.raw_text or "",
                message_id=message.id,
                posted_at=message.date.replace(tzinfo=None) if message.date else None,
                summary=summary,
            )
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
        detail=f"{summary.stored} new link(s)",
    )
    db.commit()
    logger.info(
        "channel %s (%s): %d new link(s), %d duplicate(s), watermark -> %d",
        channel.id,
        label,
        summary.stored,
        summary.duplicates,
        new_watermark,
    )
    return summary.stored


def _ensure_primary_account(db: Session, workspace_id: int, session_string: str) -> None:
    """Bootstrap the workspace's first account from the environment.

    Only the *first* account comes from ``TG_SESSION_STRING``; further
    accounts are registered with scripts/add_account.py, which is why this
    is a no-op once any account exists.
    """
    exists = db.query(TelegramAccount).filter(TelegramAccount.workspace_id == workspace_id).first()
    if exists is None:
        db.add(
            TelegramAccount(
                workspace_id=workspace_id, label="primary", session_string=encrypt_field(session_string)
            )
        )
        db.commit()


def _channels_for(db: Session, workspace_id: int, account: TelegramAccount, *, is_default: bool) -> list[Channel]:
    """Which channels this account is responsible for.

    A channel names its collecting account through ``account_id``. Channels
    that name nobody fall to the default account, so a single-account
    workspace keeps working exactly as before without anyone having to
    assign anything.
    """
    query = db.query(Channel).filter(Channel.workspace_id == workspace_id, Channel.is_active.is_(True))
    if is_default:
        return query.filter((Channel.account_id == account.id) | (Channel.account_id.is_(None))).all()
    return query.filter(Channel.account_id == account.id).all()


async def _collect_with_account(
    db: Session, account: TelegramAccount, channels: list[Channel], api_id: int, api_hash: str
) -> int:
    """Run one account's share of the channels. Never raises.

    Per-account isolation is the point: a revoked session, a banned
    account or a network failure on one account must not cost the run the
    channels every *other* account could still have collected.
    """
    try:
        session_string = decrypt_field(account.session_string)
    except InvalidToken:
        logger.error(
            "account %s (%s): session string could not be decrypted — wrong FIELD_ENCRYPTION_KEY, "
            "or the row predates encryption; skipping",
            account.id,
            account.label,
        )
        return 0

    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    try:
        await client.start()
    except Exception as exc:  # noqa: BLE001 - one bad account must not end the run
        logger.error("account %s (%s): cannot connect: %s", account.id, account.label, exc)
        return 0

    try:
        total = 0
        for channel in channels:
            total += await _collect_channel(client, db, channel)
        logger.info(
            "account %s (%s): %d new link(s) across %d channel(s)",
            account.id,
            account.label,
            total,
            len(channels),
        )
        return total
    except Exception as exc:  # noqa: BLE001 - same reasoning as above
        logger.error("account %s (%s): run aborted: %s", account.id, account.label, exc)
        return 0
    finally:
        await client.disconnect()


async def collect() -> None:
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    session_string = os.environ["TG_SESSION_STRING"]
    workspace_id = int(os.environ["COLLECTOR_WORKSPACE_ID"])

    db = SessionLocal()
    try:
        _ensure_primary_account(db, workspace_id, session_string)

        accounts = (
            db.query(TelegramAccount)
            .filter(TelegramAccount.workspace_id == workspace_id, TelegramAccount.is_active.is_(True))
            .order_by(TelegramAccount.id)
            .all()
        )
        if not accounts:
            logger.info("no active collecting accounts for this workspace; nothing to do")
            return

        # The lowest-numbered account is the default, and inherits every
        # channel that has not been assigned to a specific account.
        default_account_id = accounts[0].id

        total = 0
        collected_channels = 0
        for account in accounts:
            channels = _channels_for(db, workspace_id, account, is_default=account.id == default_account_id)
            if not channels:
                logger.info("account %s (%s): no channels assigned", account.id, account.label)
                continue
            collected_channels += len(channels)
            total += await _collect_with_account(db, account, channels, api_id, api_hash)

        if collected_channels == 0:
            logger.info("no active channels configured for this workspace; nothing to do")
            return
        logger.info(
            "done: %d new link(s) across %d channel(s) using %d account(s)",
            total,
            collected_channels,
            len(accounts),
        )
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
