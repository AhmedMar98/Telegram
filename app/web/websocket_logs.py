"""
WebSocket live log broadcaster.

A custom loguru sink that, on every log line, pushes the formatted message
to all connected WebSocket clients. The FastAPI endpoint /ws/logs accepts
connections and registers them in a global set.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

router = APIRouter()


class LogBroadcaster:
    """In-memory pub/sub for log lines, broadcast to all WS clients."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._queue: asyncio.Queue | None = None  # lazy init
        self._task: asyncio.Task | None = None
        self._sink_id: int | None = None

    async def start(self) -> None:
        """Attach as a loguru sink + start broadcaster task."""
        if self._task is not None:
            return
        self._queue = asyncio.Queue(maxsize=1000)

        # loguru sink must be sync (or async via wrapper)
        def _sink(message):
            try:
                # message is a loguru Message object; format it
                record = message.record
                payload = {
                    "time": record["time"].strftime("%H:%M:%S"),
                    "level": record["level"].name,
                    "name": record["name"],
                    "function": record["function"],
                    "line": record["line"],
                    "message": str(record["message"]),
                }
                # Non-blocking put; drop if queue full
                if self._queue is not None and self._queue.qsize() < 950:
                    self._queue.put_nowait(payload)
            except Exception:
                pass  # never let logging crash

        self._sink_id = logger.add(_sink, level="INFO", format="{message}")
        self._task = asyncio.create_task(self._broadcast_loop())
        logger.info("[log-broadcaster] started")

    async def stop(self) -> None:
        if self._sink_id is not None:
            logger.remove(self._sink_id)
            self._sink_id = None
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def add_client(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)
        logger.info("[log-broadcaster] client connected (total={})", len(self._clients))

    def remove_client(self, ws: WebSocket) -> None:
        if ws in self._clients:
            self._clients.discard(ws)
            logger.info("[log-broadcaster] client disconnected (total={})", len(self._clients))

    async def _broadcast_loop(self) -> None:
        """Continuously drain the queue and send to all WS clients."""
        assert self._queue is not None  # set in start() before this task is created
        while True:
            payload = await self._queue.get()
            text = json.dumps(payload, ensure_ascii=False)
            dead: list[WebSocket] = []
            for ws in list(self._clients):
                try:
                    await ws.send_text(text)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.remove_client(ws)


# Singleton
_broadcaster: LogBroadcaster | None = None


def get_broadcaster() -> LogBroadcaster:
    global _broadcaster
    if _broadcaster is None:
        _broadcaster = LogBroadcaster()
    return _broadcaster


@router.websocket("/ws/logs")
async def ws_logs(websocket: WebSocket) -> None:
    """Live log stream. Clients receive JSON lines like:
    {"time":"12:34:56","level":"INFO","name":"app.collectors","message":"..."}
    """
    bc = get_broadcaster()
    await bc.add_client(websocket)
    try:
        # Keep connection open; ignore inbound messages
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        bc.remove_client(websocket)
    except Exception:
        bc.remove_client(websocket)
