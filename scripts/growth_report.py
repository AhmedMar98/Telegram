"""Monthly report: how big the database is, and how fast it is growing.

Idea 186. Size alone answers "am I near the limit"; size *plus* growth
answers "when will I be", which is the question worth a monthly message.

    python scripts/growth_report.py --workspace 1

Delivered through the notification system, so it obeys the same switch as
every other alert (it is off by default — a monthly summary is proactive
sending, and phase 9a's default policy says so).

The projection is arithmetic on measured rows, not a model: it takes the
last two months of link counts and says what that rate implies. When there
is not enough history to say anything, it says that instead of
extrapolating from one point.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app.alerts import MONTHLY_DOMAINS  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import Link, Workspace  # noqa: E402
from app.notify import raise_alert  # noqa: E402
from app.storage import database_bytes  # noqa: E402
from app.timeutil import utcnow  # noqa: E402

logger = logging.getLogger("growth")


def _human(size: int | None) -> str:
    if size is None:
        return "غير متاح"
    megabytes = size / (1024 * 1024)
    if megabytes >= 1024:
        return f"{megabytes / 1024:.2f} غيغابايت"
    return f"{megabytes:.0f} ميغابايت"


def _counts(db, workspace_id: int) -> tuple[int, int, int]:
    """Total links, plus the last two 30-day windows."""
    now = utcnow()
    total = (
        db.execute(select(func.count()).select_from(Link).where(Link.workspace_id == workspace_id)).scalar() or 0
    )
    this_month = (
        db.execute(
            select(func.count())
            .select_from(Link)
            .where(Link.workspace_id == workspace_id, Link.created_at >= now - timedelta(days=30))
        ).scalar()
        or 0
    )
    previous_month = (
        db.execute(
            select(func.count())
            .select_from(Link)
            .where(
                Link.workspace_id == workspace_id,
                Link.created_at >= now - timedelta(days=60),
                Link.created_at < now - timedelta(days=30),
            )
        ).scalar()
        or 0
    )
    return total, this_month, previous_month


def _top_domains(db, workspace_id: int, limit: int = 10) -> list[tuple[str, int]]:
    rows = db.execute(
        select(Link.domain, func.count())
        .where(Link.workspace_id == workspace_id, Link.created_at >= utcnow() - timedelta(days=30))
        .group_by(Link.domain)
        .order_by(func.count().desc())
        .limit(limit)
    ).all()
    return [(domain, count) for domain, count in rows]


def _projection(used: int | None, total_links: int, this_month: int, limit: int) -> str:
    """Months until the configured limit, or an honest refusal to guess."""
    if used is None:
        return "حجم القاعدة غير مقروء على هذا المحرّك، فلا إسقاط."
    if this_month == 0 or total_links == 0:
        return "لا نموّ هذا الشهر، فلا إسقاط."

    bytes_per_link = used / total_links
    monthly_growth = bytes_per_link * this_month
    remaining = limit - used
    if remaining <= 0:
        return "الحدّ المضبوط تجاوزته القاعدة بالفعل."
    months = remaining / monthly_growth
    if months > 120:
        # Ten years is not a forecast, it is noise.
        return "بالمعدّل الحالي، الحدّ بعيد بما يتجاوز عشر سنوات."
    return f"بالمعدّل الحالي، يُبلَغ الحدّ المضبوط بعد نحو {months:.0f} شهراً."


async def run(workspace_id: int) -> None:
    settings = get_settings()
    db = SessionLocal()
    try:
        if db.get(Workspace, workspace_id) is None:
            sys.exit(f"error: no workspace with id {workspace_id}")

        used = database_bytes(db)
        total, this_month, previous_month = _counts(db, workspace_id)
        domains = _top_domains(db, workspace_id)

        if previous_month:
            change = (this_month - previous_month) / previous_month * 100
            trend = f"{change:+.0f}٪ مقارنة بالشهر السابق"
        else:
            # One data point is not a trend, and printing a percentage
            # from it would be inventing one.
            trend = "لا شهر سابق للمقارنة"

        body = "\n".join(
            [
                f"الحجم: {_human(used)} من حدّ مضبوط قدره {_human(settings.storage_limit_bytes)}",
                f"الروابط: {total} إجمالاً، {this_month} خلال ٣٠ يوماً ({trend})",
                _projection(used, total, this_month, settings.storage_limit_bytes),
                "",
                "أعلى النطاقات هذا الشهر:",
                *([f"  {domain} — {count}" for domain, count in domains] or ["  لا شيء"]),
            ]
        )
        logger.info("%s", body)
        await raise_alert(db, workspace_id, MONTHLY_DOMAINS.key, title="📊 تقرير شهري", body=body)
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", type=int, required=True, help="workspace id to report on")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(run(args.workspace))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
