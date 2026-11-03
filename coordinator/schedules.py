"""Cron schedules (SPEC §10) -- own 5-field matcher, no croniter.

``min hour dom mon dow`` with ``*``, lists (``1,2``), ranges (``1-5``) and steps
(``*/10``, ``1-30/5``).  Evaluated in **local** time.  When both day-of-month and
day-of-week are restricted a timestamp matches if *either* matches (this is what
Vixie cron does).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

log = logging.getLogger("coordinator.schedules")

#: (low, high) for the five fields.
FIELD_RANGES: Sequence[Tuple[int, int]] = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
FIELD_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")

MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
DOW_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}

#: How far ahead :func:`next_run` will look before giving up.
MAX_LOOKAHEAD_MINUTES = 366 * 24 * 60


class CronError(ValueError):
    """Raised for a malformed cron expression."""


def _alias(token: str, index: int) -> str:
    lowered = token.strip().lower()
    if index == 3 and lowered in MONTH_NAMES:
        return str(MONTH_NAMES[lowered])
    if index == 4 and lowered in DOW_NAMES:
        return str(DOW_NAMES[lowered])
    return lowered


def _parse_field(field: str, index: int) -> Set[int]:
    low, high = FIELD_RANGES[index]
    values: Set[int] = set()
    for part in str(field).split(","):
        part = part.strip()
        if not part:
            raise CronError("empty %s field" % FIELD_NAMES[index])
        step = 1
        if "/" in part:
            part, _, step_text = part.partition("/")
            try:
                step = int(step_text)
            except ValueError:
                raise CronError("bad step %r in %s" % (step_text, FIELD_NAMES[index]))
            if step <= 0:
                raise CronError("step must be positive in %s" % FIELD_NAMES[index])
            part = part.strip() or "*"
        if part == "*":
            start, end = low, high
        elif "-" in part.lstrip("-"):
            start_text, _, end_text = part.partition("-")
            try:
                start = int(_alias(start_text, index))
                end = int(_alias(end_text, index))
            except ValueError:
                raise CronError("bad range %r in %s" % (part, FIELD_NAMES[index]))
        else:
            try:
                start = end = int(_alias(part, index))
            except ValueError:
                raise CronError("bad value %r in %s" % (part, FIELD_NAMES[index]))
        if index == 4:
            if start == 7:
                start = 0
            if end == 7:
                end = 0
        if start > end:
            raise CronError("range %r is inverted in %s" % (part, FIELD_NAMES[index]))
        if start < low or end > high:
            raise CronError(
                "%s value out of range %d-%d: %r" % (FIELD_NAMES[index], low, high, part)
            )
        values.update(range(start, end + 1, step))
    if not values:
        raise CronError("no values for %s" % FIELD_NAMES[index])
    return values


class Cron:
    """A parsed 5-field cron expression."""

    __slots__ = ("expr", "minutes", "hours", "doms", "months", "dows", "dom_any", "dow_any")

    def __init__(self, expr: str) -> None:
        fields = str(expr or "").split()
        if len(fields) != 5:
            raise CronError("cron needs exactly 5 fields, got %d: %r" % (len(fields), expr))
        self.expr = " ".join(fields)
        self.minutes = _parse_field(fields[0], 0)
        self.hours = _parse_field(fields[1], 1)
        self.doms = _parse_field(fields[2], 2)
        self.months = _parse_field(fields[3], 3)
        self.dows = _parse_field(fields[4], 4)
        self.dom_any = fields[2].strip() == "*"
        self.dow_any = fields[4].strip() == "*"

    def matches(self, when: datetime) -> bool:
        if when.minute not in self.minutes:
            return False
        if when.hour not in self.hours:
            return False
        if when.month not in self.months:
            return False
        dow = (when.weekday() + 1) % 7  # Monday=0 -> cron Sunday=0
        dom_ok = when.day in self.doms
        dow_ok = dow in self.dows
        if self.dom_any and self.dow_any:
            return True
        if self.dom_any:
            return dow_ok
        if self.dow_any:
            return dom_ok
        return dom_ok or dow_ok

    def next_after(self, after: Optional[float] = None) -> Optional[float]:
        """First matching timestamp strictly after ``after`` (local time)."""
        base = datetime.fromtimestamp(after if after is not None else time.time())
        cursor = base.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(MAX_LOOKAHEAD_MINUTES):
            if self.matches(cursor):
                return cursor.timestamp()
            cursor += timedelta(minutes=1)
        return None  # pragma: no cover - impossible for valid expressions

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Cron(%r)" % self.expr


def parse(expr: str) -> Cron:
    return Cron(expr)


def validate(expr: str) -> str:
    """Raise :class:`CronError` unless ``expr`` is a valid 5-field cron."""
    return Cron(expr).expr


def matches(expr: str, when: Optional[datetime] = None) -> bool:
    return Cron(expr).matches(when or datetime.now())


def next_run(expr: str, after: Optional[float] = None) -> Optional[float]:
    return Cron(expr).next_after(after)


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------

TICK_SECONDS = 20.0


class ScheduleRunner:
    """Fires due schedules every :data:`TICK_SECONDS` (SPEC §10)."""

    def __init__(self, state: Any) -> None:
        self.state = state
        self.log = logging.getLogger("coordinator.schedules")
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._stop = asyncio.Event()
        self.refresh_all()
        self._task = asyncio.create_task(self._loop(), name="schedules")

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014
                pass

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=TICK_SECONDS)
                return
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:  # pragma: no cover
                return
            try:
                await self.tick()
            except asyncio.CancelledError:  # pragma: no cover
                return
            except Exception:
                self.log.exception("schedule tick failed")

    # ---------------------------------------------------------------- logic
    def refresh_all(self) -> None:
        """Fill in ``next_run`` for every enabled schedule that has none."""
        now = time.time()
        for row in self.state.db.select("schedules", "WHERE enabled=1"):
            if row.get("next_run"):
                continue
            try:
                nxt = next_run(row["cron"], now)
            except CronError:
                self.log.warning("schedule %s has an invalid cron %r", row["id"], row["cron"])
                continue
            self.state.db.update("schedules", {"id": row["id"]}, {"next_run": nxt})

    async def tick(self, now: Optional[float] = None) -> List[str]:
        """Submit every schedule whose ``next_run`` has passed."""
        now = time.time() if now is None else now
        fired: List[str] = []
        rows = self.state.db.select(
            "schedules", "WHERE enabled=1 AND next_run IS NOT NULL AND next_run<=?", (now,)
        )
        for row in rows:
            job_id = await self.fire(row, now)
            if job_id:
                fired.append(job_id)
        return fired

    async def fire(self, row: Dict[str, Any], now: Optional[float] = None) -> Optional[str]:
        """Submit a schedule's job spec right now and re-arm it."""
        now = time.time() if now is None else now
        spec = dict(row.get("job_spec") or {})
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
        base_name = spec.get("name") or row.get("name") or spec.get("type") or "job"
        spec["name"] = "%s @ %s" % (base_name, stamp)
        # a schedule runs the same job again on purpose, for fresh data: no
        # stored result is reused unless its spec asks for it (SPEC 9.8)
        options = dict(spec.get("options") or {})
        options.setdefault("cache", False)
        spec["options"] = options
        job_id: Optional[str] = None
        try:
            job = self.state.submit_job(spec, source={"schedule_id": row["id"]})
            job_id = job["id"]
            self.state.event(
                "schedule_fired",
                "schedule %s fired job %s" % (row.get("name"), job_id),
                job_id=job_id,
                data={"schedule_id": row["id"]},
            )
        except Exception as exc:
            self.log.exception("schedule %s could not submit its job", row.get("id"))
            self.state.event(
                "error",
                "schedule %s failed: %s" % (row.get("name"), exc),
                data={"schedule_id": row["id"]},
            )
        try:
            nxt = next_run(row["cron"], now)
        except CronError:
            nxt = None
        self.state.db.update(
            "schedules", {"id": row["id"]}, {"last_run": now, "next_run": nxt}
        )
        return job_id
