"""
Daily summary worker — generates a daily trending-links report.

Runs at midnight (configurable), computes:
    - Top 10 new links discovered today (by click_count + search_hit_count)
    - Top 10 alive links
    - Dead links discovered today
    - Category breakdown
    - LLM provider stats (requests_today, quota usage)

Stores in DailySummary table and (optionally) broadcasts to subscribers
via the Telegram bot's admin channel.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import func, select

from ..database import SessionLocal
from ..models import (
    DailySummary,
    Link,
)
from ..timeutil import utcnow


class DailySummaryWorker:
    """Generates and broadcasts daily summaries."""

    def __init__(self, run_hour: int = 0, run_minute: int = 0) -> None:
        self.run_hour = run_hour
        self.run_minute = run_minute
        self._stop = asyncio.Event()

    async def run_loop(self) -> None:
        """Sleep until next midnight, then run once; repeat."""
        logger.info(
            "[daily-summary] started; will run at {:02d}:{:02d} daily",
            self.run_hour,
            self.run_minute,
        )
        while not self._stop.is_set():
            sleep_sec = self._seconds_until_next_run()
            logger.debug("[daily-summary] next run in {:.0f}s", sleep_sec)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_sec)
                return  # stop signal received
            except TimeoutError:
                pass
            try:
                await self.generate_and_store()
            except Exception as e:
                logger.exception("[daily-summary] generation failed: {}", e)

    def _seconds_until_next_run(self) -> float:
        now = datetime.now()
        target = now.replace(hour=self.run_hour, minute=self.run_minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    async def generate_and_store(self, date_str: str | None = None) -> DailySummary:
        """Generate summary for the given date (default: yesterday)."""
        if date_str is None:
            date_str = (utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")

        logger.info("[daily-summary] generating for {}", date_str)

        start_of_day = datetime.strptime(date_str, "%Y-%m-%d")
        end_of_day = start_of_day + timedelta(days=1)

        with SessionLocal() as db:
            # New links discovered that day
            new_links = (
                db.query(Link)
                .filter(
                    Link.discovered_at >= start_of_day,
                    Link.discovered_at < end_of_day,
                    Link.archived.is_(False),
                )
                .all()
            )

            total_new = len(new_links)
            total_alive = sum(1 for link in new_links if link.alive)
            total_dead = sum(1 for link in new_links if link.alive is False)

            # Top links by engagement (clicks + search hits)
            top_links = sorted(
                new_links,
                key=lambda link: (link.click_count or 0) + (link.search_hit_count or 0),
                reverse=True,
            )[:10]
            top_links_json = json.dumps(
                [
                    {
                        "id": link.id,
                        "url": link.url,
                        "title": link.title,
                        "category": link.category,
                        "alive": link.alive,
                        "clicks": link.click_count or 0,
                        "search_hits": link.search_hit_count or 0,
                    }
                    for link in top_links
                ],
                ensure_ascii=False,
            )

            # Category breakdown
            cat_rows = db.execute(
                select(Link.category, func.count(Link.id))
                .where(Link.discovered_at >= start_of_day)
                .where(Link.discovered_at < end_of_day)
                .group_by(Link.category)
            ).all()
            category_breakdown = {c: n for c, n in cat_rows}
            category_json = json.dumps(category_breakdown, ensure_ascii=False)

            # Upsert summary
            summary = db.query(DailySummary).filter(DailySummary.summary_date == date_str).first()
            if summary is None:
                summary = DailySummary(summary_date=date_str)
                db.add(summary)
            summary.total_new = total_new
            summary.total_alive = total_alive
            summary.total_dead = total_dead
            summary.top_links_json = top_links_json
            summary.category_breakdown_json = category_json
            summary.sent_to_subscribers = False
            db.commit()
            db.refresh(summary)

        logger.info(
            "[daily-summary] {} created: new={} alive={} dead={}",
            date_str,
            total_new,
            total_alive,
            total_dead,
        )
        return summary

    def format_for_bot(self, summary: DailySummary) -> str:
        """Render the summary as an HTML message for the Telegram bot."""
        top_links = json.loads(summary.top_links_json or "[]")
        cats = json.loads(summary.category_breakdown_json or "{}")
        lines = [
            f"📊 <b> Daily Summary — {summary.summary_date}</b>\n",
            f"🆕 New links: <b>{summary.total_new}</b>",
            f"✅ Alive: <b>{summary.total_alive}</b>",
            f"❌ Dead: <b>{summary.total_dead}</b>\n",
            "<b>📂 Categories:</b>",
        ]
        for cat, count in sorted(cats.items(), key=lambda x: -x[1]):
            lines.append(f"  • {cat}: {count}")
        if top_links:
            lines.append("\n<b>🔥 Top links:</b>")
            for i, item in enumerate(top_links[:5], 1):
                title = (item.get("title") or item["url"])[:60]
                lines.append(
                    f'{i}. <a href="{item["url"]}">{_esc(title)}</a> '
                    f"(👥 {item.get('clicks', 0)})"
                )
        return "\n".join(lines)

    def stop(self) -> None:
        self._stop.set()


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
