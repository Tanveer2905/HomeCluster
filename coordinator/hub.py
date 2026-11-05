"""Dashboard WebSocket hub (SPEC §11).

Server -> client: ``snapshot``, ``device``, ``devices``, ``job``, ``task``,
``log``, ``event``, ``term``, ``term_closed``, ``stats``.
Client -> server: ``sub_logs``, ``unsub_logs``, ``term_input``, ``term_resize``,
``term_keep_open``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any, Dict, List, Optional, Set

from starlette.websockets import WebSocket, WebSocketState

log = logging.getLogger("coordinator.hub")

STATS_INTERVAL = 5.0


class Connection:
    """One dashboard socket plus its per-task log subscriptions."""

    __slots__ = ("socket", "log_subs", "_lock")

    def __init__(self, socket: WebSocket) -> None:
        self.socket = socket
        self.log_subs: Set[str] = set()
        self._lock = asyncio.Lock()

    async def send(self, message: Dict[str, Any]) -> bool:
        if self.socket.client_state != WebSocketState.CONNECTED:
            return False
        try:
            async with self._lock:
                await self.socket.send_text(json.dumps(message, default=str))
            return True
        except Exception:
            return False


class Hub:
    """Fan-out to every connected dashboard."""

    def __init__(self, state: Any) -> None:
        self.state = state
        self.connections: List[Connection] = []
        self._stats_task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._stop = asyncio.Event()
        self._stats_task = asyncio.create_task(self._stats_loop(), name="hub-stats")

    async def stop(self) -> None:
        self._stop.set()
        task = self._stats_task
        self._stats_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for conn in list(self.connections):
            with contextlib.suppress(Exception):
                await conn.socket.close()
        self.connections.clear()

    async def _stats_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=STATS_INTERVAL)
                return
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:  # pragma: no cover
                return
            if not self.connections:
                continue
            try:
                await self.broadcast({"ev": "stats", "stats": self.state.stats()})
            except asyncio.CancelledError:  # pragma: no cover
                return
            except Exception:
                log.debug("stats broadcast failed", exc_info=True)

    # --------------------------------------------------------- connections
    def add(self, socket: WebSocket) -> Connection:
        conn = Connection(socket)
        self.connections.append(conn)
        return conn

    def remove(self, conn: Connection) -> None:
        try:
            self.connections.remove(conn)
        except ValueError:
            pass

    # ---------------------------------------------------------- broadcasts
    async def broadcast(self, message: Dict[str, Any]) -> None:
        if not self.connections:
            return
        dead: List[Connection] = []
        for conn in list(self.connections):
            if not await conn.send(message):
                dead.append(conn)
        for conn in dead:
            self.remove(conn)

    async def send_log(self, task_id: str, lines: List[str]) -> None:
        """Only sockets that subscribed to this task get its log lines."""
        if not lines:
            return
        message = {"ev": "log", "task_id": str(task_id), "lines": list(lines)}
        for conn in list(self.connections):
            if str(task_id) in conn.log_subs:
                if not await conn.send(message):
                    self.remove(conn)

    def publish(self, message: Dict[str, Any]) -> None:
        """Fire-and-forget broadcast, callable from synchronous code."""
        if not self.connections:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - outside the event loop
            return
        loop.create_task(self.broadcast(message))

    # --------------------------------------------------------- convenience
    async def snapshot(self) -> Dict[str, Any]:
        state = self.state
        return {
            "ev": "snapshot",
            "devices": state.db.select("devices", "WHERE removed=0 ORDER BY created ASC"),
            "jobs": state.db.select("jobs", "ORDER BY created DESC LIMIT 50"),
            "stats": state.stats(),
            "events": state.db.select("events", "ORDER BY id DESC LIMIT 50"),
            "version": state.version,
        }

    async def handle_client_message(self, conn: Connection, raw: str) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        op = message.get("op")
        if op == "sub_logs":
            task_id = str(message.get("task_id") or "")
            if task_id:
                conn.log_subs.add(task_id)
                tail = self.state.read_task_log(task_id, tail=200)
                if tail:
                    await conn.send({"ev": "log", "task_id": task_id, "lines": tail})
        elif op == "unsub_logs":
            conn.log_subs.discard(str(message.get("task_id") or ""))
        elif op == "term_input":
            self.state.terminals.input(
                str(message.get("session_id") or ""), str(message.get("data") or "")
            )
        elif op == "term_resize":
            self.state.terminals.resize(
                str(message.get("session_id") or ""), message.get("cols"), message.get("rows"),
                bool(message.get("redraw")),
            )
        elif op == "term_keep_open":
            ids = message.get("session_ids")
            if isinstance(ids, list):
                self.state.terminals.keep_open(ids[:64])
        elif op == "ping":
            await conn.send({"ev": "pong"})
