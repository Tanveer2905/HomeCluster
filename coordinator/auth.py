"""Authentication helpers (SPEC §3).

* Dashboard / SDK / CLI: header ``X-Pin`` or the ``pc_session`` cookie (set by
  ``POST /api/login``), and only from the PC itself (``coordinator/security.py``).
* Workers: header ``X-Device-Token`` (DB stores ``sha256(token)``), from anywhere.
* Wrong PINs and unknown device tokens count towards a lockout per address.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request

log = logging.getLogger("coordinator.auth")

SESSION_COOKIE = "pc_session"
PIN_HEADER = "X-Pin"
TOKEN_HEADER = "X-Device-Token"


def hash_token(token: str) -> str:
    """``sha256`` of a device token, hex encoded."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    """Random 32-byte hex token."""
    return secrets.token_hex(32)


def unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="unauthorized")


def removed() -> HTTPException:
    return HTTPException(status_code=410, detail="removed")


def locked(seconds: float) -> HTTPException:
    return HTTPException(status_code=429, detail="too many wrong attempts; try again in %d s"
                         % max(1, int(seconds + 0.999)))


def pin_matches(supplied: Optional[str], expected: str) -> bool:
    if not supplied or not expected:
        return False
    return hmac.compare_digest(str(supplied), str(expected))


def client_host(request: Any) -> str:
    client = getattr(request, "client", None)
    return str(getattr(client, "host", "") or "")


def _state(request: Any) -> Any:
    state = getattr(request.app.state, "cluster", None)
    if state is None:  # pragma: no cover - app not started
        raise unauthorized()
    return state


def pin_ok(state: Any, request: Any, supplied: Optional[str] = None) -> bool:
    """Is this a dashboard client: on the PC itself, with the PIN or a session?

    A wrong PIN counts towards the lockout; no PIN at all does not."""
    host = client_host(request)
    security = state.security
    if not security.is_local(host):
        return False
    if security.sessions.valid(request.cookies.get(SESSION_COOKIE)):
        return True
    pin = supplied if supplied is not None else request.headers.get(PIN_HEADER)
    if not pin:
        return False
    wait = security.guard.blocked(host, local=True)
    if wait:
        raise locked(wait)
    if pin_matches(pin, state.config.pin):
        security.guard.ok(host)
        return True
    security.guard.fail(host, local=True)
    return False


def check_pin(request: Request) -> None:
    """Raise unless the request comes from the PC itself with the PIN."""
    state = _state(request)
    if not state.security.is_local(client_host(request)):
        raise HTTPException(status_code=403, detail="the dashboard only opens on the PC itself")
    if not pin_ok(state, request):
        raise unauthorized()


async def require_pin(request: Request) -> None:
    """FastAPI dependency for ``/api/*`` dashboard endpoints."""
    check_pin(request)


def device_for_token(state: Any, token: Optional[str], host: str = "") -> Dict[str, Any]:
    """Look up a device by raw token, raising 401/410/429 as per SPEC §3."""
    if not token:
        raise unauthorized()
    device = state.db.get("devices", hash_token(token), id_col="token_hash")
    if device is None:
        security = getattr(state, "security", None)
        if security is not None and host:
            local = security.is_local(host)
            security.guard.fail(host, local)
            wait = security.guard.blocked(host, local)
            if wait:
                raise locked(wait)
        raise unauthorized()
    if device.get("removed"):
        raise removed()
    return device


async def require_device(request: Request) -> Dict[str, Any]:
    """FastAPI dependency for ``/api/w/*`` worker endpoints."""
    state = _state(request)
    device = device_for_token(state, request.headers.get(TOKEN_HEADER), client_host(request))
    request.state.device = device
    return device


async def require_pin_or_device(request: Request) -> Optional[Dict[str, Any]]:
    """Dashboard/SDK on the PC (PIN) **or** a cluster member (device token).

    Used by the service call endpoints so a browser worker page, which only
    holds its device token, can use services too.  Returns the device row
    when a token authenticated the call, else ``None``.
    """
    state = _state(request)
    token = request.headers.get(TOKEN_HEADER)
    if token:
        device = device_for_token(state, token, client_host(request))
        request.state.device = device
        return device
    if pin_ok(state, request):
        return None
    raise unauthorized()
