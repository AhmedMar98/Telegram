"""
Distributed collector coordination.

When multiple userbot workers run on different machines, they must:
    1. Register themselves in the WorkerRegistry table on startup.
    2. Send periodic heartbeats (every 30s).
    3. Claim channels via Redis locks (SET NX EX) so two workers don't
       fetch from the same channel simultaneously.
    4. Release locks + mark as 'stopped' on shutdown.
    5. Detect dead workers (no heartbeat > 5 min) and re-claim their channels.

This module is NOT a separate process — it's a thin coordination layer
that the existing TelethonCollector uses.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import uuid
from datetime import timedelta

from loguru import logger
from sqlalchemy import update

from ..database import SessionLocal
from ..models import WorkerRegistry
from ..timeutil import utcnow

HEARTBEAT_INTERVAL_SEC = 30
LOCK_TTL_SEC = 120  # Redis lock expires after 2 min if worker dies
DEAD_WORKER_THRESHOLD_SEC = 300  # 5 min without heartbeat = dead


class WorkerCoordinator:
    """Coordinates distributed collectors via DB + Redis."""

    def __init__(self, account_name: str, worker_id: str | None = None) -> None:
        self.account_name = account_name
        self.worker_id = worker_id or self._generate_worker_id()
        self._heartbeat_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    @staticmethod
    def _generate_worker_id() -> str:
        host = socket.gethostname() or "unknown"
        pid = os.getpid()
        rand = uuid.uuid4().hex[:8]
        return f"{host}-{pid}-{rand}"

    # ============================================================
    # Lifecycle
    # ============================================================

    async def register(self, channels: list[str]) -> None:
        """Register this worker in the DB."""
        with SessionLocal() as db:
            existing = (
                db.query(WorkerRegistry).filter(WorkerRegistry.worker_id == self.worker_id).first()
            )
            if existing:
                existing.status = "active"
                existing.hostname = socket.gethostname()
                existing.pid = os.getpid()
                existing.channels_assigned = json.dumps(channels)
                existing.started_at = utcnow()
                existing.last_heartbeat_at = utcnow()
                existing.stopped_at = None
            else:
                db.add(
                    WorkerRegistry(
                        worker_id=self.worker_id,
                        account_name=self.account_name,
                        hostname=socket.gethostname(),
                        pid=os.getpid(),
                        status="active",
                        channels_assigned=json.dumps(channels),
                        links_collected=0,
                        last_heartbeat_at=utcnow(),
                    )
                )
            db.commit()
        logger.info("[worker] registered: id={} channels={}", self.worker_id, channels)

    async def start_heartbeats(self) -> None:
        """Spawn a background task that sends heartbeats every 30s."""
        if self._heartbeat_task is not None:
            return
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self) -> None:
        """Deregister on shutdown."""
        self._stop.set()
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
            self._heartbeat_task = None
        with SessionLocal() as db:
            db.execute(
                update(WorkerRegistry)
                .where(WorkerRegistry.worker_id == self.worker_id)
                .values(status="stopped", stopped_at=utcnow())
            )
            db.commit()
        # Release all Redis locks held by this worker
        await self._release_all_locks()
        logger.info("[worker] deregistered: {}", self.worker_id)

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                with SessionLocal() as db:
                    db.execute(
                        update(WorkerRegistry)
                        .where(WorkerRegistry.worker_id == self.worker_id)
                        .values(last_heartbeat_at=utcnow(), status="active")
                    )
                    db.commit()
            except Exception as e:
                logger.warning("[worker] heartbeat failed: {}", e)
            await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)

    def increment_links_collected(self, count: int = 1) -> None:
        """Bump the counter for this worker."""
        try:
            with SessionLocal() as db:
                db.execute(
                    update(WorkerRegistry)
                    .where(WorkerRegistry.worker_id == self.worker_id)
                    .values(links_collected=WorkerRegistry.links_collected + count)
                )
                db.commit()
        except Exception as e:
            logger.debug("[worker] increment failed: {}", e)

    # ============================================================
    # Channel claim (Redis-based distributed lock)
    # ============================================================

    async def claim_channel(self, channel: str) -> bool:
        """
        Try to acquire a Redis lock for `channel`.

        Returns True if acquired (this worker should fetch from it),
        False if another worker is already fetching.
        """
        try:
            from ..redis_client import get_redis

            r = get_redis()
            key = f"channel_lock:{channel}"
            # SET NX EX: only set if not exists, auto-expire after TTL
            acquired = r.set(key, self.worker_id, nx=True, ex=LOCK_TTL_SEC)
            if acquired:
                logger.debug("[worker] claimed channel: {}", channel)
                return True
            # Check who owns it
            owner = r.get(key)
            if owner == self.worker_id:
                # We already hold it (refresh)
                r.expire(key, LOCK_TTL_SEC)
                return True
            return False
        except Exception as e:
            # Redis unavailable → assume single-worker mode, allow fetch
            logger.debug("[worker] redis unavailable, assuming claim: {}", e)
            return True

    async def release_channel(self, channel: str) -> None:
        """Release the Redis lock for `channel`."""
        try:
            from ..redis_client import get_redis

            r = get_redis()
            key = f"channel_lock:{channel}"
            # Only delete if we own it
            owner = r.get(key)
            if owner == self.worker_id:
                r.delete(key)
                logger.debug("[worker] released channel: {}", channel)
        except Exception as e:
            logger.debug("[worker] release failed: {}", e)

    async def _release_all_locks(self) -> None:
        """Best-effort: release all locks owned by this worker."""
        try:
            from ..redis_client import get_redis

            r = get_redis()
            for key in r.scan_iter("channel_lock:*"):
                owner = r.get(key)
                if owner == self.worker_id:
                    r.delete(key)
        except Exception as e:
            logger.debug("[worker] release_all failed: {}", e)

    # ============================================================
    # Dead worker detection
    # ============================================================

    @staticmethod
    def cleanup_dead_workers() -> int:
        """Mark workers without recent heartbeats as 'dead'. Returns count."""
        cutoff = utcnow() - timedelta(seconds=DEAD_WORKER_THRESHOLD_SEC)
        with SessionLocal() as db:
            result = db.execute(
                update(WorkerRegistry)
                .where(
                    WorkerRegistry.status == "active",
                    WorkerRegistry.last_heartbeat_at < cutoff,
                )
                .values(status="dead", stopped_at=utcnow())
            )
            db.commit()
            count = result.rowcount or 0
            if count > 0:
                logger.warning("[worker] marked {} dead workers", count)
            return count

    @staticmethod
    def list_active_workers() -> list[WorkerRegistry]:
        with SessionLocal() as db:
            return (
                db.query(WorkerRegistry).filter(WorkerRegistry.status.in_(["active", "idle"])).all()
            )
