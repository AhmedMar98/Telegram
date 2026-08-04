"""
Redis client + helpers.

Used for:
    - Background task queue (RQ)
    - Per-link lock (prevent duplicate classification)
    - Rate-limit counters
    - Caching LLM responses
"""
from __future__ import annotations

from typing import Optional

import redis
from rq import Queue

from .config import get_settings


_redis_pool: Optional[redis.ConnectionPool] = None
_queue: Optional[Queue] = None


def get_redis() -> redis.Redis:
    """Singleton Redis client."""
    global _redis_pool
    if _redis_pool is None:
        settings = get_settings()
        _redis_pool = redis.ConnectionPool.from_url(
            settings.redis_url, decode_responses=True
        )
    return redis.Redis(connection_pool=_redis_pool)


def get_queue(name: str = "default") -> Queue:
    """Get an RQ queue backed by the configured Redis."""
    global _queue
    if _queue is None or _queue.name != name:
        _queue = Queue(name, connection=get_redis())
    return _queue


def is_redis_available() -> bool:
    """Ping check — used to gracefully degrade if Redis is down."""
    try:
        return bool(get_redis().ping())
    except redis.RedisError:
        return False
