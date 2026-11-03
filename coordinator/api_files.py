"""Files, pooled storage, schedules and templates (SPEC §10, §11)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from .auth import require_pin
from .db import new_id, now
from .models import ScheduleCreate, SchedulePatch, TemplateCreate
from .schedules import CronError, next_run, validate

log = logging.getLogger("coordinator.api.files")

router = APIRouter(prefix="/api", tags=["files"], dependencies=[Depends(require_pin)])

BLOCK = 1024 * 1024


def _state(request: Request) -> Any:
    return request.app.state.cluster


async def _spool(request: Request, upload: Optional[UploadFile], destination: Path) -> int:
    """Write an upload (multipart or raw body) to ``destination``; return its size."""
    size = 0
    with destination.open("wb") as handle:
        if upload is not None:
            while True:
                block = await upload.read(BLOCK)
                if not block:
                    break
                size += len(block)
                handle.write(block)
        else:
            async for block in request.stream():
                if block:
                    size += len(block)
                    handle.write(block)
    return size


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------

@router.get("/files")
async def list_files(request: Request, limit: int = 200, offset: int = 0) -> List[Dict[str, Any]]:
    return _state(request).files.list(
        limit=max(1, min(int(limit or 200), 1000)), offset=max(0, int(offset or 0))
    )


@router.post("/files")
async def upload_file(request: Request, file: UploadFile = File(...)) -> Dict[str, Any]:
    state = _state(request)
    tmp = Path(state.data) / "tmp" / ("up-%s" % secrets.token_hex(6))
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        await _spool(request, file, tmp)
        meta = await asyncio.to_thread(
            state.files.add_path, tmp, file.filename or "upload.bin", file.content_type
        )
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()
    return meta


@router.put("/files")
async def put_file(request: Request, name: str = "upload.bin") -> Dict[str, Any]:
    state = _state(request)
    tmp = Path(state.data) / "tmp" / ("up-%s" % secrets.token_hex(6))
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        await _spool(request, None, tmp)
        meta = await asyncio.to_thread(state.files.add_path, tmp, name, None)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()
    return meta


@router.get("/files/{file_id}")
async def file_meta(request: Request, file_id: str) -> Dict[str, Any]:
    meta = _state(request).files.meta(file_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="no such file")
    return meta


@router.get("/files/{file_id}/download")
async def download_file(request: Request, file_id: str) -> Any:
    state = _state(request)
    response = state.files.response(file_id, request.headers.get("range"))
    if response is None:
        raise HTTPException(status_code=404, detail="no such file")
    return response


@router.delete("/files/{file_id}")
async def delete_file(request: Request, file_id: str) -> Dict[str, Any]:
    state = _state(request)
    if not state.files.delete(file_id):
        raise HTTPException(status_code=404, detail="no such file")
    return {"ok": True, "id": file_id}


# --------------------------------------------------------------------------
# pooled storage
# --------------------------------------------------------------------------

@router.get("/storage")
async def list_blobs(request: Request, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    return _state(request).storage.list(
        limit=max(1, min(int(limit or 100), 1000)), offset=max(0, int(offset or 0))
    )


@router.post("/storage")
async def put_blob(
    request: Request,
    file: Optional[UploadFile] = File(None),
    replication: Optional[str] = Form(None),
    name: Optional[str] = Form(None),
    scheme: Optional[str] = Form(None),
) -> Dict[str, Any]:
    """Multipart upload, or JSON ``{file_id, name?, replication?, scheme?}``.

    ``scheme``: ``auto`` (erasure coding from 3 storage devices, the default),
    ``copies`` or ``ec``.  Giving ``replication`` alone asks for copies."""
    state = _state(request)
    repl: Optional[int] = None
    if replication not in (None, ""):
        try:
            repl = int(replication)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="replication must be a number")

    if file is not None:
        tmp = Path(state.data) / "tmp" / ("blob-%s" % secrets.token_hex(6))
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            await _spool(request, file, tmp)
            blob = await state.storage.put_path(
                tmp, name or file.filename or "upload.bin", repl, scheme=scheme or None
            )
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()
        return blob

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict) or not body.get("file_id"):
        raise HTTPException(status_code=400, detail="send a multipart file or {file_id}")
    file_id = str(body["file_id"])
    meta = state.files.meta(file_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="no such file")
    if body.get("replication") is not None:
        repl = int(body["replication"])
    return await state.storage.put_path(
        state.files.path_for(file_id), str(body.get("name") or meta.get("name")), repl,
        scheme=body.get("scheme") or None,
    )


@router.get("/storage/{blob_id}/status")
async def blob_status(request: Request, blob_id: str) -> Dict[str, Any]:
    status = _state(request).storage.status(blob_id)
    if status is None:
        raise HTTPException(status_code=404, detail="no such blob")
    return status


@router.get("/storage/{blob_id}")
async def get_blob(request: Request, blob_id: str) -> StreamingResponse:
    state = _state(request)
    blob = state.db.get("blobs", blob_id)
    if blob is None:
        raise HTTPException(status_code=404, detail="no such blob")
    name = str(blob.get("name") or blob_id).replace('"', "")
    return StreamingResponse(
        state.storage.stream(blob_id),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": 'attachment; filename="%s"' % name,
            "Content-Length": str(blob.get("size") or 0),
        },
    )


@router.delete("/storage/{blob_id}")
async def delete_blob(request: Request, blob_id: str) -> Dict[str, Any]:
    state = _state(request)
    if not await state.storage.delete(blob_id):
        raise HTTPException(status_code=404, detail="no such blob")
    return {"ok": True, "id": blob_id}


# --------------------------------------------------------------------------
# schedules
# --------------------------------------------------------------------------

@router.get("/schedules")
async def list_schedules(request: Request) -> List[Dict[str, Any]]:
    return _state(request).db.select("schedules", "ORDER BY created DESC")


@router.post("/schedules")
async def create_schedule(request: Request, body: ScheduleCreate) -> Dict[str, Any]:
    state = _state(request)
    try:
        return state.create_schedule(body.name, body.cron, body.job_spec, body.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/schedules/{schedule_id}")
async def patch_schedule(request: Request, schedule_id: str, body: SchedulePatch) -> Dict[str, Any]:
    state = _state(request)
    row = state.db.get("schedules", schedule_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such schedule")
    fields: Dict[str, Any] = {}
    if body.name is not None:
        fields["name"] = str(body.name)
    if body.job_spec is not None:
        fields["job_spec"] = body.job_spec
    if body.cron is not None:
        try:
            fields["cron"] = validate(body.cron)
        except CronError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    if body.enabled is not None:
        fields["enabled"] = 1 if body.enabled else 0
    cron = fields.get("cron", row["cron"])
    enabled = fields.get("enabled", row["enabled"])
    try:
        fields["next_run"] = next_run(cron) if enabled else None
    except CronError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    state.db.update("schedules", {"id": schedule_id}, fields)
    return state.db.get("schedules", schedule_id) or {}


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(request: Request, schedule_id: str) -> Dict[str, Any]:
    state = _state(request)
    if state.db.get("schedules", schedule_id) is None:
        raise HTTPException(status_code=404, detail="no such schedule")
    state.db.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
    return {"ok": True, "id": schedule_id}


@router.post("/schedules/{schedule_id}/run")
async def run_schedule(request: Request, schedule_id: str) -> Dict[str, Any]:
    state = _state(request)
    row = state.db.get("schedules", schedule_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such schedule")
    job_id = await state.schedules.fire(row)
    if job_id is None:
        return JSONResponse({"error": "the schedule's job spec could not be submitted"},
                            status_code=400)
    return {"ok": True, "id": schedule_id, "job_id": job_id}


# --------------------------------------------------------------------------
# templates
# --------------------------------------------------------------------------

@router.get("/templates")
async def list_templates(request: Request) -> List[Dict[str, Any]]:
    return _state(request).db.select("templates", "ORDER BY created DESC")


@router.post("/templates")
async def create_template(request: Request, body: TemplateCreate) -> Dict[str, Any]:
    state = _state(request)
    row = {
        "id": new_id(),
        "name": body.name,
        "job_spec": body.job_spec,
        "created": now(),
    }
    state.db.insert("templates", row)
    return row


@router.delete("/templates/{template_id}")
async def delete_template(request: Request, template_id: str) -> Dict[str, Any]:
    state = _state(request)
    if state.db.get("templates", template_id) is None:
        raise HTTPException(status_code=404, detail="no such template")
    state.db.execute("DELETE FROM templates WHERE id=?", (template_id,))
    return {"ok": True, "id": template_id}
