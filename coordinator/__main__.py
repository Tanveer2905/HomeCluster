"""``python -m coordinator`` -- start the coordinator (and the PC worker).

    python -m coordinator [--port N] [--host H] [--no-worker] [--data-dir PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator, List, Optional

import uvicorn

from . import VERSION
from .config import data_dir as resolve_data_dir, ensure_dirs, load_config, root_dir

log = logging.getLogger("coordinator.main")

WORKER_STOP_GRACE = 10.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m coordinator",
        description="PhoneCluster coordinator: REST API, dashboard and scheduler",
    )
    parser.add_argument("--port", type=int, default=None, help="TCP port (default from config.yaml)")
    parser.add_argument("--host", default=None, help="bind address (default 0.0.0.0)")
    parser.add_argument(
        "--no-worker", action="store_true", help="do not spawn the local PC worker"
    )
    parser.add_argument("--data-dir", default=None, help="runtime data directory")
    parser.add_argument("--log-level", default=None, help="debug, info, warning, error")
    parser.add_argument("--version", action="version", version="phonecluster-coordinator " + VERSION)
    return parser


def keep_temp_inside(data: Path) -> None:
    """Temporary files (big uploads, child processes) go to ``data/tmp``, not
    Windows' temp folder: everything stays inside the project."""
    import tempfile

    tmp = data / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    for name in ("TMP", "TEMP", "TMPDIR"):
        os.environ[name] = str(tmp)
    tempfile.tempdir = str(tmp)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("multipart").setLevel(logging.WARNING)


def spawn_local_worker(root: Path, data: Path, port: int, code: str, slots: int) -> Optional[subprocess.Popen]:
    """Start ``worker/worker.py`` against the PC-only door (SPEC §2, §3).  It
    joins with a one-time code handed over in its environment (never on the
    command line), or re-attaches with its saved token."""
    script = root / "worker" / "worker.py"
    if not script.is_file():
        log.warning("no worker at %s, not starting the local worker", script)
        return None
    name = "%s-pc" % (platform.node() or "pc")
    command: List[str] = [
        sys.executable,
        "-u",                # flags, not environment: the project's own Python ignores PYTHON* variables
        "-X",
        "utf8",
        str(script),
        "--url",
        "http://127.0.0.1:%d" % port,
        "--state-dir",
        str(data / "worker"),
        "--name",
        name,
    ]
    if slots > 0:
        # a fixed count; 0 (the default) lets the worker use every core but one
        command += ["--slots", str(int(slots))]
    env = dict(os.environ)
    env["PC_ROOT"] = str(root)
    env["PC_JOIN_CODE"] = code
    env.pop("PC_PIN", None)
    env.setdefault("PYTHONUNBUFFERED", "1")
    try:
        proc = subprocess.Popen(command, env=env, cwd=str(root))
    except OSError:
        log.exception("could not start the local worker")
        return None
    log.info("local worker started (pid %s, %s slots)", proc.pid, slots if slots > 0 else "auto")
    return proc


def stop_local_worker(proc: Optional[subprocess.Popen]) -> None:
    if proc is None or proc.poll() is not None:
        return
    log.info("stopping the local worker (pid %s)", proc.pid)
    try:
        proc.terminate()
    except OSError:  # pragma: no cover
        return
    try:
        proc.wait(timeout=WORKER_STOP_GRACE)
    except subprocess.TimeoutExpired:  # pragma: no cover
        log.warning("local worker did not stop, killing it")
        with_suppress_kill(proc)


def with_suppress_kill(proc: subprocess.Popen) -> None:  # pragma: no cover
    try:
        proc.kill()
        proc.wait(timeout=5)
    except Exception:
        pass


def banner(state: Any, port: int, pin: str) -> None:
    log.info("PhoneCluster coordinator %s", VERSION)
    log.info("data dir : %s", state.data)
    log.info("dashboard: %s   (opens only on this PC)", state.local_url())
    log.info("add phones: %s/#/join   (one-time codes)", state.local_url())
    for url in state.urls():
        log.info("phones   : %s   (HTTPS, the cluster's own certificate)", url)
    log.info("PIN      : %s   (for the dashboard on this PC; phones never need it)", pin)


async def _serve(server: uvicorn.Server, local: uvicorn.Server, state: Any, spawn: bool,
                 port: int, pin: str, slots: int) -> None:
    serving = asyncio.create_task(server.serve())
    worker: Optional[subprocess.Popen] = None
    local_serving: Optional["asyncio.Task"] = None
    try:
        while not server.started and not serving.done():
            await asyncio.sleep(0.05)
        if serving.done():
            await serving
            return
        # the PC-only door shares the app; the LAN server ran its lifespan
        local_serving = asyncio.create_task(local.serve())
        while not local.started and not local_serving.done():
            await asyncio.sleep(0.05)
        banner(state, port, pin)
        if spawn:
            code = state.security.codes.create("PC worker")["code"]
            worker = await asyncio.to_thread(
                spawn_local_worker, state.root, state.data, state.config.local_port, code, slots
            )
        await serving
    finally:
        server.should_exit = True
        local.should_exit = True
        if worker is not None:
            await asyncio.to_thread(stop_local_worker, worker)
        for task in (serving, local_serving):
            if task is not None and not task.done():
                with_suppress_task(task)


def with_suppress_task(task: "asyncio.Task") -> None:  # pragma: no cover
    task.cancel()


class QuietServer(uvicorn.Server):
    """The PC-only door: leaves Ctrl+C to the main server, which stops both."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.data_dir:
        os.environ["PC_DATA_DIR"] = str(Path(args.data_dir).expanduser().resolve())
    root = root_dir()
    os.environ.setdefault("PC_ROOT", str(root))
    data = resolve_data_dir(args.data_dir)
    ensure_dirs(data)
    keep_temp_inside(data)
    config = load_config(data)
    level = args.log_level or config.log_level
    setup_logging(level)

    port = int(args.port or config.port)
    host = args.host or config.host
    pin = config.pin
    spawn = bool(config.get("local_worker", True)) and not args.no_worker
    slots = int(config.get("local_worker_slots", 0) or 0)

    from .app import create_app

    app = create_app(data)
    state = app.state.cluster
    state.port = port
    state.security.ensure_tls()
    tls = state.security.tls

    uv_config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level=str(level).lower(),
        access_log=False,
        lifespan="on",
        timeout_graceful_shutdown=5,
        ssl_certfile=str(tls.server_pem),
        ssl_keyfile=str(tls.server_key),
    )
    local_config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=config.local_port,
        log_level=str(level).lower(),
        access_log=False,
        lifespan="off",
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(uv_config)
    local = QuietServer(local_config)
    try:
        asyncio.run(_serve(server, local, state, spawn, port, pin, slots))
    except KeyboardInterrupt:  # pragma: no cover
        log.info("interrupted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
