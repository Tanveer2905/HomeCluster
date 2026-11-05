"""Remote terminals (SPEC §10).

The coordinator only routes bytes: ``term_open`` / ``term_input`` / ``term_resize`` /
``term_close`` go to the worker inside its poll response, output comes back over
``POST /api/w/term/{sid}/output`` and is broadcast to the dashboard WebSocket.
Sessions outlive the dashboard page that opened them: the page can leave and
come back, read the scrollback (``GET /api/terminal/{sid}``) and carry on.
``pos`` counts every character a session ever held, so a page that reads the
scrollback can skip live output it already has.

Two kinds of session:

* line (``pty: false``): the shell's stdin is a pipe; the page sends whole lines
  and shows them itself, so they are kept in the scrollback as ``$ <line>``.
* full (``pty: true``): the shell runs on a pseudo-terminal of ``cols`` x ``rows``
  (ConPTY on Windows); the page (xterm.js) sends keystrokes and the terminal echoes
  them, so htop, vim and top work.  Needs the worker's ``pty`` capability.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any, Dict, Iterable, List, Optional

log = logging.getLogger("coordinator.terminal")

IDLE_TIMEOUT = 30 * 60.0
MAX_SCROLLBACK = 400          # line sessions: output chunks kept
MAX_PTY_SCROLLBACK = 256_000  # full sessions: characters kept (screen redraws are big)
COLS = (10, 500)
ROWS = (2, 300)


def clamp_size(cols: Any, rows: Any) -> "tuple[int, int]":
    """A terminal size the workers accept; 80 x 24 when not given."""

    def one(value: Any, limits: "tuple[int, int]", default: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return max(limits[0], min(limits[1], number))

    return one(cols, COLS, 80), one(rows, ROWS, 24)


class Session:
    """One shell on one device."""

    __slots__ = ("id", "device_id", "created", "last", "closed", "exit_code", "buffer", "pos",
                 "size", "pty", "cols", "rows")

    def __init__(self, session_id: str, device_id: str, pty: bool = False,
                 cols: int = 80, rows: int = 24) -> None:
        self.id = session_id
        self.device_id = device_id
        self.created = time.time()
        self.last = self.created
        self.closed = False
        self.exit_code: Optional[int] = None
        self.buffer: List[str] = []
        self.pos = 0  # characters ever appended, including ones scrolled away
        self.size = 0  # characters in the buffer now
        self.pty = pty
        self.cols = cols
        self.rows = rows

    def touch(self) -> None:
        self.last = time.time()

    def append(self, data: str) -> None:
        self.buffer.append(data)
        self.pos += len(data)
        self.size += len(data)
        if self.pty:
            while len(self.buffer) > 1 and self.size > MAX_PTY_SCROLLBACK:
                self.size -= len(self.buffer.pop(0))
        elif len(self.buffer) > MAX_SCROLLBACK:
            dropped = self.buffer[: len(self.buffer) - MAX_SCROLLBACK]
            del self.buffer[: len(self.buffer) - MAX_SCROLLBACK]
            self.size -= sum(len(chunk) for chunk in dropped)

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
            "pty": self.pty,
            "cols": self.cols,
            "rows": self.rows,
        }


class TerminalManager:
    """Tracks open sessions and pushes the commands that drive them."""

    def __init__(self, state: Any) -> None:
        self.state = state
        self.sessions: Dict[str, Session] = {}

    # ------------------------------------------------------------ lifecycle
    def open(self, device_id: str, pty: bool = False, cols: Any = None, rows: Any = None) -> Session:
        """Start a shell.  A full terminal is asked for with ``pty``; a device whose
        worker can't give one (no ``pty`` capability) gets a line session instead,
        which the answer's ``pty: false`` says."""
        device = self.state.db.get("devices", device_id)
        if device is None or device.get("removed"):
            raise KeyError("unknown device %s" % device_id)
        if device.get("platform") == "browser":
            raise ValueError("browser workers do not support terminals")
        if device.get("status") != "online":
            raise ValueError("device is offline")
        full = bool(pty) and "pty" in (device.get("caps") or [])
        width, height = clamp_size(cols, rows)
        session = Session(secrets.token_hex(6), device_id, full, width, height)
        self.sessions[session.id] = session
        command: Dict[str, Any] = {"cmd": "term_open", "session_id": session.id, "shell": None}
        if full:
            command.update(pty=True, cols=width, rows=height)
        self.state.send_command(device_id, command)
        log.info("terminal %s opened on %s%s", session.id, device.get("name"),
                 " (%dx%d)" % (width, height) if full else "")
        return session

    def get(self, session_id: str) -> Optional[Session]:
        return self.sessions.get(str(session_id))

    def input(self, session_id: str, data: str) -> bool:
        session = self.get(session_id)
        if session is None or session.closed:
            return False
        session.touch()
        if not session.pty:
            # the page shows the line as typed; kept for the scrollback (a full
            # terminal echoes keystrokes itself)
            session.append("$ " + data)
        self.state.send_command(
            session.device_id,
            {"cmd": "term_input", "session_id": session.id, "data": data},
        )
        return True

    def keep_open(self, session_ids: Iterable[Any]) -> int:
        """A dashboard tab still has these open, so their idle timer starts over:
        one you merely switched away from is not closed under you (SPEC §10)."""
        kept = 0
        for session_id in session_ids:
            session = self.get(session_id)
            if session is not None and not session.closed:
                session.touch()
                kept += 1
        return kept

    def resize(self, session_id: str, cols: Any, rows: Any, redraw: bool = False) -> bool:
        """A full terminal's new size (the page's window changed).  ``redraw`` asks
        the program on it to paint its screen again, for a page that comes back."""
        session = self.get(session_id)
        if session is None or session.closed or not session.pty:
            return False
        session.cols, session.rows = clamp_size(cols, rows)
        command: Dict[str, Any] = {"cmd": "term_resize", "session_id": session.id,
                                   "cols": session.cols, "rows": session.rows}
        if redraw:
            command["redraw"] = True
        self.state.send_command(session.device_id, command)
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
        """Close sessions idle for more than 30 minutes: no input, no output and no
        dashboard tab keeping them open."""
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
