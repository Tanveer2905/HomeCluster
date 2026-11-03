"""The scheduler (SPEC §9).

Ticks every 0.5 s and on demand (:meth:`Scheduler.wake`).  One pass does, in
order: mark stale devices offline, promote ``waiting`` jobs whose dependencies
are done, split ``pending`` jobs into tasks, expire leases, move work off
devices that got too hot, assign queued tasks to devices that may take them,
start backup copies of straggling tasks on idle fast devices, then complete
finished jobs.

Speed (SPEC §9.5): devices are tried fastest first; a slow device is skipped
when the faster ones would finish the job's remaining tasks before it could
finish one; and when a device runs out of work while a slower one still holds
a task, the idle device runs a backup copy -- the first result wins.  So
adding a phone never makes a job slower than the fast devices alone.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import policy
from .cache import task_key
from .db import Database, dumps, loads, new_id, now
from .plugin_ctx import PluginContext
from .speeds import bench_kind
from .verify import benched

log = logging.getLogger("coordinator.scheduler")

TICK_SECONDS = 0.5
LEASE_GRACE = 60.0
CHECKPOINT_MAX_BYTES = 256 * 1024

ACTIVE_TASK_STATES = ("assigned", "running")

#: a backup copy must be expected to finish this much sooner than the original
BACKUP_MARGIN = 1.3
#: never back up a task younger than this (seconds)
BACKUP_MIN_AGE = 5.0
#: a slow device is skipped when faster ones need at most this share of its time
GATE_MARGIN = 0.9
#: automatic task sizing (SPEC §9.6): a piece aims at least this long (hand-over
#: costs ~0.1 s) unless that is more than the device's share of what is left ...
PIECE_MIN_SECONDS = 2.0
#: ... and at most this long, so a lost piece costs little and progress shows
PIECE_MAX_SECONDS = 60.0
#: each round hands out 1/FACTOR of the remaining work, by speed
PIECE_FACTOR = 2.0
#: never smaller than this much work, even at the end: hand-over costs ~0.07 s
#: and some work has a fixed cost per piece
PIECE_FLOOR_SECONDS = 0.5
#: before any speed is known: remaining / (PROBE_DIV x slots) per probe piece
PROBE_DIV = 10


# --------------------------------------------------------------------------
# $from_job resolution (SPEC §5)
# --------------------------------------------------------------------------

def walk_path(value: Any, path: Optional[str]) -> Any:
    """Follow a dotted ``a.b.0.c`` path into a JSON value."""
    if not path:
        return value
    node = value
    for part in str(path).split("."):
        if part == "":
            continue
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, (list, tuple)):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return node


def resolve_refs(value: Any, db: Database, keymap: Dict[str, str]) -> Any:
    """Replace every ``{"$from_job": ..., "path": ...}`` with the upstream result."""
    if isinstance(value, dict):
        if "$from_job" in value:
            ref = str(value.get("$from_job"))
            job_id = keymap.get(ref, ref)
            row = db.get("jobs", job_id)
            if row is None:
                raise ValueError("$from_job references unknown job %r" % ref)
            return walk_path(row.get("result"), value.get("path"))
        return {key: resolve_refs(item, db, keymap) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_refs(item, db, keymap) for item in value]
    return value


# --------------------------------------------------------------------------


class Scheduler:
    """Owns every job/task state transition."""

    def __init__(self, state: Any) -> None:
        self.state = state
        self.db: Database = state.db
        self.log = logging.getLogger("coordinator.scheduler")
        self._task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        #: task id -> {"device_id", "since", "attempt"}: backup copies in flight
        self.backups: Dict[str, Dict[str, Any]] = {}
        #: job id -> {"backups_started": n, "backups_won": n} for the speed report
        self._counts: Dict[str, Dict[str, int]] = {}
        #: job id -> {"state", "params"}: what a plugin's work() returned (memory only;
        #: rebuilt after a restart, the cursor lives in the job's options)
        self._work: Dict[str, Dict[str, Any]] = {}
        #: device id -> {request id: until}: devices answering a service call run
        #: no normal tasks meanwhile (SPEC §9.7)
        self.holds: Dict[str, Dict[str, float]] = {}
        self.ticks = 0

    @property
    def safety(self) -> policy.Safety:
        return self.state.safety

    @property
    def hot(self) -> Set[str]:
        """Devices resting after a heat limit (hysteresis, SPEC §9.3)."""
        return self.safety.warm

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="scheduler")

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def wake(self) -> None:
        """Ask for a tick as soon as the loop gets a chance."""
        try:
            self._wake.set()
        except RuntimeError:  # pragma: no cover - no loop
            pass

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=TICK_SECONDS)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:  # pragma: no cover
                return
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                await self.tick()
            except asyncio.CancelledError:  # pragma: no cover
                return
            except Exception:
                self.log.exception("scheduler tick failed")

    # ----------------------------------------------------------------- tick
    async def tick(self) -> None:
        self.ticks += 1
        self.mark_offline_devices()
        self.promote_waiting()
        self.split_pending()
        self.expire_leases()
        self.shed_unsafe()
        self.assign()
        self.backup_stragglers()
        self.finish_jobs()
        if self.ticks % 4 == 0:
            try:
                self.state.autoscaler.tick()
            except Exception:
                self.log.exception("autoscaler tick failed")
        if self.ticks % 10 == 0:
            self.state.verifier.expire()
        if self.ticks % 120 == 0:
            self.state.terminals.reap()
            self.state.peers.expire()

    # ------------------------------------------------------------- settings
    def _sched(self, key: str, default: Any) -> Any:
        return self.state.setting("scheduler.%s" % key, default)

    # -------------------------------------------------------------- devices
    def mark_offline_devices(self) -> List[str]:
        """A device that stopped polling goes offline and loses its tasks."""
        cutoff = now() - float(self.state.config.offline_after)
        rows = self.db.select(
            "devices", "WHERE removed=0 AND status='online' AND last_seen < ?", (cutoff,)
        )
        gone: List[str] = []
        for device in rows:
            self.db.update("devices", {"id": device["id"]}, {"status": "offline"})
            self.safety.forget(device["id"])
            self.drop_backups_on(device["id"])
            self.state.peers.drop_device(device["id"])
            self.state.terminals.drop_device(device["id"])
            self.requeue_device_tasks(device["id"], "device went offline")
            gone.append(device["id"])
            self.log.info("device %s (%s) went offline", device.get("name"), device["id"])
            self.state.event(
                "device_offline",
                "%s went offline" % device.get("name"),
                device_id=device["id"],
            )
            self.state.notify("device_offline", "%s went offline" % device.get("name"),
                              {"device_id": device["id"]})
            self.state.publish_device(device["id"])
        return gone

    def healthy(self, device: Dict[str, Any]) -> bool:
        """SPEC §9.3 battery + thermal gating with hysteresis (Balanced profile)."""
        status = self.safety.check(device)
        return status["temp_state"] == "ok" and status["battery_ok"]

    def inflight(self) -> Dict[str, int]:
        return {device_id: n["jobs"] + n["services"] for device_id, n in self.load().items()}

    def load(self) -> Dict[str, Dict[str, int]]:
        """Per device: running normal tasks (backup copies included) and services."""
        rows = self.db.rows(
            "SELECT device_id, json_type(payload, '$.service') IS NOT NULL AS svc, "
            "COUNT(*) AS n FROM tasks WHERE status IN ('assigned','running') "
            "AND device_id IS NOT NULL GROUP BY device_id, svc"
        )
        out: Dict[str, Dict[str, int]] = {}
        for row in rows:
            entry = out.setdefault(row["device_id"], {"jobs": 0, "services": 0})
            entry["services" if row["svc"] else "jobs"] += int(row["n"])
        for backup in self.backups.values():
            entry = out.setdefault(backup["device_id"], {"jobs": 0, "services": 0})
            entry["jobs"] += 1
        return out

    def available_devices(self) -> List[Dict[str, Any]]:
        """Online devices that may take normal tasks (``_free`` slots) or start
        models / services (``_ai``), fastest first.

        Normal slots follow the device's CPU limit with one core spare; next
        to a model or service a phone runs one normal task and a PC half its
        slots, so the model keeps its memory and cores.
        """
        rows = self.db.select("devices", "WHERE removed=0 AND status='online' AND paused=0")
        load = self.load()
        out: List[Dict[str, Any]] = []
        for device in rows:
            status = self.safety.check(device)
            used = load.get(device["id"], {"jobs": 0, "services": 0})
            cap = policy.slots_beside_a_model(device) if used["services"] else policy.job_slots(device)
            free = cap - used["jobs"] if status["jobs"] and not self.held(device["id"]) else 0
            device["_free"] = max(0, free)
            device["_ai"] = bool(status["ai_start"])
            if device["_free"] <= 0 and not device["_ai"]:
                continue
            out.append(device)
        out.sort(key=self._speed_key)
        return out

    # ------------------------------------------------- requests come first
    def hold(self, device_ids: Sequence[str], request_id: str, seconds: Optional[float] = None) -> None:
        """A service call is running on these devices: their normal tasks pause
        (the worker suspends them) and they get no new ones until ``release``
        or ``seconds`` pass (SPEC §9.7)."""
        if not self.state.setting("scheduler.requests_first", True):
            return
        seconds = float(seconds or self._sched("hold_seconds", 120))
        until = now() + seconds
        for device_id in dict.fromkeys(d for d in device_ids if d):
            self.holds.setdefault(device_id, {})[request_id] = until
            self.state.send_command(device_id, {"cmd": "hold", "request_id": request_id,
                                                "seconds": seconds})

    def release(self, request_id: str) -> None:
        for device_id, requests in list(self.holds.items()):
            if requests.pop(request_id, None) is not None:
                self.state.send_command(device_id, {"cmd": "release", "request_id": request_id})
            if not requests:
                self.holds.pop(device_id, None)
        self.wake()

    def held(self, device_id: str) -> bool:
        requests = self.holds.get(device_id)
        if not requests:
            return False
        stamp = now()
        for request_id, until in list(requests.items()):
            if until < stamp:
                requests.pop(request_id, None)
        if not requests:
            self.holds.pop(device_id, None)
            return False
        return True

    @staticmethod
    def _speed_key(device: Dict[str, Any]) -> Tuple[float, float]:
        score = device.get("bench_score")
        return (-(float(score) if score is not None else -1e18), device.get("created") or 0)

    # ---------------------------------------------------------------- jobs
    def promote_waiting(self) -> List[str]:
        """``waiting`` -> ``pending`` once every dependency is done (SPEC §9.1)."""
        promoted: List[str] = []
        for job in self.db.select("jobs", "WHERE status='waiting' ORDER BY created ASC"):
            options = job.get("options") or {}
            keymap = dict(options.get("_pipeline_keys") or {})
            deps = [keymap.get(str(d), str(d)) for d in (job.get("depends_on") or [])]
            statuses = []
            missing = False
            for dep_id in deps:
                row = self.db.get("jobs", dep_id)
                if row is None:
                    missing = True
                    break
                statuses.append(row["status"])
            if missing:
                self.fail_job(job["id"], "dependency failed: unknown job")
                continue
            if any(s in ("failed", "cancelled") for s in statuses):
                self.fail_job(job["id"], "dependency failed")
                continue
            if not all(s == "done" for s in statuses):
                continue
            try:
                params = resolve_refs(job.get("params") or {}, self.db, keymap)
            except Exception as exc:
                self.fail_job(job["id"], "could not resolve $from_job: %s" % exc)
                continue
            self.db.update(
                "jobs", {"id": job["id"]}, {"status": "pending", "params": params}
            )
            promoted.append(job["id"])
            self.state.publish_job(job["id"])
        return promoted

    def split_pending(self) -> List[str]:
        """Split ``pending`` jobs into tasks (SPEC §9.2)."""
        started: List[str] = []
        rows = self.db.select(
            "jobs", "WHERE status='pending' ORDER BY priority DESC, created ASC"
        )
        for job in rows:
            plugin = self.state.plugins.get(job["type"])
            if plugin is None:
                self.fail_job(job["id"], "unknown job type %r" % job["type"])
                continue
            options = dict(job.get("options") or {})
            targets = options.get("targets")
            if targets == "all" and not self.state.online_devices():
                continue  # wait for a device to show up
            params = dict(job.get("params") or {})
            options["_speed_key"] = plugin.speed_key(params)
            stamp = now()
            # never compute the same thing twice (SPEC 9.8)
            cache_mode = self.state.cache.mode(job, plugin)
            if cache_mode != "off":
                options["_cache_key"] = self.state.cache.job_key(job, plugin)
                hit = self.state.cache.get(options["_cache_key"]) if cache_mode == "on" else None
                if hit is not None:
                    self._finish_from_cache(job, options, hit, stamp)
                    started.append(job["id"])
                    continue
            ctx = PluginContext(self.state, job, options)
            try:
                info = plugin.work(params, options, ctx) if targets != "all" else None
                if info is not None and int(info.get("units") or 0) > 0:
                    self._start_dynamic(job, options, info, stamp)
                    started.append(job["id"])
                    continue
                payloads = [] if info is not None else plugin.split(params, options, ctx)
            except Exception as exc:
                self.log.warning("split failed for job %s: %s", job["id"], exc)
                self.fail_job(job["id"], "split failed: %s" % exc)
                continue
            max_attempts = int(options.get("max_attempts") or 3)
            timeout = options.get("timeout")
            streamed = self.state.results.enabled(job)
            reused = 0
            for idx, payload in enumerate(payloads):
                payload.setdefault("inputs", [])
                if not payload.get("timeout"):
                    payload["timeout"] = timeout
                fingerprint = task_key(payload) if cache_mode != "off" else None
                row: Dict[str, Any] = {
                    "id": new_id(),
                    "job_id": job["id"],
                    "idx": idx,
                    "payload": payload,
                    "status": "queued",
                    "device_id": None,
                    "attempts": 0,
                    "max_attempts": max_attempts,
                    "history": [],
                    "fingerprint": fingerprint,
                }
                hit = self.state.cache.get(fingerprint) if cache_mode == "on" else None
                if hit is not None:
                    # this exact task ran before: its answer is reused
                    result = hit.get("result")
                    if streamed:
                        result = {"streamed": self.state.results.write_part(job, idx, result)}
                    row.update({"status": "done", "result": result, "outputs": hit.get("outputs") or [],
                                "reused": 1, "finished_at": stamp})
                    reused += 1
                self.db.insert("tasks", row)
            self.db.update(
                "jobs",
                {"id": job["id"]},
                {
                    "status": "running",
                    "started": stamp,
                    "total_tasks": len(payloads),
                    "done_tasks": reused,
                    "failed_tasks": 0,
                    "options": options,
                },
            )
            message = "%s split into %d task(s)" % (job.get("name"), len(payloads))
            if reused:
                message += ", %d answered from earlier runs" % reused
            self.state.event("job_started", message, job_id=job["id"])
            started.append(job["id"])
            self.state.publish_job(job["id"])
        return started

    def _finish_from_cache(self, job: Dict[str, Any], options: Dict[str, Any], hit: Dict[str, Any],
                           stamp: float) -> None:
        """An identical job finished before: its result is this job's result."""
        options["_cached"] = {"from_job": hit.get("source_job"), "at": hit.get("created")}
        report = {"cached": True, "from_job": hit.get("source_job"),
                  "saved_s": round(float(hit.get("seconds") or 0.0), 3), "wall_s": 0.0}
        self.db.update("jobs", {"id": job["id"]}, {
            "status": "done", "started": stamp, "finished": stamp, "result": hit.get("result"),
            "error": None, "options": options, "report": report, "total_tasks": 0,
            "done_tasks": 0, "failed_tasks": 0,
        })
        self.state.event("job_cached", "%s: same as an earlier job, its result was reused"
                         % job.get("name"), job_id=job["id"], data={"from_job": hit.get("source_job")})
        self.state.notify("job_done", "Job finished: %s (reused an earlier result)" % job.get("name"),
                          {"job_id": job["id"], "type": job.get("type"), "cached": True})
        self.state.publish_job(job["id"])
        self.wake()

    # ----------------------------------------------- automatic task sizing
    def _start_dynamic(self, job: Dict[str, Any], options: Dict[str, Any], info: Dict[str, Any],
                       stamp: float) -> None:
        """No tasks up front: pieces are cut as devices free up (SPEC §9.6)."""
        options["_dynamic"] = {
            "units": int(info["units"]), "cursor": 0, "pieces": 0, "done": 0,
            "per_unit": float(info.get("per_unit") or 1), "grain": max(1, int(info.get("grain") or 1)),
        }
        self._work[job["id"]] = {"state": info.get("state"), "params": dict(job.get("params") or {})}
        self.db.update("jobs", {"id": job["id"]}, {
            "status": "running", "started": stamp, "total_tasks": 0, "done_tasks": 0,
            "failed_tasks": 0, "options": options,
        })
        self.state.event(
            "job_started",
            "%s: %d units, sized to each device's speed as it goes" % (job.get("name"), info["units"]),
            job_id=job["id"],
        )
        self.state.publish_job(job["id"])

    def dynamic_jobs(self) -> List[Dict[str, Any]]:
        """Running jobs with work still to cut, in priority order."""
        rows = self.db.rows(
            "SELECT id, name, type, priority, created, options FROM jobs WHERE status='running' "
            "AND json_extract(options, '$._dynamic.cursor') < json_extract(options, '$._dynamic.units') "
            "ORDER BY priority DESC, created ASC"
        )
        out = []
        for row in rows:
            job = dict(row)
            job["options"] = loads(job.get("options")) or {}
            out.append(job)
        return out

    def _work_state(self, job: Dict[str, Any]) -> Dict[str, Any]:
        cached = self._work.get(job["id"])
        if cached is None:  # after a restart: ask the plugin again
            full = self.db.get("jobs", job["id"]) or {}
            params = dict(full.get("params") or {})
            plugin = self.state.plugins.require(job["type"])
            info = plugin.work(params, job["options"], PluginContext(self.state, full, job["options"])) or {}
            cached = {"state": info.get("state"), "params": params}
            self._work[job["id"]] = cached
        return cached

    def carve(self, job: Dict[str, Any], devices: List[Dict[str, Any]], pool: List[Dict[str, Any]],
              book: "SpeedBook") -> int:
        """Cut pieces of a dynamic job for every device with a free slot."""
        options = job["options"]
        dyn = options["_dynamic"]
        plugin = self.state.plugins.get(job["type"])
        if plugin is None:
            return 0
        try:
            work = self._work_state(job)
            ctx = PluginContext(self.state, job, options)
            probe_end = min(dyn["units"], dyn["cursor"] + dyn["grain"])
            template = {"job_id": job["id"], "_job_options": options,
                        "payload": plugin.piece(work["params"], options, ctx, work["state"],
                                                dyn["cursor"], probe_end)}
        except Exception as exc:
            self.log.warning("cutting job %s failed: %s", job["id"], exc)
            self.fail_job(job["id"], "could not cut the work: %s" % exc)
            return 0
        made = 0
        max_attempts = int(options.get("max_attempts") or 3)
        for device in devices:
            while device["_free"] > 0 and dyn["cursor"] < dyn["units"]:
                if not self.eligible(template, device):
                    break
                size = self.piece_size(job, dyn, device, pool, book, template)
                if size is None:
                    break
                lo, hi = dyn["cursor"], dyn["cursor"] + size
                payload = plugin.piece(work["params"], options, ctx, work["state"], lo, hi)
                payload.setdefault("inputs", [])
                if not payload.get("timeout"):
                    payload["timeout"] = options.get("timeout")
                task = {"id": new_id(), "job_id": job["id"], "idx": dyn["pieces"], "payload": payload,
                        "status": "queued", "device_id": None, "attempts": 0,
                        "max_attempts": max_attempts, "history": []}
                self.db.insert("tasks", task)
                dyn["pieces"] += 1
                dyn["cursor"] = hi
                self.assign_task(dict(task, checkpoint=None), device)
                device["_free"] -= 1
                for member in pool:
                    if member["id"] == device["id"]:
                        member["_free"] -= 1
                made += 1
        if made:
            self.db.update("jobs", {"id": job["id"]}, {"options": options, "total_tasks": dyn["pieces"]})
            self.state.publish_job(job["id"])
        return made

    def piece_size(self, job: Dict[str, Any], dyn: Dict[str, Any], device: Dict[str, Any],
                   pool: List[Dict[str, Any]], book: "SpeedBook", template: Dict[str, Any]) -> Optional[int]:
        """How many units to give ``device`` now, or None when it is too slow
        to help: faster devices would finish everything left first.

        With known speeds every round hands out half of what is left, split
        by speed (weighted factoring), so pieces shrink towards the end and
        all devices finish together.  Unknown speeds get a small probe piece.
        """
        left = dyn["units"] - dyn["cursor"]
        grain = dyn["grain"]
        key = job["options"].get("_speed_key") or job["type"]
        per_unit = float(dyn.get("per_unit") or 1)
        eligible = [o for o in pool if self.eligible(template, o)]
        size: float
        if book.known(job["id"], key):
            rates = {o["id"]: (book.rate(job["id"], key, o) or 0.0) / per_unit for o in eligible}
            mine = (book.rate(job["id"], key, device) or 0.0) / per_unit
            if mine > 0:
                known = [r for r in rates.values() if r > 0]
                fill = min(known) if known else mine
                total = sum(o["_cap"] * (rates[o["id"]] or fill) for o in eligible) or mine
                share = left * mine / total
                size = max(share / PIECE_FACTOR, min(mine * PIECE_MIN_SECONDS, share),
                           mine * PIECE_FLOOR_SECONDS)
                size = min(size, mine * PIECE_MAX_SECONDS)
                faster = [o for o in eligible if o["id"] != device["id"] and rates[o["id"]] > mine]
                if faster:
                    rate = sum(o["_cap"] * rates[o["id"]] for o in faster)
                    wait = 0.0 if any(o["_free"] > 0 for o in faster) else PIECE_MIN_SECONDS / 2
                    if wait + left / rate <= min(size, left) / mine * GATE_MARGIN:
                        return None
            else:
                size = left / (PROBE_DIV * max(1, sum(o["_cap"] for o in eligible)))
        else:
            size = left / (PROBE_DIV * max(1, sum(o["_cap"] for o in eligible)))
        units = max(grain, int(math.ceil(size / grain)) * grain)
        if left - units < grain:
            units = left  # no crumb at the end
        return int(min(units, left))

    # --------------------------------------------------------------- leases
    def expire_leases(self) -> List[str]:
        stamp = now()
        rows = self.db.select(
            "tasks",
            "WHERE status IN ('assigned','running') AND lease_expires IS NOT NULL "
            "AND lease_expires < ?",
            (stamp,),
        )
        expired: List[str] = []
        for task in rows:
            device_id = task.get("device_id")
            if device_id:
                self.state.send_command(
                    device_id, {"cmd": "cancel", "task_id": task["id"]}
                )
            self.requeue(task, "lease expired")
            expired.append(task["id"])
        return expired

    # ------------------------------------------------------------ assigning
    def queued_tasks(self) -> List[Dict[str, Any]]:
        """Queued tasks of running jobs, in SPEC §9.3 order."""
        rows = self.db.rows(
            "SELECT t.*, j.priority AS _priority, j.created AS _job_created, "
            "j.options AS _job_options FROM tasks t JOIN jobs j ON j.id = t.job_id "
            "WHERE t.status='queued' AND j.status='running' "
            "ORDER BY j.priority DESC, j.created ASC, t.idx ASC"
        )
        out: List[Dict[str, Any]] = []
        for row in rows:
            task = dict(row)
            task["payload"] = loads(task.get("payload")) or {}
            task["history"] = loads(task.get("history")) or []
            task["result"] = loads(task.get("result"))
            task["outputs"] = loads(task.get("outputs"))
            task["checkpoint"] = loads(task.get("checkpoint"))
            task["_job_options"] = loads(task.get("_job_options")) or {}
            out.append(task)
        return out

    def eligible(self, task: Dict[str, Any], device: Dict[str, Any]) -> bool:
        """SPEC §6 eligibility: impl kind, requires and target constraints."""
        payload = task.get("payload") or {}
        caps = set(device.get("caps") or [])
        impls = payload.get("impls") or {}
        if not set(impls) & caps:
            return False
        if not set(payload.get("requires") or []) <= caps:
            return False
        pinned = payload.get("device_id")
        if pinned and str(pinned) != device["id"]:
            return False
        targets = (task.get("_job_options") or {}).get("targets")
        if isinstance(targets, (list, tuple)) and device["id"] not in [str(t) for t in targets]:
            return False
        # checks run elsewhere; a redone task skips the device that got it wrong
        if device["id"] in (payload.get("avoid") or ()):
            return False
        # too many wrong answers lately: only work pinned to it (SPEC 9.9)
        if not pinned and not isinstance(targets, (list, tuple)) and benched(device):
            return False
        return True

    def assign(self) -> int:
        """Hand queued tasks to devices, in task order, fastest device first.

        Service tasks (models) need a device that may start AI work; they do
        not use normal slots.  Normal tasks need a free slot and skip a slow
        device when faster ones would finish the job first (SPEC §9.5).
        """
        devices = self.available_devices()
        if not devices:
            return 0
        tasks = self.queued_tasks()
        dynamic = self.dynamic_jobs()
        if not tasks and not dynamic:
            return 0
        book = SpeedBook(self)
        pool = self.job_pool()
        left: Dict[str, int] = {}
        for task in tasks:
            left[task["job_id"]] = left.get(task["job_id"], 0) + 1
        # queued tasks and jobs still being cut, in one priority order; a
        # job's requeued pieces go before new ones
        sources: List[Tuple[Any, ...]] = [
            (-int(t.get("_priority") or 0), float(t.get("_job_created") or 0), 0, int(t["idx"]), i, t)
            for i, t in enumerate(tasks)]
        sources += [(-int(j.get("priority") or 0), float(j.get("created") or 0), 1, 0, len(tasks) + i, j)
                    for i, j in enumerate(dynamic)]
        sources.sort(key=lambda s: s[:5])
        count = 0
        for *_, item in sources:
            if "_dynamic" in (item.get("options") or {}):
                count += self.carve(item, devices, pool, book)
                continue
            task = item
            service = bool((task.get("payload") or {}).get("service"))
            picked = None
            for device in devices:
                if service:
                    if not device["_ai"]:
                        continue
                elif device["_free"] <= 0:
                    continue
                if not self.eligible(task, device):
                    continue
                if not service and self.too_slow(task, device, pool, book, left[task["job_id"]]):
                    continue
                picked = device
                break
            if picked is None:
                continue
            self.assign_task(task, picked)
            left[task["job_id"]] -= 1
            if not service:
                picked["_free"] -= 1
                for member in pool:
                    if member["id"] == picked["id"]:
                        member["_free"] -= 1
            count += 1
        return count

    def job_pool(self) -> List[Dict[str, Any]]:
        """Every device allowed to run normal tasks right now, busy or not."""
        load = self.load()
        out = []
        for device in self.db.select("devices", "WHERE removed=0 AND status='online' AND paused=0"):
            if not self.safety.check(device)["jobs"]:
                continue
            used = load.get(device["id"], {"jobs": 0, "services": 0})
            cap = policy.slots_beside_a_model(device) if used["services"] else policy.job_slots(device)
            device["_cap"] = cap
            device["_free"] = max(0, cap - used["jobs"])
            out.append(device)
        return out

    def too_slow(self, task: Dict[str, Any], device: Dict[str, Any], pool: List[Dict[str, Any]],
                 book: "SpeedBook", remaining: int) -> bool:
        """Would faster devices finish the job's ``remaining`` tasks before
        ``device`` finishes this one?  Then leave it for them."""
        job_id = task["job_id"]
        key = (task.get("_job_options") or {}).get("_speed_key") or ""
        speed = book.speed(job_id, key)
        mine = speed(device)
        if not mine:
            return False
        units = task_units(task)
        faster = []
        for other in pool:
            if other["id"] == device["id"] or not self.eligible(task, other):
                continue
            theirs = speed(other)
            if theirs and theirs > mine:
                faster.append((other, theirs))
        if not faster:
            return False
        rate = sum(other["_cap"] * theirs for other, theirs in faster)
        wait = 0.0 if any(other["_free"] > 0 for other, _ in faster) else min(units / t for _, t in faster) / 2
        return wait + remaining * units / rate <= units / mine * GATE_MARGIN

    # -------------------------------------------------------------- backups
    def backup_stragglers(self) -> int:
        """Idle device + nothing queued for it + a slower device still busy
        with a task: run a copy here, the first result wins (SPEC §9.5)."""
        self._expire_backups()
        idle = [d for d in self.available_devices() if d["_free"] > 0]
        if not idle:
            return 0
        rows = self.db.rows(
            "SELECT t.*, j.options AS _job_options FROM tasks t JOIN jobs j ON j.id = t.job_id "
            "WHERE t.status IN ('assigned','running') AND j.status='running' "
            "AND json_type(t.payload, '$.service') IS NULL"
        )
        if not rows:
            return 0
        running = []
        for row in rows:
            task = dict(row)
            task["payload"] = loads(task.get("payload")) or {}
            task["history"] = loads(task.get("history")) or []
            task["_job_options"] = loads(task.get("_job_options")) or {}
            running.append(task)
        queued = self.queued_tasks()
        owners = {d["id"]: d for d in self.db.select("devices", "WHERE removed=0")}
        book = SpeedBook(self)
        stamp = now()
        started = 0
        for device in idle:
            if any(self.eligible(q, device) for q in queued):
                continue  # it has real work waiting
            best: Optional[Tuple[float, Dict[str, Any]]] = None
            for task in running:
                if task["id"] in self.backups or task.get("device_id") == device["id"]:
                    continue
                if task["_job_options"].get("backups") is False or task["payload"].get("device_id"):
                    continue
                key = task["_job_options"].get("_speed_key") or ""
                if not self.eligible(task, device) or not book.known(task["job_id"], key):
                    continue
                age = stamp - float(task.get("started_at") or task.get("assigned_at") or stamp)
                if age < BACKUP_MIN_AGE:
                    continue
                units = task_units(task)
                my_rate = book.rate(task["job_id"], key, device)
                if not my_rate:
                    continue
                mine = units / my_rate
                owner = owners.get(task.get("device_id") or "")
                their_rate = book.rate(task["job_id"], key, owner) if owner else None
                theirs = units / their_rate if their_rate else None
                if theirs is None:
                    remaining = age
                elif theirs > age:
                    remaining = theirs - age
                else:
                    remaining = age / 2  # already late: assume it needs half as long again
                gain = remaining - mine * BACKUP_MARGIN
                if gain > 0 and (best is None or gain > best[0]):
                    best = (gain, task)
            if best is not None:
                self.start_backup(best[1], device)
                started += 1
        return started

    def start_backup(self, task: Dict[str, Any], device: Dict[str, Any]) -> None:
        attempt = int(task.get("attempts") or 0)
        self.backups[task["id"]] = {"device_id": device["id"], "since": now(),
                                    "attempt": attempt, "job_id": task["job_id"]}
        command: Dict[str, Any] = {
            "id": task["id"], "job_id": task["job_id"], "idx": task["idx"],
            "attempt": attempt, "payload": task.get("payload") or {}, "backup": True,
        }
        self.state.send_command(device["id"], {"cmd": "task", "task": command})
        owner = (self.db.get("devices", task.get("device_id") or "") or {}).get("name")
        self.state.event(
            "task_backup",
            "task %s is slow on %s: backup copy on %s" % (task["id"], owner, device.get("name")),
            job_id=task["job_id"], task_id=task["id"], device_id=device["id"],
        )
        self._count(task["job_id"], "backups_started")

    def _expire_backups(self) -> None:
        stamp = now()
        for task_id, backup in list(self.backups.items()):
            task = self.db.get("tasks", task_id)
            timeout = float(((task or {}).get("payload") or {}).get("timeout")
                            or self._sched("default_timeout", 600))
            if task is None or task["status"] not in ACTIVE_TASK_STATES \
                    or stamp - backup["since"] > timeout + LEASE_GRACE:
                self.cancel_backup(task_id)

    def cancel_backup(self, task_id: str) -> None:
        backup = self.backups.pop(task_id, None)
        if backup is not None:
            self.state.send_command(backup["device_id"], {"cmd": "cancel", "task_id": task_id})

    def drop_backups_on(self, device_id: str) -> None:
        for task_id, backup in list(self.backups.items()):
            if backup["device_id"] == device_id:
                self.backups.pop(task_id, None)

    def _count(self, job_id: str, key: str) -> None:
        counts = self._counts.setdefault(job_id, {})
        counts[key] = counts.get(key, 0) + 1

    # --------------------------------------------------------------- safety
    def shed_unsafe(self) -> int:
        """Move work off devices that crossed a safety limit (SPEC §9.3).

        Too hot: every task leaves.  AI switched off, master switch off or an
        unplugged battery under the model floor: models and services leave.
        The moved tasks keep their attempts; they wait for another device, or
        for this one to cool down when they are pinned to it.
        """
        moved = 0
        for device in self.db.select("devices", "WHERE removed=0 AND status='online'"):
            status = self.safety.check(device)
            if status["shed"]:
                services_only = False
            elif not status["ai_keep"]:
                services_only = True
            else:
                continue
            reason = status["notes"][0] if status["notes"] else "device limit reached"
            here = 0
            for task in self.db.select(
                "tasks", "WHERE device_id=? AND status IN ('assigned','running')", (device["id"],)
            ):
                if services_only and not (task.get("payload") or {}).get("service"):
                    continue
                self.state.send_command(device["id"], {"cmd": "cancel", "task_id": task["id"]})
                self.requeue(task, "%s: %s" % (device.get("name"), reason), charge=False)
                here += 1
            if not services_only:
                for task_id, backup in list(self.backups.items()):
                    if backup["device_id"] == device["id"]:
                        self.cancel_backup(task_id)
            if here:
                moved += here
                self.state.event(
                    "device_rested",
                    "%s: %s; %d task(s) moved off" % (device.get("name"), reason, here),
                    device_id=device["id"],
                )
        return moved

    def assign_task(self, task: Dict[str, Any], device: Dict[str, Any]) -> None:
        payload = task.get("payload") or {}
        timeout = float(payload.get("timeout") or self._sched("default_timeout", 600))
        attempt = int(task.get("attempts") or 0) + 1
        stamp = now()
        self.db.update(
            "tasks",
            {"id": task["id"]},
            {
                "status": "assigned",
                "device_id": device["id"],
                "attempts": attempt,
                "assigned_at": stamp,
                "started_at": None,
                "finished_at": None,
                "lease_expires": stamp + timeout + LEASE_GRACE,
                "error": None,
            },
        )
        command: Dict[str, Any] = {
            "id": task["id"],
            "job_id": task["job_id"],
            "idx": task["idx"],
            "attempt": attempt,
            "payload": payload,
        }
        if task.get("checkpoint") is not None:
            # Resume where an earlier attempt left off (SPEC §6.5).
            command["checkpoint"] = task["checkpoint"]
        if "peer" in (device.get("caps") or []):
            # big inputs another phone already has come from it (SPEC 8.3)
            peer_inputs = self.state.peers.peer_inputs(payload, device["id"])
            if peer_inputs:
                command["peer_inputs"] = peer_inputs
        self.state.send_command(device["id"], {"cmd": "task", "task": command})
        self.log.debug(
            "task %s (job %s idx %s) -> %s attempt %d",
            task["id"], task["job_id"], task["idx"], device.get("name"), attempt,
        )
        self.state.publish_task(task["id"])

    def refresh_leases(self, device_id: str, task_ids: List[str]) -> int:
        """Extend the lease of tasks a polling device still reports as running."""
        stamp = now()
        touched = 0
        for task_id in task_ids:
            task = self.db.get("tasks", task_id)
            if task is None or task.get("device_id") != device_id:
                continue
            if task["status"] not in ACTIVE_TASK_STATES:
                continue
            payload = task.get("payload") or {}
            timeout = float(payload.get("timeout") or 0)
            horizon = (timeout + LEASE_GRACE) if timeout > 0 else max(
                LEASE_GRACE, 3 * float(self.state.config.poll_timeout) + LEASE_GRACE
            )
            self.db.update("tasks", {"id": task_id}, {"lease_expires": stamp + horizon})
            touched += 1
        return touched

    # ------------------------------------------------------- fault handling
    def requeue(self, task: Dict[str, Any], reason: str, charge: bool = True) -> str:
        """Requeue a task, or fail it when its attempts ran out (SPEC §9.4).

        ``charge=False`` (a safety pause, not a failure) gives the attempt back.
        A backup copy still running elsewhere takes over instead of a requeue.
        """
        task = self.db.get("tasks", task["id"]) or task
        if task.get("status") in ("done", "cancelled"):
            return str(task.get("status"))
        history = list(task.get("history") or [])
        history.append(
            {
                "device_id": task.get("device_id"),
                "attempt": task.get("attempts"),
                "error": reason,
                "ts": now(),
            }
        )
        attempts = int(task.get("attempts") or 0)
        backup = self.backups.pop(task["id"], None)
        if backup is not None:
            holder = self.db.get("devices", backup["device_id"]) or {}
            if holder.get("status") == "online" and not holder.get("removed"):
                timeout = float((task.get("payload") or {}).get("timeout")
                                or self._sched("default_timeout", 600))
                self.db.update("tasks", {"id": task["id"]}, {
                    "status": "running", "device_id": backup["device_id"],
                    "assigned_at": backup["since"], "started_at": backup["since"],
                    "lease_expires": now() + timeout + LEASE_GRACE, "error": None,
                    "history": history,
                })
                self.state.publish_task(task["id"])
                return "running"
        if not charge:
            attempts = max(0, attempts - 1)
        max_attempts = int(task.get("max_attempts") or 3)
        if charge and attempts >= max_attempts:
            return self.fail_task(task, reason, history)
        self.db.update(
            "tasks",
            {"id": task["id"]},
            {
                "status": "queued",
                "device_id": None,
                "assigned_at": None,
                "started_at": None,
                "lease_expires": None,
                "error": reason,
                "history": history,
                "attempts": attempts,
            },
        )
        self.state.event(
            "task_requeued",
            "task %s requeued: %s" % (task["id"], reason),
            job_id=task.get("job_id"),
            task_id=task["id"],
            device_id=task.get("device_id"),
        )
        self.state.publish_task(task["id"])
        self.wake()
        return "queued"

    def fail_task(
        self,
        task: Dict[str, Any],
        reason: str,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        if history is None:
            history = list(task.get("history") or [])
        self.db.update(
            "tasks",
            {"id": task["id"]},
            {
                "status": "failed",
                "error": reason,
                "finished_at": now(),
                "lease_expires": None,
                "history": history,
            },
        )
        if task.get("role"):
            # a check that could not run: no verdict, and the job goes on (SPEC 9.9)
            job = self.db.get("jobs", task["job_id"]) or {"id": task["job_id"]}
            self.state.verifier.role_failed(job, task, reason)
            self.state.publish_task(task["id"])
            self.wake()
            return "failed"
        self.db.execute(
            "UPDATE jobs SET failed_tasks = failed_tasks + 1 WHERE id=?",
            (task["job_id"],),
        )
        self.state.event(
            "task_failed",
            "task %s failed: %s" % (task["id"], reason),
            job_id=task.get("job_id"),
            task_id=task["id"],
            device_id=task.get("device_id"),
        )
        self.state.publish_task(task["id"])
        job = self.db.get("jobs", task["job_id"])
        if job and (job.get("options") or {}).get("fail_fast"):
            self.cancel_remaining(task["job_id"], "fail_fast")
        self.wake()
        return "failed"

    def requeue_device_tasks(self, device_id: str, reason: str) -> List[str]:
        rows = self.db.select(
            "tasks", "WHERE device_id=? AND status IN ('assigned','running')", (device_id,)
        )
        return [task["id"] for task in rows if self.requeue(task, reason)]

    def cancel_remaining(self, job_id: str, reason: str) -> List[str]:
        """Cancel every task of a job that has not finished yet."""
        cancelled: List[str] = []
        rows = self.db.select(
            "tasks",
            "WHERE job_id=? AND status IN ('queued','assigned','running')",
            (job_id,),
        )
        for task in rows:
            if task.get("device_id") and task["status"] in ACTIVE_TASK_STATES:
                self.state.send_command(
                    task["device_id"], {"cmd": "cancel", "task_id": task["id"]}
                )
            self.cancel_backup(task["id"])
            self.db.update(
                "tasks",
                {"id": task["id"]},
                {
                    "status": "cancelled",
                    "finished_at": now(),
                    "lease_expires": None,
                    "error": reason,
                },
            )
            cancelled.append(task["id"])
            self.state.publish_task(task["id"])
        return cancelled

    # ------------------------------------------------------- worker results
    def task_started(self, task_id: str, device: Dict[str, Any]) -> bool:
        task = self.db.get("tasks", task_id)
        if task is None or task.get("device_id") != device["id"]:
            return False
        if task["status"] != "assigned":
            return task["status"] == "running"
        self.db.update(
            "tasks", {"id": task_id}, {"status": "running", "started_at": now()}
        )
        self.state.publish_task(task_id)
        return True

    def task_checkpoint(
        self, task_id: str, device: Dict[str, Any], state: Any, attempt: Optional[int]
    ) -> Tuple[int, Dict[str, Any]]:
        """Store resumable progress reported by the device running the task."""
        task = self.db.get("tasks", task_id)
        if task is None:
            return 404, {"error": "unknown task"}
        if task.get("device_id") != device["id"] or task["status"] not in ACTIVE_TASK_STATES:
            return 409, {"error": "task is not running on this device"}
        if attempt is not None and int(attempt) != int(task.get("attempts") or 0):
            return 409, {"error": "stale attempt"}
        encoded = dumps(state)
        if encoded is not None and len(encoded) > CHECKPOINT_MAX_BYTES:
            return 413, {"error": "checkpoint larger than %d bytes" % CHECKPOINT_MAX_BYTES}
        self.db.update("tasks", {"id": task_id}, {"checkpoint": state})
        return 200, {"ok": True}

    def task_done(
        self, task_id: str, device: Dict[str, Any], body: Dict[str, Any]
    ) -> Tuple[int, Dict[str, Any]]:
        """Handle ``POST /api/w/task/{id}/done``.  Returns ``(status, payload)``.

        A *late* result is one from a device that no longer holds the task:
        it went offline (or its lease expired) mid-task, the task was requeued
        and maybe reassigned, and the original device delivered its result
        from its outbox once it reconnected.  A late **success** is accepted
        (and any duplicate attempt cancelled) so phone work is never wasted;
        a late **failure** is ignored because the task was already requeued.
        """
        task = self.db.get("tasks", task_id)
        if task is None:
            return 404, {"error": "unknown task"}
        if task["status"] == "cancelled":
            return 200, {"ok": True, "ignored": "cancelled"}
        if task["status"] in ("done", "failed"):
            return 409, {"error": "task already finished"}
        attempt = body.get("attempt")
        current = int(task.get("attempts") or 0)
        backup = self.backups.get(task_id)
        # a backup copy (SPEC §9.5) reporting before the original
        from_backup = (backup is not None and backup["device_id"] == device["id"]
                       and task["status"] in ACTIVE_TASK_STATES)
        if from_backup and not body.get("ok"):
            self.backups.pop(task_id, None)
            return 200, {"ok": True, "ignored": "backup failed"}
        # Late = from a device that no longer holds the task, or from an
        # earlier attempt on the device that got the task back after a requeue.
        late = not from_backup and (task.get("device_id") != device["id"] or (
            attempt is not None and int(attempt) < current
        ))
        if from_backup:
            pass
        elif late:
            ran_here = any(
                h.get("device_id") == device["id"] for h in (task.get("history") or [])
            )
            if not ran_here:
                return 409, {"error": "task is not assigned to this device"}
            if not body.get("ok"):
                return 200, {"ok": True, "ignored": "late failure"}
        elif attempt is not None and int(attempt) != current:
            return 409, {"error": "stale attempt"}

        duration = body.get("duration")
        if body.get("ok"):
            outputs = list(body.get("outputs") or [])
            extra: Dict[str, Any] = {}
            if late and task.get("device_id") and task["status"] in ACTIVE_TASK_STATES:
                # Someone else is redoing it: stop them, the work is already done.
                self.state.send_command(
                    task["device_id"], {"cmd": "cancel", "task_id": task_id}
                )
            if from_backup:
                # the copy won: stop the original and note it for the speed report
                self.backups.pop(task_id, None)
                primary = task.get("device_id")
                if primary and primary != device["id"]:
                    self.state.send_command(primary, {"cmd": "cancel", "task_id": task_id})
                history = list(task.get("history") or [])
                history.append({"device_id": primary, "attempt": current, "ts": now(),
                                "error": "backup on %s finished first" % (device.get("name") or device["id"]),
                                "backup_won_by": device["id"]})
                extra["history"] = history
                self._count(task["job_id"], "backups_won")
            elif task_id in self.backups:
                self.cancel_backup(task_id)  # the original finished first
            job = self.db.get("jobs", task["job_id"]) or {}
            impl = self.state.verifier.impl_of(task, device, body.get("impl"))
            if task.get("role"):
                return self._role_done(job, task, device, body, extra, impl)
            stored_result = body.get("result")
            if self.state.results.enabled(job):
                # Streamed results: items go to disk now, not into SQLite.
                try:
                    n = self.state.results.write_part(job, int(task["idx"]), stored_result)
                    stored_result = {"streamed": n}
                except Exception as exc:
                    self.log.exception("could not stream results of task %s", task_id)
                    stored_result = {"streamed": 0, "error": str(exc)}
            work = body.get("work_duration")
            fields = {
                "status": "done",
                "device_id": device["id"],
                "result": stored_result,
                "outputs": outputs,
                "error": None,
                "finished_at": now(),
                "duration": duration,
                "work_seconds": work,
                "lease_expires": None,
                "checkpoint": None,
                "impl": impl,
            }
            fields.update(extra)
            self.db.update("tasks", {"id": task_id}, fields)
            self.db.execute(
                "UPDATE jobs SET done_tasks = done_tasks + 1 WHERE id=?", (task["job_id"],)
            )
            # speed = the task's own working time, not interpreter start-up
            self._learn(job, task, device, work if work else duration)
            for output in outputs:
                if output.get("file_id"):
                    self.state.files.add_ref(str(output["file_id"]))
            self.state.peers.fetched_inputs(device, task.get("payload") or {})
            plugin = self.state.plugins.get(str(job.get("type") or ""))
            if task.get("fingerprint") and self.state.cache.mode(job, plugin) != "off":
                self.state.cache.put(task["fingerprint"], "task", str(job.get("type")), body.get("result"),
                                     outputs, [device["id"]], job.get("id"), work or duration)
            # catching wrong answers: maybe run it again on another device (SPEC 9.9)
            self.state.verifier.maybe_check(job, plugin, task, device, body, impl)
            if late:
                self.state.event(
                    "task_late_result",
                    "task %s completed by %s after being requeued"
                    % (task_id, device.get("name") or device["id"]),
                    job_id=task.get("job_id"),
                    task_id=task_id,
                    device_id=device["id"],
                )
            self.state.publish_task(task_id)
            self.wake()
            payload: Dict[str, Any] = {"ok": True}
            if late:
                payload["late"] = True
            return 200, payload

        error = str(body.get("error") or "task failed")
        if error == "cancelled":
            self.db.update(
                "tasks",
                {"id": task_id},
                {"status": "cancelled", "finished_at": now(), "lease_expires": None,
                 "error": error},
            )
            self.state.publish_task(task_id)
            self.wake()
            return 200, {"ok": True, "ignored": "cancelled"}
        outcome = self.requeue(task, error)
        return 200, {"ok": True, "outcome": outcome}

    def _role_done(self, job: Dict[str, Any], task: Dict[str, Any], device: Dict[str, Any],
                   body: Dict[str, Any], extra: Dict[str, Any], impl: Optional[str]) -> Tuple[int, Dict[str, Any]]:
        """A check copy finished (SPEC 9.9): keep its own answer next to the
        original's and let the verifier compare them."""
        work = body.get("work_duration")
        fields = {
            "status": "done", "device_id": device["id"], "result": body.get("result"),
            "outputs": list(body.get("outputs") or []), "error": None, "finished_at": now(),
            "duration": body.get("duration"), "work_seconds": work, "lease_expires": None,
            "checkpoint": None, "impl": impl,
        }
        fields.update(extra)
        self.db.update("tasks", {"id": task["id"]}, fields)
        self._learn(job, task, device, work or body.get("duration"))
        fresh = self.db.get("tasks", task["id"]) or task
        try:
            outcome = self.state.verifier.role_done(job, fresh, device, body)
        except Exception:  # a check must never break the job
            self.log.exception("comparing check %s failed", task["id"])
            outcome = "error"
        self.state.publish_task(task["id"])
        self.wake()
        return 200, {"ok": True, "check": outcome}

    def redo(self, task: Dict[str, Any], avoid: Optional[str] = None) -> None:
        """Compute a finished task again, never on ``avoid`` (SPEC 9.9)."""
        payload = dict(task.get("payload") or {})
        if avoid:
            payload["avoid"] = list(dict.fromkeys(list(payload.get("avoid") or []) + [avoid]))
        history = list(task.get("history") or [])
        history.append({"device_id": task.get("device_id"), "attempt": task.get("attempts"), "ts": now(),
                        "error": "computed again: this device gave a wrong answer elsewhere"})
        self.db.update("tasks", {"id": task["id"]}, {
            "status": "queued", "device_id": None, "attempts": 0, "assigned_at": None,
            "started_at": None, "finished_at": None, "lease_expires": None, "error": None,
            "result": None, "outputs": [], "verified": None, "result_hash": None,
            "payload": payload, "history": history,
        })
        self.db.execute("UPDATE jobs SET done_tasks = MAX(0, done_tasks - 1) WHERE id=?", (task["job_id"],))
        job = self.db.get("jobs", task["job_id"]) or {}
        options = job.get("options") or {}
        dyn = options.get("_dynamic")
        if dyn:
            per_unit = float(dyn.get("per_unit") or 1)
            dyn["done"] = max(0, int(dyn.get("done") or 0) - int(round(task_units(task) / per_unit)))
            self.db.update("jobs", {"id": job["id"]}, {"options": options})
        self.state.publish_task(task["id"])

    def _learn(self, job: Dict[str, Any], task: Dict[str, Any], device: Dict[str, Any], duration: Any) -> None:
        """Speed memory and progress of automatically sized jobs (SPEC §9.5/§9.6)."""
        payload = task.get("payload") or {}
        if payload.get("service"):
            return
        units = task_units(task)
        options = job.get("options") or {}
        try:
            seconds = float(duration or 0)
        except (TypeError, ValueError):
            seconds = 0.0
        if seconds > 0:
            self.state.speeds.record(options.get("_speed_key") or str(job.get("type") or ""),
                                     device["id"], units, seconds)
        dyn = options.get("_dynamic")
        if dyn and not task.get("role"):
            per_unit = float(dyn.get("per_unit") or 1)
            dyn["done"] = min(int(dyn.get("units") or 0),
                              int(dyn.get("done") or 0) + int(round(units / per_unit)))
            # only the progress counter changes; re-read so a concurrent cut is not undone
            fresh = (self.db.get("jobs", job["id"]) or {}).get("options") or {}
            if "_dynamic" in fresh:
                fresh["_dynamic"]["done"] = dyn["done"]
                self.db.update("jobs", {"id": job["id"]}, {"options": fresh})

    def cancel_task(self, task_id: str, reason: str = "cancelled") -> bool:
        task = self.db.get("tasks", task_id)
        if task is None or task["status"] in ("done", "failed", "cancelled"):
            return False
        if task.get("device_id") and task["status"] in ACTIVE_TASK_STATES:
            self.state.send_command(
                task["device_id"], {"cmd": "cancel", "task_id": task_id}
            )
        self.cancel_backup(task_id)
        self.db.update(
            "tasks",
            {"id": task_id},
            {"status": "cancelled", "finished_at": now(), "lease_expires": None,
             "error": reason},
        )
        self.state.publish_task(task_id)
        self.wake()
        return True

    # ---------------------------------------------------------- completion
    def task_results(self, job_id: str) -> List[Dict[str, Any]]:
        rows = self.db.select("tasks", "WHERE job_id=? AND role IS NULL ORDER BY idx ASC", (job_id,))
        out: List[Dict[str, Any]] = []
        for task in rows:
            if task["status"] == "cancelled":
                continue
            payload = task.get("payload") or {}
            out.append(
                {
                    "idx": task["idx"],
                    "device_id": task.get("device_id"),
                    "ok": task["status"] == "done",
                    "result": task.get("result"),
                    "outputs": task.get("outputs") or [],
                    "error": task.get("error"),
                    "duration": task.get("duration"),
                    "meta": payload.get("meta") or {},
                }
            )
        return out

    def finish_jobs(self) -> List[str]:
        finished: List[str] = []
        for job in self.db.select("jobs", "WHERE status='running'"):
            dyn = (job.get("options") or {}).get("_dynamic")
            if dyn and int(dyn.get("cursor") or 0) < int(dyn.get("units") or 0):
                continue  # still being cut into pieces
            pending = int(self.db.scalar(
                "SELECT COUNT(*) FROM tasks WHERE job_id=? AND status IN ('queued','assigned','running')",
                (job["id"],), default=0))
            if pending:
                continue  # checks of its tasks included: a wrong answer is fixed before the end
            counts = {
                row["status"]: int(row["n"])
                for row in self.db.rows(
                    "SELECT status, COUNT(*) AS n FROM tasks WHERE job_id=? AND role IS NULL "
                    "GROUP BY status",
                    (job["id"],),
                )
            }
            done = counts.get("done", 0)
            failed = counts.get("failed", 0)
            self.db.update(
                "jobs", {"id": job["id"]}, {"done_tasks": done, "failed_tasks": failed}
            )
            if failed:
                errors = [
                    row["error"]
                    for row in self.db.select(
                        "tasks", "WHERE job_id=? AND status='failed' AND role IS NULL ORDER BY idx LIMIT 3",
                        (job["id"],),
                    )
                ]
                self.fail_job(job["id"], "; ".join(str(e) for e in errors if e) or "task failed")
                finished.append(job["id"])
                continue
            self.complete_job(job)
            finished.append(job["id"])
        return finished

    def complete_job(self, job: Dict[str, Any]) -> None:
        plugin = self.state.plugins.get(job["type"])
        options = dict(job.get("options") or {})
        try:
            if self.state.results.enabled(job):
                # Streamed results replace reduce(): join the parts into one file.
                result = self.state.results.finish(job)
            else:
                results = self.task_results(job["id"])
                ctx = PluginContext(self.state, job, options)
                result = (
                    plugin.reduce(dict(job.get("params") or {}), results, ctx)
                    if plugin is not None
                    else [r.get("result") for r in results]
                )
        except Exception as exc:
            self.log.exception("reduce failed for job %s", job["id"])
            self.fail_job(job["id"], "reduce failed: %s" % exc)
            return
        finished = now()
        self.db.update(
            "jobs",
            {"id": job["id"]},
            {"status": "done", "finished": finished, "result": result, "error": None},
        )
        key = options.get("_cache_key")
        if key and self.state.cache.mode(job, plugin) != "off":
            devices = [row["device_id"] for row in self.db.rows(
                "SELECT DISTINCT device_id FROM tasks WHERE job_id=? AND status='done' "
                "AND device_id IS NOT NULL AND role IS NULL", (job["id"],))]
            seconds = finished - float(job.get("started") or finished)
            self.state.cache.put(key, "job", str(job.get("type")), result, [], devices, job["id"], seconds)
        try:
            report = self.job_report(job["id"])
        except Exception:  # the report is a nicety, never a reason to fail a job
            self.log.exception("speed report for job %s failed", job["id"])
            report = None
        self._counts.pop(job["id"], None)
        self._work.pop(job["id"], None)
        if report is not None:
            self.db.update("jobs", {"id": job["id"]}, {"report": report})
        self.state.event("job_done", "%s finished" % job.get("name"), job_id=job["id"])
        self.state.notify(
            "job_done",
            "Job finished: %s" % job.get("name"),
            {"job_id": job["id"], "type": job.get("type")},
        )
        self.state.publish_job(job["id"])
        self.wake()

    def job_report(self, job_id: str) -> Optional[Dict[str, Any]]:
        """How the cluster did against the fastest single device (SPEC §9.5).

        Each device's speed comes from the tasks it finished in this job; the
        time a device would have needed alone is every task's duration scaled
        to that device's speed, spread over its normal slots, plus the same
        per-task hand-over time (dispatch, upload) the cluster paid.
        """
        job = self.db.get("jobs", job_id)
        if job is None or not job.get("started") or not job.get("finished"):
            return None
        timed = [t for t in self.db.select("tasks", "WHERE job_id=? AND status='done' AND role IS NULL",
                                           (job_id,))
                 if t.get("duration") and t.get("device_id")]
        reused = int(self.db.scalar("SELECT COUNT(*) FROM tasks WHERE job_id=? AND reused=1",
                                    (job_id,), default=0))
        if not timed:
            return {"reused_tasks": reused, "wall_s": 0.0} if reused else None
        devices = {d["id"]: d for d in self.db.select("devices")}
        per: Dict[str, Dict[str, Any]] = {}
        for task in timed:
            entry = per.setdefault(task["device_id"], {"tasks": 0, "busy_s": 0.0, "units": 0.0})
            entry["tasks"] += 1
            entry["busy_s"] += float(task["duration"])
            entry["units"] += task_units(task)
        average = {d: e["busy_s"] / e["tasks"] for d, e in per.items()}
        rates = {d: e["units"] / e["busy_s"] for d, e in per.items() if e["busy_s"] > 0}
        total_units = sum(e["units"] for e in per.values())
        gaps = sorted(max(0.0, float(t["finished_at"]) - float(t["assigned_at"]) - float(t["duration"]))
                      for t in timed if t.get("assigned_at") and t.get("finished_at"))
        handover = gaps[len(gaps) // 2] if gaps else 0.0
        alone: Dict[str, float] = {}
        for device_id, rate in rates.items():
            device = devices.get(device_id)
            slots = policy.job_slots(device) if device else 1
            alone[device_id] = total_units / (rate * slots) + math.ceil(len(timed) / slots) * handover
        if not alone:
            return None
        best = min(alone, key=lambda d: alone[d])
        wall = max(0.001, float(job["finished"]) - float(job["started"]))
        counts = self._counts.get(job_id, {})
        won = sum(1 for t in timed for h in (t.get("history") or []) if h.get("backup_won_by"))
        rows = []
        for device_id, entry in sorted(per.items(), key=lambda kv: -kv[1]["units"]):
            rows.append({
                "device_id": device_id,
                "name": (devices.get(device_id) or {}).get("name"),
                "tasks": entry["tasks"],
                "units": round(entry["units"], 3),
                "busy_s": round(entry["busy_s"], 3),
                "avg_s": round(average[device_id], 3),
                "rate": round(rates.get(device_id) or 0.0, 3),
            })
        return {
            "wall_s": round(wall, 3),
            "task_time_s": round(sum(e["busy_s"] for e in per.values()), 3),
            "devices": rows,
            "fastest_device": {"device_id": best, "name": (devices.get(best) or {}).get("name"),
                               "alone_s": round(alone[best], 3)},
            "speedup": round(alone[best] / wall, 2),
            "handover_s": round(handover, 3),
            "single_device": len(per) == 1,
            "backups": {"started": counts.get("backups_started", 0), "won": won},
            "reused_tasks": reused,
            "checks": self.state.verifier.summary(job_id),
        }

    def fail_job(self, job_id: str, error: str) -> None:
        job = self.db.get("jobs", job_id)
        if job is None or job["status"] in ("done", "failed", "cancelled"):
            return
        self.db.update(
            "jobs",
            {"id": job_id},
            {"status": "failed", "finished": now(), "error": error},
        )
        self._work.pop(job_id, None)
        self.cancel_remaining(job_id, "job failed")
        self.state.event(
            "job_failed", "%s failed: %s" % (job.get("name"), error), job_id=job_id
        )
        self.state.notify(
            "job_failed",
            "Job failed: %s (%s)" % (job.get("name"), error),
            {"job_id": job_id, "error": error},
        )
        self.state.publish_job(job_id)

    def cancel_job(self, job_id: str, reason: str = "cancelled by user") -> bool:
        job = self.db.get("jobs", job_id)
        if job is None or job["status"] in ("done", "failed", "cancelled"):
            return False
        self.db.update(
            "jobs",
            {"id": job_id},
            {"status": "cancelled", "finished": now(), "error": reason},
        )
        self._work.pop(job_id, None)
        self.cancel_remaining(job_id, reason)
        self.state.event("job_cancelled", "%s cancelled" % job.get("name"), job_id=job_id)
        self.state.publish_job(job_id)
        self.wake()
        return True

    def retry_job(self, job_id: str) -> bool:
        """Requeue failed tasks and put the job back to ``running`` (SPEC §11)."""
        job = self.db.get("jobs", job_id)
        if job is None:
            return False
        rows = self.db.select(
            "tasks", "WHERE job_id=? AND status IN ('failed','cancelled') AND role IS NULL", (job_id,)
        )
        if not rows and job["status"] not in ("failed", "cancelled"):
            return False
        for task in rows:
            self.db.update(
                "tasks",
                {"id": task["id"]},
                {
                    "status": "queued",
                    "device_id": None,
                    "attempts": 0,
                    "assigned_at": None,
                    "started_at": None,
                    "finished_at": None,
                    "lease_expires": None,
                    "error": None,
                },
            )
        status = "running" if (rows or job["total_tasks"]) else "pending"
        self.db.update(
            "jobs",
            {"id": job_id},
            {"status": status, "finished": None, "error": None, "failed_tasks": 0},
        )
        self.state.publish_job(job_id)
        self.wake()
        return True

    # ------------------------------------------------------------- devices
    def remove_device(self, device_id: str) -> bool:
        device = self.db.get("devices", device_id)
        if device is None or device.get("removed"):
            return False
        self.state.send_command(device_id, {"cmd": "removed"})
        self.db.update(
            "devices", {"id": device_id}, {"removed": 1, "status": "offline", "paused": 0}
        )
        self.safety.forget(device_id)
        self.drop_backups_on(device_id)
        self.holds.pop(device_id, None)
        self.state.speeds.forget_device(device_id)
        self.state.peers.drop_device(device_id)
        self.state.terminals.drop_device(device_id)
        self.requeue_device_tasks(device_id, "device removed")
        self.state.event(
            "device_removed", "%s removed" % device.get("name"), device_id=device_id
        )
        self.state.publish_device(device_id, removed=True)
        self.wake()
        return True


def task_units(task: Dict[str, Any]) -> float:
    """How much work a task is, in its plugin's units (1 when it does not say)."""
    meta = (task.get("payload") or {}).get("meta") or {}
    try:
        units = float(meta.get("units") or 1)
    except (TypeError, ValueError):
        units = 1.0
    return units if units > 0 else 1.0


class SpeedBook:
    """How fast each device works on a job, for one scheduling pass (SPEC §9.5).

    :meth:`rate` (units per second): the job's own finished tasks on that
    device; else the speed memory for the job's kind of work (``speeds``
    table, learned across jobs); else scaled by benchmark score from a device
    with a known rate of the same runtime kind.  When nothing at all is known
    for the job, :meth:`speed` falls back to benchmark scores, which rank the
    devices but are not in units per second.
    """

    def __init__(self, scheduler: Scheduler) -> None:
        self.db = scheduler.db
        self.memory = scheduler.state.speeds
        self._job: Dict[str, Dict[str, float]] = {}
        self._mem: Dict[str, Dict[str, float]] = {}
        self._devices: Optional[Dict[str, Dict[str, Any]]] = None

    def job_rates(self, job_id: str) -> Dict[str, float]:
        cached = self._job.get(job_id)
        if cached is None:
            rows = self.db.rows(
                "SELECT device_id, SUM(COALESCE(json_extract(payload, '$.meta.units'), 1)) AS u, "
                "SUM(COALESCE(NULLIF(work_seconds, 0), duration)) AS d FROM tasks "
                "WHERE job_id=? AND status='done' AND duration > 0 "
                "AND device_id IS NOT NULL AND json_type(payload, '$.service') IS NULL GROUP BY device_id",
                (job_id,),
            )
            cached = {row["device_id"]: float(row["u"]) / float(row["d"]) for row in rows if row["d"]}
            self._job[job_id] = cached
        return cached

    def memory_rates(self, key: str) -> Dict[str, float]:
        if not key:
            return {}
        cached = self._mem.get(key)
        if cached is None:
            cached = self.memory.rates(key)
            self._mem[key] = cached
        return cached

    def known(self, job_id: str, key: str) -> bool:
        return bool(self.job_rates(job_id) or self.memory_rates(key))

    def _device(self, device_id: str) -> Dict[str, Any]:
        if self._devices is None:
            self._devices = {d["id"]: d for d in self.db.select("devices")}
        return self._devices.get(device_id) or {}

    @staticmethod
    def _score(device: Dict[str, Any]) -> Optional[float]:
        score = device.get("bench_score")
        try:
            return float(score) if score and float(score) > 0 else None
        except (TypeError, ValueError):
            return None

    def rate(self, job_id: str, key: str, device: Optional[Dict[str, Any]]) -> Optional[float]:
        """Units per second for this job on ``device``, or None."""
        if not device:
            return None
        measured = self.job_rates(job_id)
        if device["id"] in measured:
            return measured[device["id"]]
        remembered = self.memory_rates(key)
        if device["id"] in remembered:
            return remembered[device["id"]]
        score = self._score(device)
        if score is None:
            return None
        kind = bench_kind(device)
        for refs in (measured, remembered):
            for ref_id, ref_rate in refs.items():
                ref = self._device(ref_id)
                ref_score = self._score(ref)
                if ref_score and bench_kind(ref) == kind:
                    return ref_rate * score / ref_score
        return None

    def speed(self, job_id: str, key: str) -> Any:
        """A ``device -> speed`` function consistent across devices for one job:
        rates when the job's speed is known anywhere, benchmark scores before."""
        if self.known(job_id, key):
            return lambda device: self.rate(job_id, key, device)
        return lambda device: self._score(device)
