"""Distributed LLM endpoints (SPEC 6.8, 11).

Dashboard / SDK (PIN):
    GET    /api/llm/runtime                     pinned llama.cpp build and what is available
    GET    /api/llm/models                      GGUF files in the models folder (+ downloads)
    PUT    /api/llm/models?name=x.gguf          upload a model (raw body)
    POST   /api/llm/models/fetch {url, name?}   download a model on the coordinator
    DELETE /api/llm/models/{name}
    GET    /api/llm/devices                     devices with memory, GPU, llama.cpp build, eligibility
    POST   /api/llm/plan                        preview {model, mode, devices, host, weights, ctx}
    GET    /api/llm/deployments
    POST   /api/llm/deployments                 {name, model, mode, devices, host, weights, ctx, threads, force}

``mode`` ``auto`` (default without host/weights) picks the devices among
``devices`` (empty: any); ``manual`` uses exactly ``devices``.
    GET    /api/llm/deployments/{id|name}
    POST   /api/llm/deployments/{id|name}/stop
    POST   /api/llm/deployments/{id|name}/restart
    POST   /api/llm/deployments/{id|name}/keep  {keep}: never unload / unload when idle
    DELETE /api/llm/deployments/{id|name}       (stopped, idle or failed only)
    GET    /api/llm/default · PUT {model}       the model "chat" reaches

Any model can also just be called: ``POST /api/services/<name>/call`` with
the model's name (file name without .gguf) or ``chat`` loads it on the best
device when it is not running; it unloads again after ``llm.idle_minutes``.

Chatting is a normal service call: ``POST /api/services/<name>/call``.

Workers (device token):
    GET /api/w/runtime/llama                    manifest {tag, assets}
    GET /api/w/runtime/llama/{file}             a build archive
    GET /api/w/models/{name}                    a model file (Range supported)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .auth import require_device, require_pin

router = APIRouter(prefix="/api/llm", tags=["llm"], dependencies=[Depends(require_pin)])
worker_router = APIRouter(prefix="/api/w", tags=["llm-worker"])


def _llm(request: Request) -> Any:
    return request.app.state.cluster.llm


class PlanRequest(BaseModel):
    model: str
    mode: Optional[str] = None
    devices: List[str] = Field(default_factory=list)
    host: Optional[str] = None
    weights: Dict[str, float] = Field(default_factory=dict)
    ctx: int = 2048


class DeployRequest(PlanRequest):
    name: str
    threads: int = 0
    force: bool = False
    extra_args: List[str] = Field(default_factory=list)
    #: unload after this many unused minutes (None: the cluster setting, 0: keep loaded)
    idle_minutes: Optional[float] = None


class KeepRequest(BaseModel):
    keep: bool = True


class DefaultRequest(BaseModel):
    model: Optional[str] = None


class FetchRequest(BaseModel):
    url: str
    name: Optional[str] = None


# ------------------------------------------------------------------ runtime
@router.get("/runtime")
async def runtime(request: Request) -> Dict[str, Any]:
    return _llm(request).runtime_manifest()


# ------------------------------------------------------------------- models
@router.get("/models")
async def models(request: Request) -> List[Dict[str, Any]]:
    return _llm(request).list_models()


@router.put("/models")
async def upload_model(request: Request, name: str) -> Dict[str, Any]:
    try:
        return await _llm(request).save_upload(name, request.stream())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/models/fetch")
async def fetch_model(request: Request, body: FetchRequest) -> Dict[str, Any]:
    try:
        return _llm(request).start_fetch(body.url, body.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/models/{name}")
async def delete_model(request: Request, name: str) -> Dict[str, Any]:
    try:
        _llm(request).delete_model(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'\""))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True}


# ---------------------------------------------------------------- planning
@router.get("/devices")
async def devices(request: Request) -> List[Dict[str, Any]]:
    return _llm(request).devices()


@router.post("/plan")
async def plan(request: Request, body: PlanRequest) -> Dict[str, Any]:
    if body.mode not in (None, "auto", "manual"):
        raise HTTPException(status_code=400, detail="mode must be auto or manual")
    try:
        return _llm(request).plan(body.model, body.devices, body.host, body.weights, body.ctx, body.mode)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'\""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ------------------------------------------------------------- deployments
def _get(request: Request, key: str) -> Dict[str, Any]:
    dep = _llm(request).get(key)
    if dep is None:
        raise HTTPException(status_code=404, detail="no deployment %s" % key)
    return dep


@router.get("/deployments")
async def deployments(request: Request) -> List[Dict[str, Any]]:
    manager = _llm(request)
    return [manager.describe(d) for d in manager.deployments()]


@router.post("/deployments")
async def deploy(request: Request, body: DeployRequest) -> Dict[str, Any]:
    manager = _llm(request)
    if body.mode not in (None, "auto", "manual"):
        raise HTTPException(status_code=400, detail="mode must be auto or manual")
    try:
        dep = manager.deploy(body.name, body.model, body.devices, body.host, body.weights,
                             body.ctx, body.threads, body.force, body.extra_args, body.mode,
                             idle_minutes=body.idle_minutes)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'\""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return manager.describe(dep)


@router.get("/deployments/{key}")
async def deployment(request: Request, key: str) -> Dict[str, Any]:
    return _llm(request).describe(_get(request, key))


@router.post("/deployments/{key}/stop")
async def stop(request: Request, key: str) -> Dict[str, Any]:
    manager = _llm(request)
    return manager.describe(manager.stop(_get(request, key)["id"]))


@router.post("/deployments/{key}/restart")
async def restart(request: Request, key: str) -> Dict[str, Any]:
    manager = _llm(request)
    return manager.describe(manager.restart(_get(request, key)["id"]))


@router.delete("/deployments/{key}")
async def delete(request: Request, key: str) -> Dict[str, Any]:
    try:
        _llm(request).delete(_get(request, key)["id"])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True}


@router.post("/deployments/{key}/keep")
async def keep(request: Request, key: str, body: KeepRequest) -> Dict[str, Any]:
    """Keep a model loaded, or let it unload when unused (``llm.idle_minutes``)."""
    manager = _llm(request)
    return manager.describe(manager.set_keep(_get(request, key)["id"], body.keep))


@router.get("/default")
async def get_default(request: Request) -> Dict[str, Any]:
    manager = _llm(request)
    model = manager.default_model()
    return {"model": model, "name": manager.get_name_for(model) if model else None,
            "aliases": ["chat", "llm"]}


@router.put("/default")
async def set_default(request: Request, body: DefaultRequest) -> Dict[str, Any]:
    """The model ``chat`` reaches."""
    manager = _llm(request)
    try:
        manager.set_default(body.model)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'\""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return await get_default(request)


# ------------------------------------------------------------------ workers
@worker_router.get("/runtime/llama")
async def worker_runtime(request: Request, device: Dict[str, Any] = Depends(require_device)) -> Dict[str, Any]:
    return _llm(request).runtime_manifest()


@worker_router.get("/runtime/llama/{file}")
async def worker_runtime_file(request: Request, file: str,
                              device: Dict[str, Any] = Depends(require_device)) -> FileResponse:
    path = _llm(request).runtime_file(file)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="no such runtime file")
    return FileResponse(path, media_type="application/octet-stream", filename=file)


@worker_router.get("/models/{name}")
async def worker_model(request: Request, name: str,
                       device: Dict[str, Any] = Depends(require_device)) -> FileResponse:
    try:
        path = _llm(request).model_path(name)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'\""))
    # FileResponse honours Range, so an interrupted download resumes
    return FileResponse(path, media_type="application/octet-stream", filename=path.name)
