"""
Link Intelligence Platform — Application package.

Layered architecture:
    collectors/   → Telegram userbots (Telethon + Mock)
    classifier/   → 3-tier classification pipeline (rules → metadata → AI)
    llm/          → Multi-provider LLM orchestrator with failover & quota tracking
    vitality/     → Async link alive/dead checker with human-like rate
    search/       → Hybrid text (FTS5) + semantic (sqlite-vec) search
    bot/          → Telegram bot for end-user queries
    web/          → FastAPI admin panel + REST API
    workers/      → Background workers (collect/classify/vitality)
"""

__version__ = "4.0.0"
__all__ = ["__version__"]
