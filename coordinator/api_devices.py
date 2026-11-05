"""Info, auth, devices, terminal, plugins, events and settings (SPEC §11)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from . import policy
from .auth import SESSION_COOKIE, client_host, locked, pin_matches, require_pin
from .db import now
from .models import (DevicePatch, LoginRequest, NotifyRequest, SettingsPatch, TerminalOpen,
                     TerminalResize, TrustPatch)
from .screen import ScreenError

log = logging.getLogger("coordinator.api.devices")

public = APIRouter(prefix="/api", tags=["public"])
router = APIRouter(prefix="/api", tags=["cluster"], dependencies=[Depends(require_pin)])


def _state(request: Request) -> Any:
    return request.app.state.cluster


def join_payload(state: Any, code: bool = False) -> Dict[str, Any]:
    """What a new device needs (SPEC §3): the LAN address, the fingerprint of
    the cluster's certificate and, with ``code``, a fresh one-time join code
    with the commands and QR built from it.  Never the PIN."""
    state.security.ensure_tls()
    made = state.security.codes.create("dashboard") if code else None
    return state.security.join_info(state.primary_url(), made)


# --------------------------------------------------------------------------
# public
# --------------------------------------------------------------------------

@public.get("/info")
async def info(request: Request) -> Dict[str, Any]:
    state = _state(request)
    from .static_routes import pyodide_version

    pyodide = pyodide_version(state.root)
    return {
        "name": "phonecluster",
        "version": state.version,
        "urls": state.urls(),
        "port": state.port or state.config.port,
        "has_pin": bool(state.config.pin),
        "time": now(),
        "discovery_port": int(state.setting("discovery_port", 8766) or 8766)
        if state.setting("discovery", True) else None,
        # browser workers can run Python (Pyodide) when this is set
        "pyodide": pyodide,
    }


@public.post("/login")
async def login(request: Request, body: LoginRequest) -> Response:
    """PIN -> a session cookie (a random token, never the PIN).  Only on the
    PC itself; wrong PINs lock the address out for a while."""
    state = _state(request)
    host = client_host(request)
    if not state.security.is_local(host):
        return JSONResponse({"error": "the dashboard only opens on the PC itself"}, status_code=403)
    wait = state.security.guard.blocked(host, local=True)
    if wait:
        raise locked(wait)
    if not pin_matches(body.pin, state.config.pin):
        state.security.guard.fail(host, local=True)
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    state.security.guard.ok(host)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        SESSION_COOKIE,
        state.security.sessions.create(),
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        max_age=12 * 3600,
        path="/",
    )
    return response


# --------------------------------------------------------------------------
# cluster
# --------------------------------------------------------------------------

@router.get("/join")
async def api_join(request: Request) -> Dict[str, Any]:
    """Addresses and the certificate fingerprint; no code."""
    return join_payload(_state(request))


@router.post("/join/code")
async def api_join_code(request: Request) -> Dict[str, Any]:
    """A new one-time join code (valid once, for ``security.join_minutes``)
    with the Termux command, the browser link and its QR."""
    return join_payload(_state(request), code=True)


@router.get("/stats")
async def stats(request: Request) -> Dict[str, Any]:
    return _state(request).stats()


@router.get("/plugins")
async def plugins(request: Request) -> List[Dict[str, Any]]:
    return _state(request).plugins.describe_all()


@router.get("/events")
async def events(
    request: Request, limit: int = 100, kind: Optional[str] = None, offset: int = 0
) -> List[Dict[str, Any]]:
    state = _state(request)
    limit = max(1, min(int(limit or 100), 1000))
    offset = max(0, int(offset or 0))
    if kind:
        return state.db.select(
            "events",
            "WHERE kind=? ORDER BY id DESC LIMIT ? OFFSET ?",
            (kind, limit, offset),
        )
    return state.db.select(
        "events", "ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
    )


@router.get("/speeds")
async def speeds(request: Request) -> List[Dict[str, Any]]:
    """What the cluster learned: units per second per kind of work and device."""
    return _state(request).speeds.listing()


@router.get("/settings")
async def get_settings(request: Request) -> Dict[str, Any]:
    return _state(request).settings_view()


@router.patch("/settings")
async def patch_settings(request: Request, body: SettingsPatch) -> Dict[str, Any]:
    state = _state(request)
    patch = body.model_dump(exclude_none=True)
    pin = patch.get("pin")
    if pin is not None and not str(pin).strip().isdigit():
        raise HTTPException(status_code=400, detail="pin must be digits")
    view = state.save_settings(patch)
    if pin:
        log.info("cluster PIN changed")
    state.scheduler.wake()
    return view


# --------------------------------------------------------------------------
# devices
# --------------------------------------------------------------------------

@router.get("/devices")
async def list_devices(request: Request, limit: int = 200, offset: int = 0) -> List[Dict[str, Any]]:
    state = _state(request)
    rows = state.db.select(
        "devices",
        "WHERE removed=0 ORDER BY created ASC LIMIT ? OFFSET ?",
        (max(1, min(int(limit or 200), 1000)), max(0, int(offset or 0))),
    )
    busy = {
        row["device_id"]: int(row["n"])
        for row in state.db.rows(
            "SELECT device_id, COUNT(*) AS n FROM tasks WHERE status IN "
            "('assigned','running') AND device_id IS NOT NULL GROUP BY device_id"
        )
    }
    for device in rows:
        device.pop("token_hash", None)
        device["running"] = busy.get(device["id"], 0)
        device["healthy"] = state.scheduler.healthy(device)
        device["control"] = state.safety.view(device)
        device["trust"] = state.verifier.view(device)
        device["update"] = state.updates.view(device)
    return rows


@router.get("/devices/{device_id}")
async def get_device(request: Request, device_id: str) -> Dict[str, Any]:
    state = _state(request)
    device = state.db.get("devices", device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="no such device")
    device.pop("token_hash", None)
    device["running"] = int(
        state.db.scalar(
            "SELECT COUNT(*) FROM tasks WHERE device_id=? AND status IN ('assigned','running')",
            (device_id,),
            default=0,
        )
    )
    device["tasks_done"] = int(
        state.db.scalar(
            "SELECT COUNT(*) FROM tasks WHERE device_id=? AND status='done'",
            (device_id,),
            default=0,
        )
    )
    device["healthy"] = state.scheduler.healthy(device)
    device["control"] = state.safety.view(device)
    device["trust"] = state.verifier.view(device)
    device["update"] = state.updates.view(device)
    return device


@router.patch("/devices/{device_id}")
async def patch_device(request: Request, device_id: str, body: DevicePatch) -> Dict[str, Any]:
    state = _state(request)
    device = state.db.get("devices", device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="no such device")
    fields: Dict[str, Any] = {}
    command: Dict[str, Any] = {"cmd": "config"}
    if body.name is not None:
        fields["name"] = str(body.name)
        # a name given here survives the worker joining again (restarts, updates)
        fields["settings"] = dict(device.get("settings") or {}, name_set=True)
        device["settings"] = fields["settings"]
    if body.slots is not None:
        fields["slots"] = max(1, int(body.slots))
        command["slots"] = fields["slots"]
    if body.paused is not None:
        fields["paused"] = 1 if body.paused else 0
        command["paused"] = bool(body.paused)
    if body.settings is not None:
        try:
            patch = policy.clean_settings(dict(body.settings))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        merged = dict(device.get("settings") or {})
        if "roles" in patch:
            roles = dict(merged.get("roles") or {})
            roles.update(patch.pop("roles"))
            merged["roles"] = roles
        merged.update(patch)
        fields["settings"] = merged
    if fields:
        state.db.update("devices", {"id": device_id}, fields)
    if len(command) > 1:
        state.send_command(device_id, command)
    if body.paused is not None:
        state.event(
            "device_paused",
            "%s %s" % (device.get("name"), "switched off" if body.paused else "switched on"),
            device_id=device_id,
        )
    state.publish_device(device_id)
    state.scheduler.wake()
    state.llm.wake()  # role or limit changes can move models
    updated = state.db.get("devices", device_id) or {}
    updated.pop("token_hash", None)
    updated["control"] = state.safety.view(updated)
    updated["trust"] = state.verifier.view(updated)
    return updated


@router.delete("/devices/{device_id}")
async def remove_device(request: Request, device_id: str) -> Dict[str, Any]:
    state = _state(request)
    if state.db.get("devices", device_id) is None:
        raise HTTPException(status_code=404, detail="no such device")
    ok = state.scheduler.remove_device(device_id)
    return {"ok": ok, "id": device_id}


@router.get("/trust")
async def list_trust(request: Request) -> List[Dict[str, Any]]:
    """How far each device's answers can be trusted (SPEC §9.9)."""
    state = _state(request)
    out = []
    for device in state.db.select("devices", "WHERE removed=0 ORDER BY created ASC"):
        view = state.verifier.view(device)
        view.update({"device_id": device["id"], "name": device.get("name"),
                     "status": device.get("status")})
        out.append(view)
    return out


@router.post("/devices/{device_id}/trust")
async def trust_device(request: Request, device_id: str,
                       body: Optional[TrustPatch] = None) -> Dict[str, Any]:
    """*Trust again*: forgive past wrong answers, jobs resume (checked often at first)."""
    state = _state(request)
    try:
        view = state.verifier.reset(device_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="no such device")
    return dict(view, device_id=device_id)


@router.post("/devices/{device_id}/benchmark")
async def benchmark_device(request: Request, device_id: str) -> Dict[str, Any]:
    state = _state(request)
    device = state.db.get("devices", device_id)
    if device is None or device.get("removed"):
        raise HTTPException(status_code=404, detail="no such device")
    state.send_command(device_id, {"cmd": "benchmark"})
    return {"ok": True, "id": device_id}


@router.post("/devices/{device_id}/notify")
async def notify_device(request: Request, device_id: str, body: NotifyRequest) -> Dict[str, Any]:
    state = _state(request)
    ok = state.notifier.notify_device(device_id, body.title, body.body)
    if not ok:
        raise HTTPException(status_code=404, detail="no such device")
    return {"ok": True, "id": device_id}


@router.post("/devices/{device_id}/screen")
async def show_screen(request: Request, device_id: str) -> Dict[str, Any]:
    """Open the phone's screen in a window on this PC (scrcpy, SPEC §10)."""
    state = _state(request)
    device = state.db.get("devices", device_id)
    if device is None or device.get("removed"):
        raise HTTPException(status_code=404, detail="no such device")
    try:
        return await asyncio.to_thread(state.screens.open, device)
    except ScreenError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# --------------------------------------------------------------------------
# terminal
# --------------------------------------------------------------------------

@router.post("/terminal")
async def open_terminal(request: Request, body: TerminalOpen) -> Dict[str, Any]:
    state = _state(request)
    try:
        session = state.terminals.open(body.device_id, body.pty, body.cols, body.rows)
    except KeyError:
        raise HTTPException(status_code=404, detail="no such device")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return session.to_dict()


@router.get("/terminal")
async def list_terminals(request: Request) -> List[Dict[str, Any]]:
    return _state(request).terminals.list()


@router.get("/terminal/{session_id}")
async def terminal_scrollback(request: Request, session_id: str) -> Dict[str, Any]:
    """An open session with its scrollback, so the dashboard can pick it up again."""
    session = _state(request).terminals.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="no such session")
    return session.scrollback()


@router.post("/terminal/{session_id}/resize")
async def resize_terminal(request: Request, session_id: str, body: TerminalResize) -> Dict[str, Any]:
    """A full terminal's new size (line sessions have none)."""
    terminals = _state(request).terminals
    session = terminals.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="no such session")
    if not terminals.resize(session_id, body.cols, body.rows, body.redraw):
        raise HTTPException(status_code=400, detail="not a full terminal")
    return session.to_dict()


@router.delete("/terminal/{session_id}")
async def close_terminal(request: Request, session_id: str) -> Dict[str, Any]:
    state = _state(request)
    ok = state.terminals.close(session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="no such session")
    return {"ok": True, "session_id": session_id}
