"""Remote line terminals (SPEC §10).

The coordinator only routes bytes: ``term_open`` / ``term_input`` / ``term_close``
go to the worker inside its poll response, output comes back over
``POST /api/w/term/{sid}/output`` and is broadcast to the dashboard WebSocket.
Sessions outlive the dashboard page that opened them: the page can leave and
come back, read the scrollback (``GET /api/terminal/{sid}``: output plus the
commands typed) and carry on.  ``pos`` counts every character a session ever
held, so a page that reads the scrollback can skip live output it already has.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger("coordinator.terminal")

IDLE_TIMEOUT = 30 * 60.0
MAX_SCROLLBACK = 400


class Session:
    """One shell on one device."""

    __slots__ = ("id", "device_id", "created", "last", "closed", "exit_code", "buffer", "pos")

    def __init__(self, session_id: str, device_id: str) -> None:
        self.id = session_id
        self.device_id = device_id
        self.created = time.time()
        self.last = self.created
        self.closed = False
        self.exit_code: Optional[int] = None
        self.buffer: List[str] = []
        self.pos = 0  # characters ever appended, including ones scrolled away

    def touch(self) -> None:
        self.last = time.time()

    def append(self, data: str) -> None:
        self.buffer.append(data)
        self.pos += len(data)
        if len(self.buffer) > MAX_SCROLLBACK:
            del self.buffer[: len(self.buffer) - MAX_SCROLLBACK]

    def scrollback(self) -> Dict[str, Any]:
        return dict(self.to_dict(), output="".join(self.buffer), pos=self.pos)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.id,
            "device_id": self.device_id,
            "created": self.created,
            "last": self.last,
            "closed": self.closed,
            "exit_code": self.exit_code,
        }


class TerminalManager:
    """Tracks open sessions and pushes the commands that drive them."""

    def __init__(self, state: Any) -> None:
        self.state = state
        self.sessions: Dict[str, Session] = {}

    # ------------------------------------------------------------ lifecycle
    def open(self, device_id: str) -> Session:
        device = self.state.db.get("devices", device_id)
        if device is None or device.get("removed"):
            raise KeyError("unknown device %s" % device_id)
        if device.get("platform") == "browser":
            raise ValueError("browser workers do not support terminals")
        if device.get("status") != "online":
            raise ValueError("device is offline")
        session = Session(secrets.token_hex(6), device_id)
        self.sessions[session.id] = session
        self.state.send_command(
            device_id, {"cmd": "term_open", "session_id": session.id, "shell": None}
        )
        log.info("terminal %s opened on %s", session.id, device.get("name"))
        return session

    def get(self, session_id: str) -> Optional[Session]:
        return self.sessions.get(str(session_id))

    def input(self, session_id: str, data: str) -> bool:
        session = self.get(session_id)
        if session is None or session.closed:
            return False
        session.touch()
        session.append("$ " + data)  # the page shows it as typed; kept for the scrollback
        self.state.send_command(
            session.device_id,
            {"cmd": "term_input", "session_id": session.id, "data": data},
        )
        return True

    def close(self, session_id: str, exit_code: Optional[int] = None, notify_worker: bool = True) -> bool:
        session = self.sessions.get(str(session_id))
        if session is None:
            return False
        if notify_worker and not session.closed:
            self.state.send_command(
                session.device_id, {"cmd": "term_close", "session_id": session.id}
            )
        session.closed = True
        session.exit_code = exit_code
        self.sessions.pop(str(session_id), None)
        return True

    # -------------------------------------------------------------- output
    async def on_output(self, session_id: str, data: str) -> None:
        session = self.get(session_id)
        message: Dict[str, Any] = {"ev": "term", "session_id": str(session_id), "data": data}
        if session is not None:
            session.touch()
            session.append(data)
            message["pos"] = session.pos
        await self.state.hub.broadcast(message)

    async def on_closed(self, session_id: str, exit_code: Optional[int]) -> None:
        self.close(session_id, exit_code, notify_worker=False)
        await self.state.hub.broadcast(
            {"ev": "term_closed", "session_id": str(session_id), "exit_code": exit_code}
        )

    # ----------------------------------------------------------- reaping
    def sessions_for(self, device_id: str) -> List[Session]:
        return [s for s in self.sessions.values() if s.device_id == device_id]

    def reap(self, now: Optional[float] = None) -> List[str]:
        """Close sessions idle for more than 30 minutes."""
        now = time.time() if now is None else now
        stale = [s.id for s in self.sessions.values() if now - s.last > IDLE_TIMEOUT]
        for session_id in stale:
            log.info("terminal %s closed (idle)", session_id)
            self.close(session_id)
            self._tell_pages(session_id)
        return stale

    def drop_device(self, device_id: str) -> List[str]:
        """Forget every session of a device that went away."""
        gone = [s.id for s in self.sessions_for(device_id)]
        for session_id in gone:
            self.close(session_id, notify_worker=False)
            self._tell_pages(session_id)
        return gone

    def _tell_pages(self, session_id: str) -> None:
        """A page still showing the session learns it ended (no worker will say so)."""
        self.state.hub.publish({"ev": "term_closed", "session_id": session_id, "exit_code": None})

    def list(self) -> List[Dict[str, Any]]:
        return [s.to_dict() for s in self.sessions.values()]
