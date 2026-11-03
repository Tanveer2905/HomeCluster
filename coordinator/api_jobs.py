"""Job, pipeline and task endpoints (SPEC §11)."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import secrets
import shutil
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import StreamingResponse

from . import packages
from .auth import require_pin
from .models import JobSpec, PipelineRequest, RerunRequest

log = logging.getLogger("coordinator.api.jobs")

router = APIRouter(prefix="/api", tags=["jobs"], dependencies=[Depends(require_pin)])


def _state(request: Request) -> Any:
    return request.app.state.cluster


def _job_or_404(state: Any, job_id: str) -> Dict[str, Any]:
    job = state.db.get("jobs", job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="no such job")
    return job


def _clean(job: Dict[str, Any]) -> Dict[str, Any]:
    """Hide internal option keys from API consumers."""
    out = dict(job)
    options = dict(out.get("options") or {})
    dynamic = options.get("_dynamic")
    if isinstance(dynamic, dict):
        # automatically sized job (SPEC 9.6): progress in the plugin's units
        out["progress_units"] = {"units": dynamic.get("units"), "done": dynamic.get("done"),
                                 "cut": dynamic.get("cursor"), "pieces": dynamic.get("pieces")}
    if options.get("_cached"):
        out["cached"] = options["_cached"]  # answered by an identical earlier job (SPEC 9.8)
    if options.get("_rechecked"):
        out["rechecked"] = options["_rechecked"]  # computed again after a wrong answer (SPEC 9.9)
    for key in list(options):
        if key.startswith("_"):
            options.pop(key)
    out["options"] = options
    return out


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------

@router.get("/jobs")
async def list_jobs(
    request: Request,
    status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    state = _state(request)
    limit = max(1, min(int(limit or 100), 1000))
    offset = max(0, int(offset or 0))
    if status:
        rows = state.db.select(
            "jobs",
            "WHERE status=? ORDER BY created DESC LIMIT ? OFFSET ?",
            (status, limit, offset),
        )
    else:
        rows = state.db.select(
            "jobs", "ORDER BY created DESC LIMIT ? OFFSET ?", (limit, offset)
        )
    return [_clean(job) for job in rows]


@router.post("/jobs")
async def submit_job(request: Request, spec: JobSpec) -> Dict[str, Any]:
    state = _state(request)
    try:
        job = state.submit_job(spec.model_dump(exclude_none=False))
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc).strip("'\""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _clean(job)


@router.post("/pipelines")
async def submit_pipeline(request: Request, body: PipelineRequest) -> Dict[str, Any]:
    state = _state(request)
    try:
        return state.submit_pipeline(body.name, list(body.jobs or []))
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc).strip("'\""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/packages")
async def submit_package(
    request: Request,
    file: UploadFile = File(...),
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Upload a zipped job package (``job.yaml`` + files) and submit it.

    ``?dry_run=true`` returns the resolved job/pipeline spec without
    submitting or uploading anything (file references show as
    ``<upload:name>``).  See ``coordinator/packages.py`` / docs/PACKAGES.md.
    """
    state = _state(request)
    work = Path(state.data) / "tmp" / ("pkg-%s" % secrets.token_hex(6))
    work.mkdir(parents=True, exist_ok=True)
    archive = work / "package.zip"
    try:
        with archive.open("wb") as handle:
            while True:
                block = await file.read(1024 * 1024)
                if not block:
                    break
                handle.write(block)

        def run() -> Dict[str, Any]:
            root = packages.unpack_zip(archive, work / "pkg")
            return packages.submit(root, state, dry_run=dry_run)

        try:
            result = await asyncio.to_thread(run)
        except packages.PackageError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc).strip("'\""))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if result.get("job"):
        result["job"] = _clean(result["job"])
    return result


@router.get("/jobs/{job_id}")
async def get_job(request: Request, job_id: str) -> Dict[str, Any]:
    return _clean(_job_or_404(_state(request), job_id))


@router.get("/jobs/{job_id}/tasks")
async def job_tasks(request: Request, job_id: str) -> List[Dict[str, Any]]:
    state = _state(request)
    _job_or_404(state, job_id)
    tasks = state.db.select("tasks", "WHERE job_id=? ORDER BY idx ASC", (job_id,))
    for task in tasks:
        task.pop("payload", None)
    return tasks


@router.get("/jobs/{job_id}/result")
async def job_result(request: Request, job_id: str) -> Dict[str, Any]:
    job = _job_or_404(_state(request), job_id)
    return {
        "id": job["id"],
        "status": job["status"],
        "result": job.get("result"),
        "error": job.get("error"),
    }


@router.get("/jobs/{job_id}/results")
async def job_results(request: Request, job_id: str) -> Response:
    """Stream a job's items as JSON lines (SPEC §6.6).

    For a job with ``options.stream_results`` this is the results file once
    done, or every part written so far while it runs.  For any other job the
    final ``result`` is streamed one line per element (or one line).
    """
    state = _state(request)
    job = _job_or_404(state, job_id)
    name = "%s.jsonl" % str(job.get("name") or job_id).replace('"', "")
    headers = {"Content-Disposition": 'attachment; filename="%s"' % name}
    if state.results.enabled(job):
        return StreamingResponse(
            state.results.iter_lines(job), media_type="application/x-ndjson", headers=headers
        )
    if job["status"] != "done":
        return Response(status_code=204)
    result = job.get("result")
    items = result if isinstance(result, list) else [result]
    body = "".join(json.dumps(item, default=repr) + "\n" for item in items)
    return Response(body, media_type="application/x-ndjson", headers=headers)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: str) -> Dict[str, Any]:
    state = _state(request)
    _job_or_404(state, job_id)
    ok = state.scheduler.cancel_job(job_id)
    return {"ok": ok, "id": job_id, "status": (state.db.get("jobs", job_id) or {}).get("status")}


@router.post("/jobs/{job_id}/rerun")
async def rerun_job(request: Request, job_id: str,
                    body: Optional[RerunRequest] = None) -> Dict[str, Any]:
    """Submit the job again; ``{"fresh": true}`` computes everything again
    instead of reusing stored results (SPEC §9.8)."""
    state = _state(request)
    _job_or_404(state, job_id)
    return _clean(state.rerun_job(job_id, fresh=bool(body and body.fresh)))


@router.get("/cache")
async def cache_info(request: Request, limit: int = 50) -> Dict[str, Any]:
    """Stored results: how many, how big, how often reused (SPEC §9.8)."""
    state = _state(request)
    return {"stats": state.cache.stats(),
            "entries": state.cache.listing(max(1, min(int(limit or 50), 1000)))}


@router.delete("/cache")
async def cache_clear(request: Request) -> Dict[str, Any]:
    return {"ok": True, "removed": _state(request).cache.clear()}


@router.post("/jobs/{job_id}/retry")
async def retry_job(request: Request, job_id: str) -> Dict[str, Any]:
    state = _state(request)
    _job_or_404(state, job_id)
    ok = state.scheduler.retry_job(job_id)
    return {"ok": ok, "id": job_id, "status": (state.db.get("jobs", job_id) or {}).get("status")}


@router.delete("/jobs/{job_id}")
async def delete_job(request: Request, job_id: str) -> Dict[str, Any]:
    state = _state(request)
    job = _job_or_404(state, job_id)
    if job["status"] in ("pending", "running", "waiting"):
        state.scheduler.cancel_job(job_id, "job deleted")
    state.results.discard(job_id)
    state.db.execute("DELETE FROM tasks WHERE job_id=?", (job_id,))
    state.db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    state.hub.publish({"ev": "job", "job": {"id": job_id}, "deleted": True})
    return {"ok": True, "id": job_id}


# --------------------------------------------------------------------------
# export (SPEC §11)
# --------------------------------------------------------------------------

def _tasks_csv(tasks: List[Dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["idx", "task_id", "device_id", "status", "attempts", "duration", "error", "result"])
    for task in tasks:
        writer.writerow(
            [
                task.get("idx"),
                task.get("id"),
                task.get("device_id") or "",
                task.get("status"),
                task.get("attempts"),
                task.get("duration") if task.get("duration") is not None else "",
                task.get("error") or "",
                json.dumps(task.get("result"), default=str),
            ]
        )
    return buffer.getvalue()


@router.get("/jobs/{job_id}/export")
async def export_job(request: Request, job_id: str, format: str = "json") -> Response:
    state = _state(request)
    job = _clean(_job_or_404(state, job_id))
    tasks = state.db.select("tasks", "WHERE job_id=? ORDER BY idx ASC", (job_id,))
    for task in tasks:
        task.pop("payload", None)
    fmt = (format or "json").lower()

    if fmt == "json":
        body = json.dumps(
            {"job": job, "tasks": tasks, "result": job.get("result")},
            indent=2,
            default=str,
        ).encode("utf-8")
        return Response(
            content=body,
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="%s.json"' % job_id},
        )
    if fmt == "csv":
        return Response(
            content=_tasks_csv(tasks).encode("utf-8"),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="%s.csv"' % job_id},
        )
    if fmt != "zip":
        raise HTTPException(status_code=400, detail="format must be json, csv or zip")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "result.json",
            json.dumps(
                {"job": job, "result": job.get("result"), "error": job.get("error")},
                indent=2,
                default=str,
            ),
        )
        archive.writestr("job.json", json.dumps(job, indent=2, default=str))
        archive.writestr("tasks.csv", _tasks_csv(tasks))
        for task in tasks:
            lines = state.read_task_log(task["id"], tail=0)
            if lines:
                archive.writestr(
                    "logs/task-%s-%s.log" % (task.get("idx"), task["id"]),
                    "\n".join(lines) + "\n",
                )
            for output in task.get("outputs") or []:
                file_id = str(output.get("file_id") or "")
                if not file_id:
                    continue
                try:
                    data = state.files.read(file_id)
                except (OSError, ValueError):
                    continue
                name = str(output.get("name") or file_id)
                archive.writestr("outputs/%s_%s" % (task.get("idx"), name), data)
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="%s.zip"' % job_id},
    )


# --------------------------------------------------------------------------
# tasks
# --------------------------------------------------------------------------

@router.get("/tasks/{task_id}")
async def get_task(request: Request, task_id: str) -> Dict[str, Any]:
    state = _state(request)
    task = state.db.get("tasks", task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="no such task")
    return task


@router.get("/tasks/{task_id}/log")
async def get_task_log(request: Request, task_id: str, tail: Optional[int] = None) -> Dict[str, Any]:
    state = _state(request)
    lines = state.read_task_log(task_id, tail=tail)
    return {"task_id": task_id, "lines": lines}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(request: Request, task_id: str) -> Dict[str, Any]:
    state = _state(request)
    if state.db.get("tasks", task_id) is None:
        raise HTTPException(status_code=404, detail="no such task")
    ok = state.scheduler.cancel_task(task_id)
    return {"ok": ok, "id": task_id}
