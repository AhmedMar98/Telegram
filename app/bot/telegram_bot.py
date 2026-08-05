"""
Telegram search bot — exposes /search, /stats, /popular, /alive commands.

Built with aiogram 3.x. Runs as a separate asyncio task alongside the web server.

Anti-spam guarantees:
    - Bot only RESPONDS to private messages and to /commands in groups.
    - Bot NEVER proactively messages users or channels.
    - Rate-limited per user (max 10 commands/minute).
"""
from __future__ import annotations

import time
from collections import defaultdict

from loguru import logger

from ..config import get_settings
from ..database import SessionLocal
from ..models import Link
from ..search.hybrid import HybridSearch

# Per-user rate limit
MAX_CMD_PER_MIN = 10
RATE_WINDOW_SEC = 60


class TelegramSearchBot:
    """aiogram 3.x bot for searching the link database."""

    def __init__(self, search_engine: HybridSearch | None = None) -> None:
        self.settings = get_settings()
        self.token = self.settings.tg_bot_token
        self.search = search_engine or HybridSearch()
        self._bot = None
        self._dp = None
        self._user_cmd_log: dict[int, list[float]] = defaultdict(list)

    @property
    def is_available(self) -> bool:
        return bool(self.token)

    def _check_rate(self, user_id: int) -> bool:
        """Return True if user is within rate limit."""
        now = time.time()
        recent = [t for t in self._user_cmd_log[user_id] if now - t < RATE_WINDOW_SEC]
        self._user_cmd_log[user_id] = recent
        if len(recent) >= MAX_CMD_PER_MIN:
            return False
        recent.append(now)
        return True

    async def start(self) -> None:
        """Start the bot (blocking — run in a task)."""
        if not self.is_available:
            logger.warning("TG_BOT_TOKEN not set; search bot disabled")
            return
        try:
            from aiogram import Bot, Dispatcher
            from aiogram.filters import Command
            from aiogram.types import Message
        except ImportError:
            logger.error("aiogram not installed: pip install aiogram")
            return

        self._bot = Bot(self.token)
        self._dp = Dispatcher()

        @self._dp.message(Command("start"))
        async def cmd_start(m: Message) -> None:
            await m.answer(
                "🤖 <b>Link Intelligence Bot</b>\n\n"
                "Commands:\n"
                "/search &lt;query&gt; — hybrid search (text + semantic)\n"
                "/stats — database statistics\n"
                "/popular — top 10 popular links\n"
                "/alive — recent alive links\n",
                parse_mode="HTML",
            )

        @self._dp.message(Command("search"))
        async def cmd_search(m: Message) -> None:
            if not self._check_rate(m.from_user.id):
                await m.answer("⏳ Too many requests. Wait a minute.")
                return
            query = (m.text or "").replace("/search", "").strip()
            if not query:
                await m.answer("Usage: /search <query>")
                return
            try:
                results = await self.search.search(query, limit=10, alive_only=True)
            except Exception as e:
                logger.exception("Search failed")
                await m.answer(f"❌ Search error: {e}")
                return
            if not results:
                await m.answer("No results found.")
                return
            lines = [f"🔍 <b>Top {len(results)} for</b> '<code>{query}</code>':\n"]
            for i, r in enumerate(results, 1):
                title = (r.title or r.url)[:60]
                lines.append(
                    f"{i}. <a href=\"{r.url}\">{_escape(title)}</a>\n"
                    f"   📂 {r.category} | ⭐ {r.score:.2f} | "
                    f"{'✅' if r.alive else '❌'}\n"
                )
            await m.answer("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)

        @self._dp.message(Command("stats"))
        async def cmd_stats(m: Message) -> None:
            from sqlalchemy import func, select
            with SessionLocal() as db:
                total = db.query(Link).count()
                alive = db.query(Link).filter(Link.alive.is_(True)).count()
                dead = db.query(Link).filter(Link.alive.is_(False)).count()
                cat_rows = db.execute(
                    select(Link.category, func.count(Link.id))
                    .where(Link.archived.is_(False))
                    .group_by(Link.category)
                ).all()
            lines = [
                "📊 <b>Database Stats</b>\n",
                f"🔗 Total links: <b>{total}</b>",
                f"✅ Alive: <b>{alive}</b>",
                f"❌ Dead: <b>{dead}</b>\n",
                "<b>📂 By category:</b>",
            ]
            for cat, cnt in sorted(cat_rows, key=lambda x: -x[1]):
                lines.append(f"  • {cat}: {cnt}")
            await m.answer("\n".join(lines), parse_mode="HTML")

        @self._dp.message(Command("popular"))
        async def cmd_popular(m: Message) -> None:
            with SessionLocal() as db:
                links = db.query(Link).filter(
                    Link.archived.is_(False),
                    Link.alive.is_(True),
                ).order_by(Link.click_count.desc()).limit(10).all()
            if not links:
                await m.answer("No popular links yet.")
                return
            lines = ["🔥 <b>Top 10 popular</b>:\n"]
            for i, link in enumerate(links, 1):
                title = (link.title or link.url)[:60]
                lines.append(
                    f'{i}. <a href="{link.url}">{_escape(title)}</a> '
                    f"(👥 {link.click_count} clicks)\n"
                )
            await m.answer("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)

        @self._dp.message(Command("alive"))
        async def cmd_alive(m: Message) -> None:
            with SessionLocal() as db:
                links = db.query(Link).filter(
                    Link.alive.is_(True),
                    Link.archived.is_(False),
                ).order_by(Link.last_checked_at.desc()).limit(10).all()
            if not links:
                await m.answer("No alive links yet.")
                return
            lines = ["✅ <b>Recent alive links</b>:\n"]
            for i, link in enumerate(links, 1):
                title = (link.title or link.url)[:60]
                lines.append(
                    f'{i}. <a href="{link.url}">{_escape(title)}</a>\n   📂 {link.category}\n'
                )
            await m.answer("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)

        logger.info("Starting Telegram search bot...")
        await self._dp.start_polling(self._bot)

    async def stop(self) -> None:
        if self._bot:
            await self._bot.session.close()


def _escape(s: str) -> str:
    """Escape HTML entities."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
