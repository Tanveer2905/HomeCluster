"""Worker endpoints, ``/api/w/*`` (SPEC §8)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .auth import client_host, hash_token, locked, new_token, pin_matches, require_device
from .db import new_id, now
from .models import (
    BenchRequest,
    ChunkAckRequest,
    PeerFetchRequest,
    PeerReadyRequest,
    PeerResultRequest,
    PollRequest,
    RegisterRequest,
    ScrubReport,
    ServiceDoneRequest,
    ServicePartialRequest,
    ServiceReadyRequest,
    TaskCheckpointRequest,
    TaskDoneRequest,
    TaskLogRequest,
    TermClosedRequest,
    TermOutputRequest,
)

log = logging.getLogger("coordinator.api.worker")

router = APIRouter(prefix="/api/w", tags=["worker"])

MAX_UPLOAD = 2 * 1024 * 1024 * 1024
LOG_LINE_CAP = 10000


def _state(request: Request) -> Any:
    return request.app.state.cluster


# --------------------------------------------------------------------------
# register / poll / leave
# --------------------------------------------------------------------------

@router.post("/register")
async def register(request: Request, body: RegisterRequest) -> Dict[str, Any]:
    """Join the cluster with a one-time join code (or, on the PC itself, the
    PIN), or re-attach with a saved ``device_id`` + ``token``.  The answer
    carries the cluster's CA and a certificate for this device (SPEC 3, 8.3)."""
    state = _state(request)
    stamp = now()
    host = client_host(request)
    local = state.security.is_local(host)
    caps = [str(c) for c in (body.caps or [])]
    info = dict(body.info or {})
    slots = max(1, int(body.slots or 1))
    device: Optional[Dict[str, Any]] = None
    token: Optional[str] = None

    if body.device_id and body.token:
        existing = state.db.get("devices", str(body.device_id))
        if existing is None or existing.get("token_hash") != hash_token(str(body.token)):
            state.security.guard.fail(host, local)
            raise HTTPException(status_code=401, detail="unauthorized")
        if existing.get("removed"):
            raise HTTPException(status_code=410, detail="removed")
        device = existing
        token = str(body.token)
    else:
        wait = state.security.guard.blocked(host, local)
        if wait:
            raise locked(wait)
        joining_id = new_id()
        if body.join_code and state.security.codes.consume(body.join_code, joining_id):
            pass  # a code shown on the PC, now used up
        elif local and body.pin and pin_matches(body.pin, state.config.pin):
            pass  # the PC's own worker
        else:
            state.security.guard.fail(host, local)
            detail = "join code unknown, used or expired: make a new one on the PC's Join page"
            raise HTTPException(status_code=401, detail=detail if body.join_code else "unauthorized")
        state.security.guard.ok(host)

    if device is None:
        device_id = joining_id
        token = new_token()
        state.db.insert(
            "devices",
            {
                "id": device_id,
                "name": body.name or "device",
                "platform": body.platform or "termux",
                "token_hash": hash_token(token),
                "caps": caps,
                "info": info,
                "status": "online",
                "paused": 0,
                "slots": slots,
                "settings": {},
                "last_seen": stamp,
                "created": stamp,
                "removed": 0,
            },
        )
        state.event(
            "device_joined", "%s joined" % (body.name or device_id), device_id=device_id
        )
        state.notify(
            "device_joined",
            "%s joined the cluster" % (body.name or device_id),
            {"device_id": device_id, "platform": body.platform},
        )
        device = state.db.get("devices", device_id) or {}
        log.info("device %s (%s) joined with caps %s", body.name, device_id, ",".join(caps))
    else:
        device_id = device["id"]
        was_offline = device.get("status") != "online"
        # a name a person chose (on the dashboard, or on the device itself) wins over
        # the one a worker reports when it joins again after a restart or update
        settings = dict(device.get("settings") or {})
        if body.rename and body.name:
            name, settings["name_set"] = body.name, True
        elif settings.get("name_set"):
            name = device.get("name")
        else:
            name = body.name or device.get("name")
        state.db.update(
            "devices",
            {"id": device_id},
            {
                "name": name,
                "settings": settings,
                "platform": body.platform or device.get("platform"),
                "caps": caps or device.get("caps") or [],
                "info": info or device.get("info") or {},
                "slots": slots,
                "status": "online",
                "last_seen": stamp,
            },
        )
        if was_offline:
            state.event(
                "device_online", "%s is back online" % device.get("name"), device_id=device_id
            )
        device = state.db.get("devices", device_id) or {}

    state.updates.registered(device_id, info)
    state.publish_device(device_id)
    state.scheduler.wake()
    response: Dict[str, Any] = {
        "device_id": device_id,
        "token": token,
        "name": device.get("name"),   # the name the cluster knows it by
        "config": {
            "poll_timeout": state.config.poll_timeout,
            "offline_after": state.config.offline_after,
            "log_batch_ms": 500,
            "cache_mb": int(state.setting("storage.cache_mb", 512) or 512),
        },
    }
    if device.get("bench_score") is not None:
        response["bench_done"] = True
    # the CA to check the coordinator and other devices with, and this
    # device's own certificate for sending files to them (SPEC 3, 8.3)
    response["tls"] = await asyncio.to_thread(state.security.tls_bundle, device_id)
    return response


@router.post("/poll")
async def poll(
    request: Request, body: PollRequest, device: Dict[str, Any] = Depends(require_device)
) -> Dict[str, Any]:
    """Long-poll for commands (SPEC §8/§9)."""
    state = _state(request)
    state.screens.hosts[device["id"]] = client_host(request)  # for adb over Wi-Fi
    status = body.status
    fields: Dict[str, Any] = {"status": "online", "last_seen": now()}
    if status.battery is not None:
        fields["battery"] = int(status.battery)
    if status.charging is not None:
        fields["charging"] = 1 if status.charging else 0
    if status.temp is not None:
        fields["temp"] = float(status.temp)
    if status.storage_free_mb is not None:
        fields["storage_free_mb"] = int(status.storage_free_mb)
    if status.mem_free_mb is not None:
        fields["mem_free_mb"] = int(status.mem_free_mb)
    if isinstance(status.gpu, dict):
        fields["gpu"] = {k: status.gpu.get(k) for k in
                         ("name", "vram_total_mb", "vram_free_mb", "temp", "util", "ts")}
    was_offline = device.get("status") != "online"
    state.db.update("devices", {"id": device["id"]}, fields)
    if was_offline:
        state.event(
            "device_online", "%s is back online" % device.get("name"), device_id=device["id"]
        )
    # A device that keeps reporting a task keeps its lease (services never
    # finish, so their lease would otherwise expire).
    running = [str(t) for t in (status.running or []) if t]
    if running:
        state.scheduler.refresh_leases(device["id"], running)
    state.publish_device(device["id"])
    state.scheduler.wake()

    # an idle worker with an older worker.py gets the update on its own: anything
    # queued meanwhile waits here for the restarted worker (SPEC 8.4)
    update = state.updates.command_for(device, running)
    if update is not None:
        return {"commands": [update]}
    queue = state.queue_for(device["id"])
    commands: List[Dict[str, Any]] = []
    timeout = float(state.config.poll_timeout)
    try:
        commands.append(await asyncio.wait_for(queue.get(), timeout=timeout))
    except asyncio.TimeoutError:
        pass
    while True:
        try:
            commands.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    answer: Dict[str, Any] = {"commands": commands}
    if device.get("platform") == "browser":
        answer["page"] = state.updates.page_version()  # a changed page reloads itself when idle
    return answer


@router.post("/leave")
async def leave(
    request: Request, device: Dict[str, Any] = Depends(require_device)
) -> Dict[str, Any]:
    state = _state(request)
    state.db.update("devices", {"id": device["id"]}, {"status": "offline"})
    state.terminals.drop_device(device["id"])
    state.scheduler.requeue_device_tasks(device["id"], "device left")
    state.event("device_offline", "%s left" % device.get("name"), device_id=device["id"])
    state.publish_device(device["id"])
    state.scheduler.wake()
    return {"ok": True}


# --------------------------------------------------------------------------
# tasks
# --------------------------------------------------------------------------

@router.post("/task/{task_id}/start")
async def task_start(
    request: Request, task_id: str, device: Dict[str, Any] = Depends(require_device)
) -> Dict[str, Any]:
    state = _state(request)
    ok = state.scheduler.task_started(task_id, device)
    return {"ok": bool(ok)}


@router.post("/task/{task_id}/log")
async def task_log(
    request: Request,
    task_id: str,
    body: TaskLogRequest,
    device: Dict[str, Any] = Depends(require_device),
) -> Dict[str, Any]:
    state = _state(request)
    lines = [str(line) for line in (body.lines or [])][:LOG_LINE_CAP]
    if lines:
        state.append_task_log(task_id, lines)
        await state.hub.send_log(task_id, lines)
    return {"ok": True, "lines": len(lines)}


@router.post("/task/{task_id}/checkpoint")
async def task_checkpoint(
    request: Request,
    task_id: str,
    body: TaskCheckpointRequest,
    device: Dict[str, Any] = Depends(require_device),
) -> Response:
    """Save resumable progress for a running task (SPEC §6.5)."""
    state = _state(request)
    status, payload = state.scheduler.task_checkpoint(task_id, device, body.state, body.attempt)
    return JSONResponse(payload, status_code=status)


@router.post("/task/{task_id}/done")
async def task_done(
    request: Request,
    task_id: str,
    body: TaskDoneRequest,
    device: Dict[str, Any] = Depends(require_device),
) -> Response:
    state = _state(request)
    status, payload = state.scheduler.task_done(task_id, device, body.model_dump())
    return JSONResponse(payload, status_code=status)


# --------------------------------------------------------------------------
# services (SPEC §6.7)
# --------------------------------------------------------------------------

@router.post("/service/{task_id}/ready")
async def service_ready(
    request: Request,
    task_id: str,
    body: ServiceReadyRequest,
    device: Dict[str, Any] = Depends(require_device),
) -> Dict[str, Any]:
    state = _state(request)
    ok = await state.services.instance_ready(task_id, device["id"], body.name, body.meta or {})
    if not ok:
        raise HTTPException(status_code=409, detail="task is not a running service on this device")
    return {"ok": True}


def _request_for(state: Any, request_id: str, device: Dict[str, Any]) -> Any:
    req = state.services.requests.get(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="unknown request")
    if req.instance is None or req.instance.device_id != device["id"]:
        raise HTTPException(status_code=409, detail="request is not assigned to this device")
    return req


@router.get("/requests/{request_id}/input")
async def request_input(
    request: Request, request_id: str, device: Dict[str, Any] = Depends(require_device)
) -> Response:
    """The request body, streamed to the serving device as the caller uploads it."""
    state = _state(request)
    req = _request_for(state, request_id, device)
    headers = {"X-Request-Name": str(req.name or ""), "Cache-Control": "no-store"}
    if req.size is not None:
        headers["X-Request-Size"] = str(req.size)
    return StreamingResponse(
        state.services.input_stream(req), media_type=req.content_type or "application/octet-stream",
        headers=headers,
    )


@router.post("/requests/{request_id}/partial")
async def request_partial(
    request: Request,
    request_id: str,
    body: ServicePartialRequest,
    device: Dict[str, Any] = Depends(require_device),
) -> Dict[str, Any]:
    state = _state(request)
    req = _request_for(state, request_id, device)
    state.services.add_partial(req, body.data)
    return {"ok": True}


@router.post("/requests/{request_id}/done")
async def request_done(
    request: Request,
    request_id: str,
    body: ServiceDoneRequest,
    device: Dict[str, Any] = Depends(require_device),
) -> Dict[str, Any]:
    state = _state(request)
    req = state.services.requests.get(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="unknown request")
    if req.instance is None or req.instance.device_id != device["id"]:
        # a device answering a request it never got (e.g. after a restart): ignore
        return {"ok": True, "ignored": True}
    await state.services.complete(req, bool(body.ok), body.result, body.error,
                                  list(body.outputs or []), body.ms)
    return {"ok": True}


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------

@router.get("/files/{file_id}")
async def worker_file(
    request: Request, file_id: str, device: Dict[str, Any] = Depends(require_device)
) -> Response:
    state = _state(request)
    response = state.files.response(file_id, request.headers.get("range"))
    if response is None:
        raise HTTPException(status_code=404, detail="no such file")
    return response


@router.put("/files")
async def worker_upload(
    request: Request, name: str = "upload.bin", device: Dict[str, Any] = Depends(require_device)
) -> Dict[str, Any]:
    state = _state(request)
    meta = await _store_stream(state, request, name)
    return {"file_id": meta["id"], "size": meta["size"], "name": meta["name"]}


async def _store_stream(state: Any, request: Request, name: str) -> Dict[str, Any]:
    chunks: List[bytes] = []
    total = 0
    async for block in request.stream():
        if not block:
            continue
        total += len(block)
        if total > MAX_UPLOAD:
            raise HTTPException(status_code=413, detail="upload too large")
        chunks.append(block)
    return await asyncio.to_thread(state.files.add_stream, chunks, name)


# --------------------------------------------------------------------------
# pooled storage chunks
# --------------------------------------------------------------------------

@router.get("/chunk/{chunk_id}")
async def get_chunk(
    request: Request, chunk_id: str, device: Dict[str, Any] = Depends(require_device)
) -> Response:
    state = _state(request)
    path = state.storage.cached(chunk_id)
    if path is None:
        raise HTTPException(status_code=404, detail="chunk not staged")
    data = await asyncio.to_thread(path.read_bytes)
    return Response(content=data, media_type="application/octet-stream")


@router.put("/chunk/{chunk_id}")
async def put_chunk(
    request: Request,
    chunk_id: str,
    req_id: str = "",
    device: Dict[str, Any] = Depends(require_device),
) -> Dict[str, Any]:
    state = _state(request)
    data = await request.body()
    accepted = state.storage.on_chunk_upload(chunk_id, req_id, data)
    return {"ok": bool(accepted)}


@router.post("/chunk_ack")
async def chunk_ack(
    request: Request,
    body: ChunkAckRequest,
    device: Dict[str, Any] = Depends(require_device),
) -> Dict[str, Any]:
    state = _state(request)
    state.storage.on_ack(device["id"], body.chunk_id, bool(body.ok), body.error)
    return {"ok": True}


@router.post("/storage/scrub")
async def scrub_report(
    request: Request,
    body: ScrubReport,
    device: Dict[str, Any] = Depends(require_device),
) -> Dict[str, Any]:
    """A device re-hashed stored pieces; damaged or lost ones get rebuilt (SPEC §10)."""
    state = _state(request)
    return state.storage.on_scrub(device["id"], list(body.checked), list(body.bad), list(body.missing))


# --------------------------------------------------------------------------
# phone-to-phone transfers (SPEC §8.3)
# --------------------------------------------------------------------------

@router.post("/peer/fetch")
async def peer_fetch(
    request: Request,
    body: PeerFetchRequest,
    device: Dict[str, Any] = Depends(require_device),
) -> Dict[str, Any]:
    """Where to download a whole input file from: peers that hold it (each
    with a one-time token) plus the coordinator, and every piece's SHA-256."""
    return await _state(request).peers.fetch(device, body.file_id)


@router.post("/peer/ready")
async def peer_ready(
    request: Request,
    body: PeerReadyRequest,
    device: Dict[str, Any] = Depends(require_device),
) -> Dict[str, Any]:
    ok = _state(request).peers.on_ready(device["id"], body.model_dump())
    return {"ok": ok}


@router.post("/peer/result")
async def peer_result(
    request: Request,
    body: PeerResultRequest,
    device: Dict[str, Any] = Depends(require_device),
) -> Dict[str, Any]:
    ok = _state(request).peers.on_result(device["id"], body.model_dump())
    return {"ok": ok}


# --------------------------------------------------------------------------
# benchmark + terminal
# --------------------------------------------------------------------------

@router.post("/bench")
async def bench(
    request: Request, body: BenchRequest, device: Dict[str, Any] = Depends(require_device)
) -> Dict[str, Any]:
    state = _state(request)
    payload = body.model_dump()
    state.set_bench_score(device["id"], float(body.score or 0.0), payload)
    log.info("device %s benchmark score %.3f", device.get("name"), body.score or 0.0)
    return {"ok": True}


@router.post("/term/{session_id}/output")
async def term_output(
    request: Request,
    session_id: str,
    body: TermOutputRequest,
    device: Dict[str, Any] = Depends(require_device),
) -> Dict[str, Any]:
    state = _state(request)
    await state.terminals.on_output(session_id, body.data or "")
    return {"ok": True}


@router.post("/term/{session_id}/closed")
async def term_closed(
    request: Request,
    session_id: str,
    body: TermClosedRequest,
    device: Dict[str, Any] = Depends(require_device),
) -> Dict[str, Any]:
    state = _state(request)
    await state.terminals.on_closed(session_id, body.exit_code)
    return {"ok": True}
