"""Bot handlers grouped by responsibility.

Three routers, not the four-to-six separate *bots* an earlier design
proposed. The distinction is the whole point: separate bots would mean
separate tokens and separate webhooks — more credentials to protect and
more endpoints to defend — to buy an organisational split that aiogram's
``Router`` already provides inside a single Dispatcher, at no cost.
"""

from __future__ import annotations

from app.bot.routers.onboarding import router as onboarding_router
from app.bot.routers.search import router as search_router
from app.bot.routers.status import router as status_router

__all__ = ["onboarding_router", "search_router", "status_router"]
