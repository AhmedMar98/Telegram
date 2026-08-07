"""
Collect worker — runs the collector in a loop, persists new links to DB.

For each monitored channel:
    1. Read last_message_id from DB (or 0 if first run)
    2. Fetch new messages via collector
    3. Extract links + deduplicate by url_hash (SHA-256)
    4. Insert new links with status=NEW
    5. Update last_message_id
"""

from __future__ import annotations

import asyncio
import hashlib

from loguru import logger

from ..collectors.base import make_collector
from ..database import SessionLocal
from ..models import Account, Channel, Link, LinkStatus
from ..tenant import get_current_tenant
from ..timeutil import utcnow


def url_hash(url: str) -> str:
    """SHA-256 of normalized URL."""
    return hashlib.sha256(url.strip().lower().encode("utf-8")).hexdigest()


def _ensure_default_account(db) -> int:
    """Return the id of a usable Account row, creating a placeholder if none exist.

    Channel rows carry a FK to accounts.id; without this seed the very first
    call would fail with `FOREIGN KEY constraint failed`.
    """
    acc = db.query(Account).order_by(Account.id.asc()).first()
    if acc is not None:
        return acc.id
    acc = Account(
        name="main",
        api_id_enc="",
        api_hash_enc="",
        phone_enc="",
        is_active=True,
        is_main=True,
    )
    db.add(acc)
    db.commit()
    db.refresh(acc)
    logger.info("[collect] seeded default account id={}", acc.id)
    return acc.id


async def collect_once() -> int:
    """Run one collection cycle. Returns count of new links discovered."""
    settings_channels = _get_settings_channels()
    if not settings_channels:
        logger.warning("No channels to monitor. Set TG_MONITOR_CHANNELS in .env")
        return 0

    collector = make_collector()
    new_count = 0

    for channel_name in settings_channels:
        with SessionLocal() as db:
            # Upsert channel row — bind to the first available account.
            default_account_id = _ensure_default_account(db)
            ch = db.query(Channel).filter(Channel.channel_username == channel_name).first()
            if ch is None:
                ch = Channel(channel_username=channel_name, account_id=default_account_id)
                db.add(ch)
                db.commit()
            last_id = ch.last_message_id or 0

        try:
            async for link_ext in collector.collect_links(channel_name, last_message_id=last_id):
                with SessionLocal() as db:
                    # Check if link already exists
                    existing = (
                        db.query(Link).filter(Link.url_hash == url_hash(link_ext.url)).first()
                    )
                    if existing:
                        continue  # dedup

                    new_link = Link(
                        url=link_ext.url,
                        url_hash=url_hash(link_ext.url),
                        tenant_id=get_current_tenant().tenant_id,
                        title=None,
                        description=None,
                        category="unknown",
                        status=LinkStatus.NEW.value,
                        source_channel=channel_name,
                        source_message_id=link_ext.source_message_id,
                        discovered_at=link_ext.discovered_at or utcnow(),
                    )
                    db.add(new_link)
                    db.commit()
                    new_count += 1
                    # v4.1 metrics
                    try:
                        from ..web.metrics import record_link_collected

                        record_link_collected()
                    except Exception:
                        pass
                    logger.debug("[collect] new link #{}: {}", new_link.id, link_ext.url[:80])

                    # Update channel's last_message_id.
                    # (Channel has no last_seen_at column — the previous code
                    # set it as a plain Python attr and it was never persisted;
                    # dropped.)
                    ch = db.query(Channel).filter(Channel.channel_username == channel_name).first()
                    if ch and link_ext.source_message_id:
                        ch.last_message_id = max(
                            ch.last_message_id or 0, link_ext.source_message_id
                        )
                        db.commit()
        except Exception as e:
            logger.exception("[collect] error on channel {}: {}", channel_name, e)

    logger.info("[collect] cycle done: {} new links", new_count)
    return new_count


def _get_settings_channels() -> list[str]:
    from ..config import get_settings

    return get_settings().monitor_channels_list


async def run_loop(interval_sec: int = 60) -> None:
    """Run collect_once every interval_sec until interrupted."""
    logger.info("Collect worker started (interval={}s)", interval_sec)
    while True:
        try:
            await collect_once()
        except Exception as e:
            logger.exception("[collect] loop error: {}", e)
        await asyncio.sleep(interval_sec)
