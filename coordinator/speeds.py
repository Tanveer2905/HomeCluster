"""Speed memory (SPEC §9.5): how fast each device works per kind of job.

Every finished task teaches the coordinator a rate in *units per second* for
``(speed key, device)``: the speed key names the work (``prime_range``,
``hash_search:sha256``, ``python_map:<hash of the function>`` ...), and a
unit is what the plugin counts (items, numbers, samples; 1 per task when the
plugin does not say).  The rate is a time-weighted average over roughly the
last ``WINDOW`` seconds of work: a 0.01 s task barely moves it, a phone that
heats up and slows down shows within a few pieces.  Rates survive restarts
in the ``speeds`` table.  A device the cluster has not seen doing a kind of
work yet gets a rate scaled by benchmark score from a device that has, of
the same runtime kind (Python vs JavaScript scores are not comparable).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .db import now

#: seconds of work the average remembers
WINDOW = 20.0


def bench_kind(device: Dict[str, Any]) -> str:
    bench = device.get("bench") if isinstance(device.get("bench"), dict) else {}
    kind = (bench or {}).get("kind")
    if kind:
        return str(kind)
    return "js" if device.get("platform") == "browser" else "python"


class SpeedMemory:
    def __init__(self, state: Any) -> None:
        self.state = state
        self.db = state.db

    def record(self, key: str, device_id: str, units: float, seconds: float) -> Optional[float]:
        """Learn from one finished task; returns the new rate."""
        if not key or not device_id or seconds <= 0 or units <= 0:
            return None
        seconds = float(seconds)
        row = self.db.row("SELECT rate, samples, seconds FROM speeds WHERE key=? AND device_id=?",
                          (key, device_id))
        if row is None:
            rate = float(units) / seconds
            self.db.execute(
                "INSERT INTO speeds (key, device_id, rate, samples, seconds, updated) VALUES (?,?,?,?,?,?)",
                (key, device_id, rate, 1, min(seconds, WINDOW), now()))
            return rate
        held = min(float(row["seconds"] or 1.0), WINDOW)
        old = float(row["rate"] or 0.0)
        rate = (old * held + float(units)) / (held + seconds)
        self.db.execute("UPDATE speeds SET rate=?, samples=?, seconds=?, updated=? WHERE key=? AND device_id=?",
                        (rate, int(row["samples"] or 0) + 1, min(held + seconds, WINDOW), now(), key, device_id))
        return rate

    def rates(self, key: str) -> Dict[str, float]:
        rows = self.db.rows("SELECT device_id, rate FROM speeds WHERE key=?", (key,))
        return {row["device_id"]: float(row["rate"]) for row in rows if row["rate"]}

    def rate(self, key: str, device_id: str) -> Optional[float]:
        row = self.db.row("SELECT rate FROM speeds WHERE key=? AND device_id=?", (key, device_id))
        return float(row["rate"]) if row is not None and row["rate"] else None

    def forget_device(self, device_id: str) -> None:
        self.db.execute("DELETE FROM speeds WHERE device_id=?", (device_id,))

    def listing(self) -> List[Dict[str, Any]]:
        names = {d["id"]: d.get("name") for d in self.db.select("devices")}
        rows = self.db.rows("SELECT * FROM speeds ORDER BY key, rate DESC")
        return [{"key": r["key"], "device_id": r["device_id"], "device": names.get(r["device_id"]),
                 "rate": round(float(r["rate"]), 4), "samples": int(r["samples"]),
                 "updated": r["updated"]} for r in rows]
