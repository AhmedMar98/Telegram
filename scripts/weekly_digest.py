"""Weekly digest: what was collected, what died, and what went quiet.

Idea 151. Three numbers that are individually visible in the dashboard and
collectively invisible: nobody opens the dashboard weekly to compare this
week's count with last week's, and nobody notices a channel that stopped
posting — a channel that goes quiet looks exactly like a channel with
nothing to say.

    python scripts/weekly_digest.py --workspace 1

Off by default. A summary nobody asked for is the proactive sending that
phase 9's gate exists to prevent, so this fires only for a workspace that
switched ``weekly_digest`` on.

**Sent even when every number is zero**, for the same reason the backup
confirmation is sent on failure as well as success: a message that only
arrives when something happened cannot be told apart from a message that
stopped arriving. A quiet week is a fact worth one line.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app.alerts import WEEKLY_DIGEST  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import Channel, Link, Workspace  # noqa: E402
from app.notify import raise_alert  # noqa: E402
from app.timeutil import utcnow  # noqa: E402

logger = logging.getLogger("digest")

WINDOW_DAYS = 7

# A channel is "silent" only after twice the digest window. Plenty of
# channels post fortnightly, and flagging one of those every single week
# would train the reader to skip the section that matters.
SILENT_AFTER_DAYS = 14

# Enough to see the shape of the week without turning a digest into a feed.
TOP_CATEGORIES = 3
LISTED_CHANNELS = 5


@dataclass
class Digest:
    """What the last week did to a workspace's collection."""

    new_links: int = 0
    # Links whose vitality check turned up dead during the window. Named
    # for what actually happened: the check *confirmed* it, and the page
    # may well have gone months ago. Presenting a detection time as a
    # death time would be inventing a fact the data does not carry.
    confirmed_dead: int = 0
    top_categories: list[tuple[str, int]] = field(default_factory=list)
    silent_channels: list[str] = field(default_factory=list)
    active_channels: int = 0

    @property
    def is_quiet(self) -> bool:
        return self.new_links == 0 and self.confirmed_dead == 0 and not self.silent_channels


def _channel_label(channel: Channel) -> str:
    return channel.username or channel.title or channel.tg_channel_id


def build(db, workspace_id: int) -> Digest:
    """Read the week. Pure query work, so it is testable without sending."""
    now = utcnow()
    since = now - timedelta(days=WINDOW_DAYS)
    silent_before = now - timedelta(days=SILENT_AFTER_DAYS)

    digest = Digest()
    digest.new_links = (
        db.execute(
            select(func.count())
            .select_from(Link)
            .where(Link.workspace_id == workspace_id, Link.created_at >= since)
        ).scalar()
        or 0
    )
    digest.confirmed_dead = (
        db.execute(
            select(func.count())
            .select_from(Link)
            .where(
                Link.workspace_id == workspace_id,
                Link.is_alive.is_(False),
                Link.last_checked_at >= since,
            )
        ).scalar()
        or 0
    )
    digest.top_categories = [
        (category, count)
        for category, count in db.execute(
            select(Link.category, func.count())
            .where(Link.workspace_id == workspace_id, Link.created_at >= since)
            .group_by(Link.category)
            .order_by(func.count().desc())
            .limit(TOP_CATEGORIES)
        ).all()
    ]

    # A channel's own last link, compared against the silence threshold.
    # The manual bucket is excluded: it has no upstream to go quiet, so
    # "you have not pasted anything in a fortnight" is not news.
    from app.ingest import IMPORT_CHANNEL_PREFIX, MANUAL_CHANNEL_ID

    channels = db.query(Channel).filter(Channel.workspace_id == workspace_id, Channel.is_active.is_(True)).all()
    for channel in channels:
        if channel.tg_channel_id == MANUAL_CHANNEL_ID or channel.tg_channel_id.startswith(IMPORT_CHANNEL_PREFIX):
            continue
        digest.active_channels += 1
        newest = db.execute(select(func.max(Link.created_at)).where(Link.channel_id == channel.id)).scalar()
        # A channel that has never produced a link is silent only once it
        # has had the chance to: judged by when it was added, not skipped.
        reference = newest or channel.created_at
        if reference is not None and reference < silent_before:
            digest.silent_channels.append(_channel_label(channel))

    return digest


def render(digest: Digest) -> str:
    lines = [
        f"روابط جديدة خلال {WINDOW_DAYS} أيام: {digest.new_links}",
        f"روابط تأكّد موتها في فحوص هذا الأسبوع: {digest.confirmed_dead}",
    ]
    if digest.top_categories:
        lines += ["", "أكثر التصنيفات:", *(f"  • {name} — {count}" for name, count in digest.top_categories)]
    if digest.silent_channels:
        shown = digest.silent_channels[:LISTED_CHANNELS]
        remainder = len(digest.silent_channels) - len(shown)
        lines += [
            "",
            f"قنوات صامتة منذ أكثر من {SILENT_AFTER_DAYS} يوماً "
            f"({len(digest.silent_channels)} من {digest.active_channels}):",
            *(f"  • {label}" for label in shown),
            *([f"  و{remainder} غيرها."] if remainder > 0 else []),
        ]
    elif digest.active_channels:
        lines += ["", f"كل القنوات النشطة ({digest.active_channels}) ما تزال تنشر."]
    if digest.is_quiet:
        lines += ["", "أسبوع بلا تغيير — لا جديد ولا موت ولا صمت."]
    return "\n".join(lines)


async def run(workspace_id: int) -> Digest:
    db = SessionLocal()
    try:
        if db.get(Workspace, workspace_id) is None:
            sys.exit(f"error: no workspace with id {workspace_id}")

        digest = build(db, workspace_id)
        body = render(digest)
        logger.info("%s", body)
        await raise_alert(db, workspace_id, WEEKLY_DIGEST.key, title="🗓️ ملخّص الأسبوع", body=body)
        return digest
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", type=int, required=True, help="workspace id to summarise")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(run(args.workspace))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
