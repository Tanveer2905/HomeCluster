"""More copies of busy services (SPEC 6.4c/6.4d).

Every couple of seconds: a service whose callers wait (a request queued for
``scale_up_after`` seconds, or two queued) gets another copy on a device
that may take it, up to ``max_copies``; a copy the autoscaler added that sat
unused for ``scale_down_minutes`` while nobody waited is stopped again.

* a language model that runs alone on one device gets a *replica*
  deployment (same name, ``options.replica_of``) on another device that can
  hold it alone; split models are not copied;
* a ``service`` job gets one more task, pinned to the fastest free device
  that does not host it yet (``meta.autoscaled``).

The service relay already spreads requests over every ready instance.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from .db import new_id

log = logging.getLogger("coordinator.autoscale")

#: after a failed attempt (nowhere to put a copy), wait this long
COOLDOWN = 60.0


class Autoscaler:
    def __init__(self, state: Any) -> None:
        self.state = state
        #: service name -> {"since", "kind", "ref"}: a copy that is starting
        self.pending: Dict[str, Dict[str, Any]] = {}
        self.cooldown: Dict[str, float] = {}

    def _setting(self, key: str, default: Any) -> Any:
        return self.state.setting("services.%s" % key, default)

    def tick(self) -> None:
        if not self._setting("autoscale", True):
            return
        registry = self.state.services
        stamp = time.time()
        waiting: Dict[str, List[float]] = {}
        for req in list(registry.requests.values()):
            if req.status == "queued":
                waiting.setdefault(req.service, []).append(stamp - req.created)
        names = {inst.name for inst in registry.instances.values() if not inst.name.startswith("_")}
        for name in names:
            queued = waiting.get(name) or []
            if queued and (max(queued) >= float(self._setting("scale_up_after", 2)) or len(queued) >= 2):
                self.scale_up(name)
        self.scale_down(waiting)

    # ---------------------------------------------------------------- up
    def _copies(self, name: str) -> int:
        return sum(1 for inst in self.state.services.instances.values() if inst.name == name)

    def scale_up(self, name: str) -> bool:
        stamp = time.time()
        if self.cooldown.get(name, 0) > stamp:
            return False
        pending = self.pending.get(name)
        if pending is not None:
            if self._pending_alive(pending) and stamp - pending["since"] < float(
                    self.state.setting("llm.load_wait", 900) or 900):
                return False  # one copy at a time
            self.pending.pop(name, None)
        if self._copies(name) >= int(self._setting("max_copies", 3)):
            return False
        manager = self.state.llm
        dep = manager.get(name)
        if dep is not None and dep["status"] in ("starting", "loading", "ready", "degraded"):
            replica = manager.add_replica(dep)
            if replica is None:
                self.cooldown[name] = stamp + COOLDOWN
                return False
            self.pending[name] = {"since": stamp, "kind": "replica", "ref": replica["id"]}
            return True
        task_id = self._copy_service_task(name)
        if task_id is None:
            self.cooldown[name] = stamp + COOLDOWN
            return False
        self.pending[name] = {"since": stamp, "kind": "task", "ref": task_id}
        return True

    def _pending_alive(self, pending: Dict[str, Any]) -> bool:
        """Still starting (not ready yet, not gone)?"""
        db = self.state.db
        if pending["kind"] == "replica":
            row = db.get("llm_deployments", pending["ref"])
            return row is not None and row["status"] in ("starting", "loading")
        task = db.get("tasks", pending["ref"])
        if task is None or task["status"] not in ("queued", "assigned", "running"):
            return False
        return pending["ref"] not in self.state.services.instances

    def _copy_service_task(self, name: str) -> Optional[str]:
        """One more task of the ``service`` job behind ``name``."""
        registry = self.state.services
        scheduler = self.state.scheduler
        insts = [i for i in registry.instances.values() if i.name == name]
        if not insts:
            return None
        job = self.state.db.get("jobs", insts[0].job_id)
        if job is None or job["type"] != "service" or job["status"] != "running":
            return None
        source = self.state.db.get("tasks", insts[0].task_id)
        if source is None:
            return None
        hosting = {i.device_id for i in insts}
        for task in self.state.db.select("tasks", "WHERE job_id=? AND status IN ('queued','assigned','running')",
                                         (job["id"],)):
            if task.get("device_id"):
                hosting.add(task["device_id"])
            pinned = (task.get("payload") or {}).get("device_id")
            if pinned:
                hosting.add(pinned)
        payload = dict(source.get("payload") or {})
        payload.pop("device_id", None)
        probe = {"payload": payload, "_job_options": job.get("options") or {}, "job_id": job["id"]}
        target = None
        for device in scheduler.available_devices():  # fastest first
            if device["id"] in hosting or not device["_ai"] or not scheduler.eligible(probe, device):
                continue
            target = device
            break
        if target is None:
            return None
        payload["device_id"] = target["id"]
        payload["meta"] = dict(payload.get("meta") or {}, autoscaled=True)
        idx = int(self.state.db.scalar("SELECT COALESCE(MAX(idx), -1) FROM tasks WHERE job_id=?",
                                       (job["id"],), default=-1)) + 1
        task_id = new_id()
        self.state.db.insert("tasks", {
            "id": task_id, "job_id": job["id"], "idx": idx, "payload": payload, "status": "queued",
            "device_id": None, "attempts": 0, "max_attempts": int(source.get("max_attempts") or 5),
            "history": [],
        })
        self.state.db.execute("UPDATE jobs SET total_tasks = total_tasks + 1 WHERE id=?", (job["id"],))
        self.state.event("service_copy", "%s is busy: another copy on %s" % (name, target.get("name")),
                         job_id=job["id"], task_id=task_id, device_id=target["id"])
        scheduler.wake()
        return task_id

    # -------------------------------------------------------------- down
    def scale_down(self, waiting: Dict[str, List[float]]) -> int:
        quiet = float(self._setting("scale_down_minutes", 3)) * 60
        stamp = time.time()
        registry = self.state.services
        stopped = 0
        by_task = {inst.task_id: inst for inst in registry.instances.values()}

        def idle(inst: Any, since: float) -> bool:
            return (not inst.busy and not waiting.get(inst.name)
                    and stamp - max(float(inst.last_used or 0), float(inst.since or 0), since) >= quiet)

        manager = self.state.llm
        for replica in manager.deployments(active_only=True, replicas=True):
            if not (replica.get("options") or {}).get("replica_of") or replica["status"] != "ready":
                continue
            head = manager._head_instance(replica)
            if head is not None and idle(head, float(replica.get("ready_at") or 0)):
                manager.drop_replica(replica, "not needed any more")
                self.state.event("llm_copy", "extra copy of %s stopped: quiet again" % replica["name"])
                stopped += 1
        for task in self.state.db.select(
                "tasks", "WHERE status IN ('assigned','running') AND json_extract(payload, '$.meta.autoscaled') = 1"):
            inst = by_task.get(task["id"])
            if inst is not None and idle(inst, float(task.get("started_at") or 0)):
                self.state.scheduler.cancel_task(task["id"], "extra copy not needed any more")
                self.state.event("service_copy", "extra copy of %s stopped: quiet again" % inst.name,
                                 task_id=task["id"], device_id=inst.device_id)
                stopped += 1
        return stopped
