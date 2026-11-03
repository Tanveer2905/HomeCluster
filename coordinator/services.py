"""Services: long-lived model hosts on devices, and the request relay (SPEC §6.7).

A ``service`` job runs a task that loads a model once and then answers
requests.  This module keeps the registry of ready instances and relays
each request between the caller and the serving device:

    caller  --POST /api/services/{name}/call (streamed body)-->  coordinator
    coordinator --"service_request" command-->  device (poll channel)
    device  --GET /api/w/requests/{id}/input (streamed)-->  coordinator
    device  --POST /api/w/requests/{id}/partial|done-->  coordinator
    coordinator --HTTP response / WebSocket "request" events-->  caller

Request bytes pass through a bounded in-memory queue (backpressure: a slow
model slows the upload); nothing is written to disk on the coordinator or
the serving device unless the service code chooses to.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional

from .db import new_id, now

log = logging.getLogger("coordinator.services")

QUEUE_CHUNKS = 32            # in-flight upload chunks per request (backpressure)
DEFAULT_QUEUE_TIMEOUT = 60.0 # seconds to wait for a free instance
DEFAULT_REQUEST_TIMEOUT = 300.0
HISTORY = 200                # finished requests kept in memory
INACTIVE = ("done", "failed", "cancelled")


class Unavailable(Exception):
    """The service is not running and cannot be started (e.g. the model does not fit)."""


class Instance:
    """One running copy of a service (= one service task on one device)."""

    __slots__ = ("task_id", "job_id", "device_id", "name", "meta", "ready", "busy",
                 "requests_done", "total_ms", "last_used", "since", "current")

    def __init__(self, task_id: str, job_id: str, device_id: str, name: str, meta: Dict[str, Any]) -> None:
        self.task_id = task_id
        self.job_id = job_id
        self.device_id = device_id
        self.name = name
        self.meta = meta or {}
        self.ready = True
        self.busy = False
        self.requests_done = 0
        self.total_ms = 0
        self.last_used: Optional[float] = None
        self.since = now()
        self.current: Optional[str] = None

    def to_dict(self, state: Any) -> Dict[str, Any]:
        device = state.db.get("devices", self.device_id) or {}
        return {
            "task_id": self.task_id,
            "job_id": self.job_id,
            "device_id": self.device_id,
            "device_name": device.get("name"),
            "status": "busy" if self.busy else ("ready" if self.ready else "loading"),
            "current_request": self.current,
            "requests_done": self.requests_done,
            "avg_ms": int(self.total_ms / self.requests_done) if self.requests_done else None,
            "last_used": self.last_used,
            "since": self.since,
        }


class ServiceRequest:
    def __init__(self, request_id: str, service: str, params: Dict[str, Any],
                 content_type: str, size: Optional[int], name: Optional[str],
                 caller: Optional[str]) -> None:
        self.id = request_id
        self.service = service
        self.params = params
        self.content_type = content_type
        self.size = size
        self.name = name
        self.caller = caller
        self.status = "queued"
        self.instance: Optional[Instance] = None
        self.chunks: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue(maxsize=QUEUE_CHUNKS)
        self.partials: List[Any] = []
        self.result: Any = None
        self.error: Optional[str] = None
        self.outputs: List[Dict[str, Any]] = []
        self.finished = asyncio.Event()
        self.changed = asyncio.Event()
        self.created = now()
        self.started: Optional[float] = None
        self.ended: Optional[float] = None
        self.bytes_in = 0
        self.ms: Optional[int] = None
        self.uploaded = False

    def to_dict(self, include_partials: bool = True) -> Dict[str, Any]:
        out = {
            "id": self.id,
            "service": self.service,
            "status": self.status,
            "device_id": self.instance.device_id if self.instance else None,
            "content_type": self.content_type,
            "size": self.size,
            "name": self.name,
            "bytes_in": self.bytes_in,
            "created": self.created,
            "started": self.started,
            "ended": self.ended,
            "ms": self.ms,
            "result": self.result,
            "error": self.error,
            "outputs": self.outputs,
            "partial_count": len(self.partials),
        }
        if include_partials:
            out["partials"] = list(self.partials)
        return out

    def touch(self) -> None:
        self.changed.set()
        self.changed = asyncio.Event()


class ServiceRegistry:
    def __init__(self, state: Any) -> None:
        self.state = state
        self.instances: Dict[str, Instance] = {}          # task_id -> Instance
        self.requests: Dict[str, ServiceRequest] = {}
        self.history: List[str] = []
        self._free = asyncio.Condition()

    # ------------------------------------------------------------ registry
    def services(self, include_hidden: bool = False) -> List[Dict[str, Any]]:
        """Callable services; names starting with ``_`` (e.g. LLM shards) are internal."""
        by_name: Dict[str, Dict[str, Any]] = {}
        for inst in self.instances.values():
            if inst.name.startswith("_") and not include_hidden:
                continue
            job = self.state.db.get("jobs", inst.job_id) or {}
            params = job.get("params") or {}
            entry = by_name.setdefault(inst.name, {
                "name": inst.name,
                "description": params.get("description") or "",
                "accepts": params.get("accepts") or "",
                "produces": params.get("produces") or "",
                "streaming_output": bool(params.get("streaming_output")),
                "request_timeout": params.get("request_timeout"),
                "instances": [],
            })
            entry["instances"].append(inst.to_dict(self.state))
        manager = getattr(self.state, "llm", None)
        if manager is not None:
            # models that load on the first call (SPEC 6.4d) are callable too
            for item in manager.loadable():
                if item["name"] not in by_name:
                    by_name[item["name"]] = {
                        "name": item["name"], "description": item["description"],
                        "accepts": "text or JSON {messages, max_tokens, temperature}",
                        "produces": "text (streamed)", "streaming_output": True,
                        "request_timeout": None, "instances": [], "on_demand": True,
                    }
        out = sorted(by_name.values(), key=lambda s: s["name"])
        for entry in out:
            entry["ready"] = sum(1 for i in entry["instances"] if i["status"] == "ready")
            entry["busy"] = sum(1 for i in entry["instances"] if i["status"] == "busy")
        return out

    def names(self) -> List[str]:
        return sorted({i.name for i in self.instances.values()})

    async def instance_ready(self, task_id: str, device_id: str, name: str, meta: Dict[str, Any]) -> bool:
        task = self.state.db.get("tasks", task_id)
        if task is None or task.get("device_id") != device_id or task["status"] not in ("assigned", "running"):
            return False
        inst = self.instances.get(task_id)
        if inst is None:
            inst = Instance(task_id, task["job_id"], device_id, name, meta)
            self.instances[task_id] = inst
            device = self.state.db.get("devices", device_id) or {}
            self.state.event("service_ready", "service %s ready on %s" % (name, device.get("name")),
                             device_id=device_id, job_id=task["job_id"], task_id=task_id)
            log.info("service %s ready on %s (task %s)", name, device.get("name"), task_id)
        else:
            # child restarted after a crash or a cancelled request (or re-announced
            # after the coordinator restarted): take the fresh facts
            inst.ready = True
            inst.busy = False
            inst.current = None
            inst.meta = meta or inst.meta
        self._publish_services()
        self._notify_llm()
        async with self._free:
            self._free.notify_all()
        return True

    def _notify_llm(self) -> None:
        manager = getattr(self.state, "llm", None)
        if manager is not None:
            manager.wake()

    def drop_instance(self, task_id: str, reason: str = "service stopped") -> None:
        inst = self.instances.pop(task_id, None)
        if inst is None:
            return
        log.info("service %s on %s gone: %s", inst.name, inst.device_id, reason)
        for req in list(self.requests.values()):
            if req.instance is inst and req.status in ("sending", "running"):
                self._finish(req, ok=False, error="service instance went away (%s)" % reason)
        self._publish_services()
        self._notify_llm()

    def on_task_update(self, task: Dict[str, Any]) -> None:
        """Called for every task state change: forget instances of ended tasks."""
        if task.get("id") in self.instances and task.get("status") not in ("assigned", "running"):
            self.drop_instance(str(task["id"]), "task %s" % task.get("status"))

    def on_device_offline(self, device_id: str) -> None:
        for task_id, inst in list(self.instances.items()):
            if inst.device_id == device_id:
                self.drop_instance(task_id, "device offline")

    def _publish_services(self) -> None:
        self.state.hub.publish({"ev": "services", "services": self.services()})

    # ------------------------------------------------------------ requests
    def _pick(self, name: str) -> Optional[Instance]:
        candidates = [i for i in self.instances.values() if i.name == name and i.ready and not i.busy]
        if not candidates:
            return None
        # least recently used first, so several instances share the load
        candidates.sort(key=lambda i: (i.last_used or 0))
        return candidates[0]

    def _load_on_demand(self, name: str) -> Optional[str]:
        """A model called ``name`` that is not running starts now (SPEC 6.4d);
        returns what is happening, or None when ``name`` is no model."""
        manager = getattr(self.state, "llm", None)
        if manager is None:
            return None
        try:
            return manager.ensure(name)
        except (KeyError, ValueError) as exc:
            raise Unavailable("cannot load %s: %s" % (name, exc))

    async def acquire(self, name: str, timeout: float, req: Optional["ServiceRequest"] = None) -> Instance:
        deadline = time.time() + timeout
        loading = False
        async with self._free:
            while True:
                inst = self._pick(name)
                if inst is not None:
                    inst.busy = True
                    inst.last_used = now()
                    return inst
                if not any(i.name == name for i in self.instances.values()):
                    note = self._load_on_demand(name)
                    if note is None and not loading:
                        raise LookupError("no running service named %r" % name)
                    if note is not None and not loading:
                        loading = True
                        wait = float(self.state.setting("llm.load_wait", 900) or 900)
                        deadline = max(deadline, time.time() + wait)
                        if req is not None:
                            self.add_partial(req, {"status": note})
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError("%r is still loading" % name if loading
                                       else "all instances of %r are busy" % name)
                try:
                    await asyncio.wait_for(self._free.wait(), timeout=min(remaining, 5.0))
                except asyncio.TimeoutError:
                    continue

    async def release(self, inst: Instance) -> None:
        inst.busy = False
        inst.current = None
        async with self._free:
            self._free.notify_all()
        self._publish_services()

    def create_request(self, name: str, params: Dict[str, Any], content_type: str,
                       size: Optional[int], filename: Optional[str], caller: Optional[str],
                       request_id: Optional[str] = None) -> ServiceRequest:
        rid = str(request_id or new_id())
        if rid in self.requests:
            raise ValueError("request id %r already used" % rid)
        req = ServiceRequest(rid, name, params, content_type, size, filename, caller)
        self.requests[rid] = req
        self._remember(rid)
        return req

    def _remember(self, rid: str) -> None:
        self.history.append(rid)
        while len(self.history) > HISTORY:
            old = self.history.pop(0)
            stale = self.requests.get(old)
            if stale is not None and stale.status in INACTIVE:
                self.requests.pop(old, None)

    def _publish_request(self, req: ServiceRequest, partial: Any = None) -> None:
        message: Dict[str, Any] = {"ev": "request", "request": req.to_dict(include_partials=False)}
        if partial is not None:
            message["partial"] = partial
        self.state.hub.publish(message)

    def devices_of(self, inst: Instance) -> List[str]:
        """Every device doing work for this instance: its own, plus the shards
        of a split model."""
        devices = [inst.device_id]
        deployment = (inst.meta or {}).get("deployment")
        manager = getattr(self.state, "llm", None)
        if deployment and manager is not None:
            devices += manager.devices_for(str(deployment))
        return list(dict.fromkeys(devices))

    async def dispatch(self, req: ServiceRequest, queue_timeout: float) -> Instance:
        """Find an instance and hand it the request (the body follows)."""
        inst = await self.acquire(req.service, queue_timeout, req)
        req.instance = inst
        req.status = "sending"
        req.started = now()
        inst.current = req.id
        # requests come first: normal tasks on these devices pause meanwhile
        self.state.scheduler.hold(self.devices_of(inst), req.id)
        self.state.send_command(inst.device_id, {
            "cmd": "service_request",
            "task_id": inst.task_id,
            "request": {
                "id": req.id, "name": req.name, "params": req.params,
                "content_type": req.content_type, "size": req.size,
            },
        })
        self._publish_services()
        self._publish_request(req)
        return inst

    async def feed(self, req: ServiceRequest, body: AsyncIterator[bytes], timeout: float) -> None:
        """Pump the caller's upload into the request queue (with backpressure)."""
        try:
            async for chunk in body:
                if not chunk:
                    continue
                if req.status in INACTIVE:
                    break
                req.bytes_in += len(chunk)
                await asyncio.wait_for(req.chunks.put(chunk), timeout=timeout)
        finally:
            req.uploaded = True
            try:
                req.chunks.put_nowait(None)
            except asyncio.QueueFull:
                # drain one slot so the EOF marker fits
                try:
                    req.chunks.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                req.chunks.put_nowait(None)

    async def input_stream(self, req: ServiceRequest) -> AsyncIterator[bytes]:
        """What the serving device pulls: the upload, chunk by chunk."""
        if req.status == "sending":
            req.status = "running"
            self._publish_request(req)
        while True:
            chunk = await req.chunks.get()
            if chunk is None:
                return
            yield chunk

    def add_partial(self, req: ServiceRequest, data: Any) -> None:
        if req.status in INACTIVE:
            return
        req.partials.append(data)
        req.touch()
        self._publish_request(req, partial=data)

    def _finish(self, req: ServiceRequest, ok: bool, result: Any = None, error: Optional[str] = None,
                outputs: Optional[List[Dict[str, Any]]] = None, ms: Optional[int] = None) -> None:
        if req.status in INACTIVE:
            return
        self.state.scheduler.release(req.id)
        req.status = "done" if ok else "failed"
        req.result = result
        req.error = error
        req.outputs = list(outputs or [])
        req.ended = now()
        req.ms = ms if ms is not None else int((req.ended - (req.started or req.created)) * 1000)
        for output in req.outputs:
            if output.get("file_id"):
                self.state.files.add_ref(str(output["file_id"]))
        inst = req.instance
        if inst is not None and inst.current == req.id:
            inst.requests_done += 1 if ok else 0
            inst.total_ms += req.ms or 0
        req.finished.set()
        req.touch()
        self._publish_request(req)

    async def complete(self, req: ServiceRequest, ok: bool, result: Any, error: Optional[str],
                       outputs: List[Dict[str, Any]], ms: Optional[int]) -> None:
        inst = req.instance
        self._finish(req, ok, result, error, outputs, ms)
        if inst is not None:
            await self.release(inst)

    async def cancel(self, req: ServiceRequest, reason: str = "cancelled") -> bool:
        if req.status in INACTIVE:
            return False
        inst = req.instance
        if inst is not None:
            self.state.send_command(inst.device_id, {"cmd": "service_cancel", "request_id": req.id})
        self.state.scheduler.release(req.id)
        req.status = "cancelled"
        req.error = reason
        req.ended = now()
        req.finished.set()
        req.touch()
        try:
            req.chunks.put_nowait(None)
        except asyncio.QueueFull:
            pass
        self._publish_request(req)
        if inst is not None:
            await self.release(inst)
        return True

    async def wait_finished(self, req: ServiceRequest, timeout: float) -> bool:
        try:
            await asyncio.wait_for(req.finished.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_change(self, req: ServiceRequest, timeout: float) -> None:
        try:
            await asyncio.wait_for(req.changed.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    def recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = [self.requests[r] for r in reversed(self.history) if r in self.requests]
        return [r.to_dict(include_partials=False) for r in rows[:limit]]
