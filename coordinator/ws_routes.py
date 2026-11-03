"""Dashboard WebSocket route ``/ws/dashboard`` (SPEC §11)."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .auth import HTTPException, pin_ok

log = logging.getLogger("coordinator.ws")

router = APIRouter()


@router.websocket("/ws/dashboard")
async def dashboard_socket(websocket: WebSocket) -> None:
    state = websocket.app.state.cluster
    # only on the PC itself, with the PIN or a dashboard session (SPEC 3)
    try:
        allowed = pin_ok(state, websocket, websocket.query_params.get("pin") or None)
    except HTTPException:  # locked out after wrong PINs
        allowed = False
    if not allowed:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    conn = state.hub.add(websocket)
    try:
        await websocket.send_text(json.dumps(await state.hub.snapshot(), default=str))
        while True:
            raw = await websocket.receive_text()
            await state.hub.handle_client_message(conn, raw)
    except WebSocketDisconnect:
        pass
    except Exception:  # pragma: no cover - client vanished mid-write
        log.debug("dashboard socket closed unexpectedly", exc_info=True)
    finally:
        state.hub.remove(conn)
