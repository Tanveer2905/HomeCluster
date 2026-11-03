"""PhoneCluster coordinator package.

FastAPI application that schedules work onto old Android phones (Termux or
browser workers) and the local PC worker.  See ``docs/SPEC.md``.
"""

from __future__ import annotations

__all__ = ["VERSION", "create_app"]

VERSION = "1.0.0"


def create_app(data_dir=None):  # type: ignore[no-untyped-def]
    """Lazy re-export of :func:`coordinator.app.create_app`."""
    from .app import create_app as _create_app

    return _create_app(data_dir)
