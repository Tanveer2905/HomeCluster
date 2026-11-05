"""Workers that update themselves (SPEC §8.4).

A Python worker reports the SHA-256 of its own ``worker.py`` when it registers.
When the PC's ``worker/worker.py`` differs and the device is idle (no task,
service, open terminal or file it is sending), the next poll answers with only
``{"cmd": "update", "sha256", "size"}``.  The worker fetches ``/worker.py`` over
the connection it already trusts, checks the hash, test-starts the new file on
its own Python, keeps the old one as ``worker.py.bak`` and restarts.  Tasks
queued meanwhile wait in the device's queue for the restarted worker.

Browser workers get the version of the page files with every poll and reload
themselves, when idle, once it changes.  The PC can already run code on every
worker (that is what jobs are), so updates give it no new power.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("coordinator.updates")

PYTHON_PLATFORMS = ("termux", "linux", "windows")
RESEND_AFTER = 60.0   # seconds before an unanswered update offer is made again


class WorkerUpdates:
    def __init__(self, state: Any) -> None:
        self.state = state
        self._hashes: Dict[str, Tuple[int, int, str]] = {}   # path -> (mtime_ns, size, sha256)
        self.offered: Dict[str, Tuple[str, float]] = {}       # device id -> (sha256, when)

    # ------------------------------------------------------------- versions
    def _sha(self, path: Path) -> Optional[str]:
        try:
            stat = path.stat()
        except OSError:
            return None
        cached = self._hashes.get(str(path))
        if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            return cached[2]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self._hashes[str(path)] = (stat.st_mtime_ns, stat.st_size, digest)
        return digest

    @property
    def worker_path(self) -> Path:
        return Path(self.state.root) / "worker" / "worker.py"

    def worker_sha(self) -> Optional[str]:
        return self._sha(self.worker_path)

    def page_version(self) -> Optional[str]:
        """One hash over the browser worker's files."""
        folder = Path(self.state.root) / "worker" / "browser"
        try:
            files = sorted(p for p in folder.iterdir() if p.is_file())
        except OSError:
            return None
        digest = hashlib.sha256()
        for path in files:
            digest.update(("%s:%s\n" % (path.name, self._sha(path))).encode("utf-8"))
        return digest.hexdigest()[:16]

    # ---------------------------------------------------------------- state
    def busy(self, device_id: str, running: Optional[List[str]] = None) -> Optional[str]:
        """Why this device should not restart now, or None when it is idle."""
        if running:
            return "running %d task(s)" % len(running)
        count = self.state.db.scalar(
            "SELECT COUNT(*) FROM tasks WHERE device_id=? AND status IN ('assigned','running')",
            (device_id,), default=0)
        if count:
            return "running %d task(s)" % int(count)
        if self.state.terminals.sessions_for(device_id):
            return "a terminal is open"
        if any(o.get("device_id") == device_id for o in self.state.peers.offers.values()):
            return "sending a file to another device"
        return None

    def view(self, device: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """What the dashboard shows about this device's worker version."""
        if device.get("platform") not in PYTHON_PLATFORMS:
            return None
        have = (device.get("info") or {}).get("worker_sha256")
        want = self.worker_sha()
        if not have:
            return {"state": "by_hand"}   # a worker from before self-updates
        if not want or have == want:
            return {"state": "current"}
        offer = self.offered.get(device["id"])
        if offer and offer[0] == want and time.time() - offer[1] < RESEND_AFTER:
            return {"state": "updating"}
        reason = self.busy(device["id"])
        return {"state": "waiting", "reason": reason} if reason else {"state": "due"}

    # ------------------------------------------------------------- the offer
    def command_for(self, device: Dict[str, Any], running: List[str]) -> Optional[Dict[str, Any]]:
        """The update command for this poll, or None."""
        if device.get("platform") not in PYTHON_PLATFORMS:
            return None
        have = (device.get("info") or {}).get("worker_sha256")
        want = self.worker_sha()
        if not have or not want or have == want:
            return None
        offer = self.offered.get(device["id"])
        if offer and offer[0] == want and time.time() - offer[1] < RESEND_AFTER:
            return None
        if self.busy(device["id"], running):
            return None
        self.offered[device["id"]] = (want, time.time())
        log.info("offering worker %s to %s (it runs %s)", want[:12], device.get("name"), str(have)[:12])
        return {"cmd": "update", "sha256": want, "size": self.worker_path.stat().st_size}

    def registered(self, device_id: str, info: Dict[str, Any]) -> None:
        """A worker (re)registered: it now runs what it reports."""
        offer = self.offered.get(device_id)
        if offer and info.get("worker_sha256") == offer[0]:
            self.offered.pop(device_id, None)
            name = (self.state.db.get("devices", device_id) or {}).get("name") or device_id
            self.state.event("worker_updated", "%s now runs the new worker.py" % name, device_id=device_id)
