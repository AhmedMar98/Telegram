"""
Quota tracker — persists per-provider quota state to the database.

Updates LLMProviderState rows from response headers and applies cooldowns
when a provider returns rate-limit (429) or repeated errors.

In-memory fallback when DB tables aren't available (e.g. during unit tests).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from loguru import logger
from sqlalchemy import select

from ..database import SessionLocal
from ..models import LLMProviderState


# Cooldown after a rate-limit hit
DEFAULT_COOLDOWN_SEC = 60
# Cooldown after a hard failure
ERROR_COOLDOWN_SEC = 30
# Max consecutive errors before marking provider unhealthy
MAX_CONSECUTIVE_ERRORS = 5


class QuotaTracker:
    """Tracks quota usage and health per provider (DB-backed with in-memory fallback)."""

    def __init__(self, db_factory=None) -> None:
        self.db_factory = db_factory or SessionLocal
        # In-memory fallback when DB unavailable
        self._memory: dict[str, dict] = {}

    def _try_db(self, op):
        """Try a DB op; on failure, fall back to in-memory."""
        try:
            return op()
        except Exception as e:
            logger.debug("[quota] DB unavailable, using in-memory: {}", e)
            return None

    def update_success(self, provider: str, quota=None) -> None:
        """Record a successful call; update quota from headers if available."""
        self._memory.setdefault(provider, {})["last_success"] = datetime.utcnow()
        self._memory[provider]["cooldown_until"] = None

        def _db_op():
            with self.db_factory() as db:
                state = db.get(LLMProviderState, provider)
                if state is None:
                    state = LLMProviderState(name=provider)
                    db.add(state)
                state.is_healthy = True
                state.requests_today = (state.requests_today or 0) + 1
                state.requests_total = (state.requests_total or 0) + 1
                state.last_success_at = datetime.utcnow()
                state.cooldown_until = None
                state.last_error = None
                if quota is not None:
                    if quota.limit is not None:
                        state.quota_limit = quota.limit
                    if quota.remaining is not None:
                        state.quota_remaining = quota.remaining
                    if quota.reset_at is not None:
                        state.quota_resets_at = quota.reset_at
                db.commit()
        self._try_db(_db_op)

    def update_rate_limited(self, provider: str, quota=None) -> None:
        """Mark provider as rate-limited; apply cooldown."""
        self._memory.setdefault(provider, {})["cooldown_until"] = (
            datetime.utcnow() + timedelta(seconds=DEFAULT_COOLDOWN_SEC)
        )

        def _db_op():
            with self.db_factory() as db:
                state = db.get(LLMProviderState, provider)
                if state is None:
                    state = LLMProviderState(name=provider)
                    db.add(state)
                state.cooldown_until = datetime.utcnow() + timedelta(seconds=DEFAULT_COOLDOWN_SEC)
                state.last_error = "rate_limited"
                if quota is not None:
                    if quota.reset_at is not None:
                        state.quota_resets_at = quota.reset_at
                db.commit()
        self._try_db(_db_op)
        logger.info("[quota] {} rate-limited, cooldown {}s", provider, DEFAULT_COOLDOWN_SEC)

    def update_failure(self, provider: str, error: str) -> None:
        """Record a non-rate-limit failure."""
        self._memory.setdefault(provider, {})["cooldown_until"] = (
            datetime.utcnow() + timedelta(seconds=ERROR_COOLDOWN_SEC)
        )

        def _db_op():
            with self.db_factory() as db:
                state = db.get(LLMProviderState, provider)
                if state is None:
                    state = LLMProviderState(name=provider)
                    db.add(state)
                state.last_error = error[:500]
                state.cooldown_until = datetime.utcnow() + timedelta(seconds=ERROR_COOLDOWN_SEC)
                db.commit()
        self._try_db(_db_op)

    def is_in_cooldown(self, provider: str) -> bool:
        """Check if a provider is currently in cooldown."""
        # Check memory first (fast path)
        mem_state = self._memory.get(provider, {})
        mem_cd = mem_state.get("cooldown_until")
        if mem_cd is not None and mem_cd > datetime.utcnow():
            return True

        def _db_op():
            with self.db_factory() as db:
                state = db.get(LLMProviderState, provider)
                if state is None:
                    return False
                if state.cooldown_until is None:
                    return False
                return state.cooldown_until > datetime.utcnow()
        result = self._try_db(_db_op)
        return result if result is not None else False

    def list_healthy(self) -> list[str]:
        """Return list of provider names not in cooldown and healthy."""
        def _db_op():
            with self.db_factory() as db:
                stmt = select(LLMProviderState).where(
                    LLMProviderState.cooldown_until.is_(None) |
                    (LLMProviderState.cooldown_until <= datetime.utcnow())
                )
                return [r.name for r in db.scalars(stmt)]
        result = self._try_db(_db_op)
        return result if result is not None else list(self._memory.keys())
