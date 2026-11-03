"""Caller-facing service endpoints (SPEC §6.7, §11).

    GET  /api/services                          running services + instances
    POST /api/services/{name}/call              send a request, get the result
    GET  /api/requests                          recent requests
    GET  /api/requests/{id}                     one request (with partials)
    GET  /api/requests/{id}/partials?since=N    long-poll for new partials
    POST /api/requests/{id}/cancel

``call`` takes the request body in one of three shapes:
  * raw bytes with any ``Content-Type`` (audio, image, ...): streamed to the
    service as it arrives, so a big file starts being processed at once;
  * ``multipart/form-data`` with a ``file`` part (browser forms) and an
    optional ``params`` part holding JSON;
  * ``application/json`` ``{"text": ..., "params": {...}}`` or any JSON: the
    service receives the JSON text as its body.
Extra settings go in ``?params=<json>``; a client may pick its own
``?request_id=`` so it can follow partials on a second connection while the
upload is still in flight.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .auth import require_pin_or_device
from .services import DEFAULT_QUEUE_TIMEOUT, DEFAULT_REQUEST_TIMEOUT, Unavailable

log = logging.getLogger("coordinator.api.services")

router = APIRouter(prefix="/api", tags=["services"], dependencies=[Depends(require_pin_or_device)])


def _state(request: Request) -> Any:
    return request.app.state.cluster


def _caller(request: Request) -> Optional[str]:
    device = getattr(request.state, "device", None)
    return str(device["id"]) if isinstance(device, dict) else None


@router.get("/services")
async def list_services(request: Request) -> List[Dict[str, Any]]:
    return _state(request).services.services()


@router.get("/requests")
async def list_requests(request: Request, limit: int = 50) -> List[Dict[str, Any]]:
    return _state(request).services.recent(max(1, min(int(limit or 50), 200)))


@router.get("/requests/{request_id}")
async def get_request(request: Request, request_id: str) -> Dict[str, Any]:
    req = _state(request).services.requests.get(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="unknown request")
    return req.to_dict()


@router.get("/requests/{request_id}/partials")
async def request_partials(request: Request, request_id: str, since: int = 0, wait: float = 20.0) -> Dict[str, Any]:
    """Partials after index ``since``; blocks up to ``wait`` s for new ones."""
    state = _state(request)
    req = state.services.requests.get(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="unknown request")
    since = max(0, int(since or 0))
    if len(req.partials) <= since and req.status not in ("done", "failed", "cancelled"):
        await state.services.wait_change(req, min(max(float(wait), 0.0), 60.0))
    return {
        "id": req.id,
        "status": req.status,
        "partials": req.partials[since:],
        "next": len(req.partials),
        "result": req.result if req.status == "done" else None,
        "error": req.error,
        "outputs": req.outputs,
    }


@router.post("/requests/{request_id}/cancel")
async def cancel_request(request: Request, request_id: str) -> Dict[str, Any]:
    state = _state(request)
    req = state.services.requests.get(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="unknown request")
    ok = await state.services.cancel(req, "cancelled by caller")
    return {"ok": ok, "status": req.status}


def _parse_params(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="params must be JSON")
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="params must be a JSON object")
    return value


async def _multipart_body(request: Request) -> Any:
    """(bytes iterator, content_type, size, filename, params) from a form upload."""
    form = await request.form()
    upload = form.get("file")
    params = _parse_params(form.get("params")) if isinstance(form.get("params"), str) else {}
    if upload is None or isinstance(upload, str):
        text = form.get("text")
        if isinstance(text, str):
            data = text.encode("utf-8")
            return _iter_bytes(data), "text/plain; charset=utf-8", len(data), None, params
        raise HTTPException(status_code=400, detail="multipart call needs a 'file' or 'text' part")
    size = getattr(upload, "size", None)

    async def gen() -> AsyncIterator[bytes]:
        while True:
            block = await upload.read(64 * 1024)
            if not block:
                break
            yield block

    return gen(), upload.content_type or "application/octet-stream", size, upload.filename, params


async def _iter_bytes(data: bytes) -> AsyncIterator[bytes]:
    if data:
        yield data


@router.post("/services/{name}/call")
async def call_service(
    request: Request,
    name: str,
    params: Optional[str] = None,
    request_id: Optional[str] = None,
    queue_timeout: float = DEFAULT_QUEUE_TIMEOUT,
    timeout: Optional[float] = None,
) -> JSONResponse:
    state = _state(request)
    registry = state.services
    content_type = request.headers.get("content-type") or "application/octet-stream"
    extra = _parse_params(params)
    filename = request.headers.get("x-file-name")
    size: Optional[int] = None
    body: AsyncIterator[bytes]
    if content_type.startswith("multipart/form-data"):
        body, content_type, size, filename, form_params = await _multipart_body(request)
        extra = dict(form_params, **extra)
    else:
        length = request.headers.get("content-length")
        size = int(length) if length and length.isdigit() else None
        body = request.stream()

    # "chat" and model file names reach the right model (SPEC 6.4d)
    name = state.llm.resolve_alias(name)
    try:
        req = registry.create_request(name, extra, content_type, size, filename, _caller(request),
                                      request_id=request_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    try:
        inst = await registry.dispatch(req, max(0.0, float(queue_timeout)))
    except LookupError as exc:
        registry._finish(req, ok=False, error=str(exc))
        raise HTTPException(status_code=404, detail=str(exc))
    except (TimeoutError, Unavailable) as exc:
        registry._finish(req, ok=False, error=str(exc))
        raise HTTPException(status_code=503, detail=str(exc))

    job = state.db.get("jobs", inst.job_id) or {}
    per_request = float(timeout or (job.get("params") or {}).get("request_timeout") or DEFAULT_REQUEST_TIMEOUT)
    try:
        await registry.feed(req, body, timeout=per_request)
    except asyncio.TimeoutError:
        await registry.cancel(req, "service did not accept the upload in time")
        raise HTTPException(status_code=504, detail="service did not accept the upload in time")
    except Exception as exc:  # client went away mid-upload
        log.info("upload for request %s aborted: %s", req.id, exc)
        await registry.cancel(req, "upload aborted")
        raise HTTPException(status_code=400, detail="upload aborted")

    if not await registry.wait_finished(req, per_request):
        await registry.cancel(req, "request timed out after %.0fs" % per_request)
        return JSONResponse(req.to_dict(), status_code=504)
    status = 200 if req.status == "done" else 502
    return JSONResponse(req.to_dict(), status_code=status)
