"""FastAPI application factory (SPEC §2, §11)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional, Union

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import VERSION
from .state import ClusterState

log = logging.getLogger("coordinator.app")

PathLike = Union[str, Path]


def create_app(data_dir: Optional[PathLike] = None) -> FastAPI:
    """Build the coordinator app.  ``data_dir`` overrides ``PC_DATA_DIR``."""
    state = ClusterState(Path(data_dir) if data_dir else None)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await state.startup()
        try:
            yield
        finally:
            await state.shutdown()

    app = FastAPI(
        title="PhoneCluster",
        version=VERSION,
        description="LAN compute cluster of old Android phones (see docs/SPEC.md)",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.cluster = state
    # unknown host names refused; everything but the worker side only for this PC (SPEC 3)
    from .security import SecurityMiddleware

    app.add_middleware(SecurityMiddleware)

    # ------------------------------------------------------------- errors
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail: Any = exc.detail
        message = detail if isinstance(detail, str) else str(detail)
        return JSONResponse(
            {"error": message}, status_code=exc.status_code, headers=getattr(exc, "headers", None)
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = exc.errors()
        first = errors[0] if errors else {}
        location = ".".join(str(p) for p in (first.get("loc") or [])[1:])
        message = "%s: %s" % (location or "body", first.get("msg", "invalid request"))
        return JSONResponse({"error": message, "detail": errors}, status_code=422)

    from .storage import StorageError

    @app.exception_handler(StorageError)
    async def storage_error(request: Request, exc: StorageError) -> JSONResponse:
        # e.g. erasure coding asked for with fewer than three storage devices
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"error": "internal error: %s" % exc}, status_code=500)

    # ------------------------------------------------------------- routes
    from . import (api_devices, api_files, api_jobs, api_llm, api_services, api_worker,
                   static_routes, ws_routes)

    app.include_router(api_worker.router)
    app.include_router(api_llm.worker_router)
    app.include_router(api_llm.router)
    app.include_router(api_devices.public)
    app.include_router(api_devices.router)
    app.include_router(api_jobs.router)
    app.include_router(api_files.router)
    app.include_router(api_services.router)
    app.include_router(ws_routes.router)
    app.include_router(static_routes.router)
    static_routes.install_static(app, state.root)
    return app
