"""Shared fixtures for the coordinator tests.

Nothing here is autouse on purpose: ``test_sdk_unit.py`` and
``test_worker_unit.py`` belong to other components and must stay unaffected.
Every fixture works on a temporary data directory -- the project's own ``data``
folder is never touched.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("PC_ROOT", str(ROOT))

import httpx  # noqa: E402

QUIET_NOTIFICATIONS = {
    "desktop": False,
    "phone": False,
    "webhook_url": None,
    "on": [],
}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def free_port() -> int:
    """An unused TCP port on the loopback interface."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def wait_for(
    predicate: Callable[[], Any], timeout: float = 10.0, interval: float = 0.05
) -> Any:
    """Poll ``predicate`` until it returns something truthy (or time out)."""
    deadline = time.time() + timeout
    last: Any = None
    while time.time() < deadline:
        last = predicate()
        if asyncio.iscoroutine(last):
            last = await last
        if last:
            return last
        await asyncio.sleep(interval)
    return last


def run_python_impl(impl: Dict[str, Any], ctx: Optional[Any] = None) -> Any:
    """Execute a plugin's python impl in-process (mirrors SPEC §6.1)."""
    namespace: Dict[str, Any] = {"__name__": "pc_task"}
    exec(compile(impl.get("code") or "", "<pc_task>", "exec"), namespace)
    entry = namespace.get(impl.get("entry") or "run")
    if not callable(entry):
        raise NameError("entry point %r not found" % impl.get("entry"))
    return entry(dict(impl.get("args") or {}), ctx or SimpleCtx())


class SimpleCtx:
    """The subset of the worker ``ctx`` (SPEC §6.1) that plugin code uses."""

    def __init__(self, task_id: str = "t0", device_name: str = "fake", inputs: Optional[List[str]] = None):
        self.task_id = task_id
        self.device_name = device_name
        self.workdir = None
        self.inputs = list(inputs or [])
        self.lines: List[str] = []
        self.outputs: List[Dict[str, Any]] = []
        self.progress_values: List[float] = []

    def log(self, *parts: Any) -> None:
        self.lines.append(" ".join(str(p) for p in parts))

    def input(self, file_id: Any) -> str:
        return self.inputs[int(file_id)]

    def output(self, path: str, name: Optional[str] = None) -> None:
        self.outputs.append({"path": path, "name": name or os.path.basename(path)})

    def progress(self, fraction: float) -> None:
        self.progress_values.append(float(fraction))


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

def quiet_extras(cluster: Any) -> None:
    """Result reuse and random second-device checks off, so a test sees
    exactly the tasks it creates; tests of those features switch them on."""
    cluster.config.set("cache.enabled", False)
    cluster.config.set("verify.share", 0.0)
    cluster.config.set("verify.suspect_share", 0.0)
    # the in-process test client calls itself "cluster.test" (SPEC 3 host check)
    cluster.config.set("security.extra_hosts", ["cluster.test"])


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    """A throwaway ``data/`` tree for one test."""
    directory = tmp_path / "data"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.fixture()
def app(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A configured (not yet started) coordinator app on ``data_dir``."""
    monkeypatch.setenv("PC_ROOT", str(ROOT))
    monkeypatch.setenv("PC_DATA_DIR", str(data_dir))
    from coordinator.app import create_app

    application = create_app(data_dir)
    cluster = application.state.cluster
    cluster.config.set("poll_timeout", 0.25)
    cluster.config.set("local_worker", False)
    cluster.config.set("discovery", False)  # never bind the real UDP port in tests
    cluster.config.set("notifications", dict(QUIET_NOTIFICATIONS))
    quiet_extras(cluster)
    cluster.port = 8765
    return application


@pytest_asyncio.fixture()
async def client(app: Any):
    """An ``httpx`` client bound to the app with its lifespan running."""
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://cluster.test", timeout=30.0
        ) as http:
            http.headers["X-Pin"] = app.state.cluster.config.pin
            yield http


@pytest_asyncio.fixture()
async def anon(app: Any):
    """The same app, but without the PIN header (for auth tests)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://cluster.test", timeout=30.0
    ) as http:
        yield http


@pytest.fixture()
def cluster(app: Any) -> Any:
    """The :class:`ClusterState` behind ``app`` / ``client``."""
    return app.state.cluster


@pytest.fixture()
def state(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """A bare :class:`ClusterState` with **no** background loops running.

    Every scheduler step is synchronous, so tests using this fixture can stay
    synchronous too and drive the scheduler one phase at a time.
    """
    monkeypatch.setenv("PC_ROOT", str(ROOT))
    monkeypatch.setenv("PC_DATA_DIR", str(data_dir))
    from coordinator.state import ClusterState

    cluster = ClusterState(data_dir)
    cluster.config.set("notifications", dict(QUIET_NOTIFICATIONS))
    cluster.config.set("poll_timeout", 0.25)
    quiet_extras(cluster)
    try:
        yield cluster
    finally:
        cluster.db.close()


# --------------------------------------------------------------------------
# a scriptable worker
# --------------------------------------------------------------------------

class FakeDevice:
    """A worker that speaks the SPEC §8 protocol over the ASGI client."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        pin: str,
        name: str = "phone",
        platform: str = "termux",
        caps: Optional[List[str]] = None,
        slots: int = 1,
        info: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.http = http
        self.pin = pin
        self.name = name
        self.platform = platform
        self.caps = list(caps or ["python", "shell", "service", "termux", "storage"])
        self.slots = slots
        self.info = dict(info or {"cpu_count": 4, "mem_mb": 1024, "os": "test"})
        self.device_id: Optional[str] = None
        self.token: Optional[str] = None
        self.config: Dict[str, Any] = {}
        self.store: Dict[str, bytes] = {}
        self.commands: List[Dict[str, Any]] = []
        self.terminals: List[str] = []
        #: values reported in every poll unless overridden per call
        self.battery: Optional[int] = 90
        self.charging: Optional[bool] = True
        self.temp: Optional[float] = None
        self.storage_free_mb: Optional[int] = 4096
        self.mem_free_mb: Optional[int] = None
        #: set to False to make the device reject chunks, None to ignore them
        self.accept_chunks: Optional[bool] = True

    # ------------------------------------------------------------ plumbing
    @property
    def headers(self) -> Dict[str, str]:
        return {"X-Device-Token": str(self.token)}

    async def register(self, **extra: Any) -> Dict[str, Any]:
        body = {
            "pin": self.pin,
            "name": self.name,
            "platform": self.platform,
            "caps": self.caps,
            "info": self.info,
            "slots": self.slots,
        }
        body.update(extra)
        response = await self.http.post("/api/w/register", json=body)
        response.raise_for_status()
        data = response.json()
        self.device_id = data["device_id"]
        self.token = data["token"]
        self.config = data.get("config") or {}
        return data

    async def poll(self, **status: Any) -> List[Dict[str, Any]]:
        block = {
            "battery": status.pop("battery", self.battery),
            "charging": status.pop("charging", self.charging),
            "temp": status.pop("temp", self.temp),
            "free_slots": status.pop("free_slots", self.slots),
            "running": status.pop("running", []),
            "storage_free_mb": status.pop("storage_free_mb", self.storage_free_mb),
            "mem_free_mb": status.pop("mem_free_mb", self.mem_free_mb),
        }
        response = await self.http.post(
            "/api/w/poll", json={"status": block}, headers=self.headers
        )
        response.raise_for_status()
        return list(response.json().get("commands") or [])

    async def start(self, task_id: str) -> httpx.Response:
        return await self.http.post(
            "/api/w/task/%s/start" % task_id, json={}, headers=self.headers
        )

    async def log(self, task_id: str, lines: List[str]) -> httpx.Response:
        return await self.http.post(
            "/api/w/task/%s/log" % task_id,
            json={"lines": lines, "ts": time.time()},
            headers=self.headers,
        )

    async def done(
        self,
        task_id: str,
        ok: bool = True,
        result: Any = None,
        outputs: Optional[List[Dict[str, Any]]] = None,
        error: Optional[str] = None,
        duration: float = 0.01,
        attempt: Optional[int] = None,
    ) -> httpx.Response:
        body: Dict[str, Any] = {
            "ok": ok,
            "result": result,
            "outputs": outputs or [],
            "duration": duration,
        }
        if error is not None:
            body["error"] = error
        if attempt is not None:
            body["attempt"] = attempt
        return await self.http.post(
            "/api/w/task/%s/done" % task_id, json=body, headers=self.headers
        )

    async def leave(self) -> httpx.Response:
        return await self.http.post("/api/w/leave", json={}, headers=self.headers)

    # ---------------------------------------------------------- execution
    async def run_task(self, command: Dict[str, Any], ctx: Optional[Any] = None) -> httpx.Response:
        """Execute a ``task`` command's python impl and report the result."""
        task = command["task"]
        await self.start(task["id"])
        impls = task["payload"].get("impls") or {}
        if "python" not in impls:
            return await self.done(task["id"], ok=False, error="no python impl")
        try:
            result = run_python_impl(impls["python"], ctx or SimpleCtx(task["id"], self.name))
        except Exception as exc:  # pragma: no cover - surfaced by the assertion
            return await self.done(task["id"], ok=False, error=str(exc))
        return await self.done(task["id"], ok=True, result=result)

    async def handle(self, command: Dict[str, Any]) -> None:
        """React to one coordinator command the way ``worker.py`` would."""
        self.commands.append(command)
        kind = command.get("cmd")
        if kind == "task":
            await self.run_task(command)
        elif kind == "store_chunk":
            if self.accept_chunks is not None:
                await self.store_chunk(command, ok=bool(self.accept_chunks))
        elif kind == "send_chunk":
            await self.send_chunk(command)
        elif kind == "del_chunk":
            self.store.pop(command.get("chunk_id", ""), None)
        elif kind == "scrub_chunks":
            await self.scrub(command)
        elif kind == "term_open":
            self.terminals.append(command["session_id"])
        elif kind == "term_input":
            await self.term_output(command["session_id"], command.get("data") or "")
        elif kind == "term_close":
            await self.term_closed(command["session_id"], 0)

    async def work(self, rounds: int = 6, **status: Any) -> int:
        """Poll a few times, handling every command that arrives."""
        handled = 0
        for _ in range(rounds):
            for command in await self.poll(**status):
                await self.handle(command)
                handled += 1
        return handled

    async def serve(self, stop: "asyncio.Event", **status: Any) -> None:
        """Poll in a loop until ``stop`` is set (run me as a task)."""
        while not stop.is_set():
            try:
                for command in await self.poll(**status):
                    await self.handle(command)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(0.05)

    def commands_of(self, kind: str) -> List[Dict[str, Any]]:
        return [c for c in self.commands if c.get("cmd") == kind]

    # ------------------------------------------------------------ storage
    async def store_chunk(self, command: Dict[str, Any], ok: bool = True) -> httpx.Response:
        chunk_id = command["chunk_id"]
        if ok:
            response = await self.http.get(
                "/api/w/chunk/%s" % chunk_id, headers=self.headers
            )
            if response.status_code == 200:
                self.store[chunk_id] = response.content
            else:
                ok = False
        return await self.http.post(
            "/api/w/chunk_ack",
            json={"chunk_id": chunk_id, "ok": bool(ok)},
            headers=self.headers,
        )

    async def scrub(self, command: Dict[str, Any]) -> httpx.Response:
        """Re-hash stored chunks like worker.py does; damaged ones are deleted."""
        import hashlib

        checked, bad, missing = [], [], []
        for chunk_id in command.get("chunk_ids") or []:
            data = self.store.get(chunk_id)
            if data is None:
                missing.append(chunk_id)
            elif hashlib.sha256(data).hexdigest() == chunk_id:
                checked.append(chunk_id)
            else:
                bad.append(chunk_id)
                self.store.pop(chunk_id, None)
        return await self.http.post(
            "/api/w/storage/scrub", json={"checked": checked, "bad": bad, "missing": missing},
            headers=self.headers,
        )

    async def send_chunk(self, command: Dict[str, Any]) -> Optional[httpx.Response]:
        chunk_id = command["chunk_id"]
        data = self.store.get(chunk_id)
        if data is None:
            return None
        return await self.http.put(
            "/api/w/chunk/%s" % chunk_id,
            params={"req_id": command.get("req_id", "")},
            content=data,
            headers=self.headers,
        )

    # ----------------------------------------------------------- terminal
    async def term_output(self, session_id: str, data: str) -> httpx.Response:
        return await self.http.post(
            "/api/w/term/%s/output" % session_id, json={"data": data}, headers=self.headers
        )

    async def term_closed(self, session_id: str, exit_code: int = 0) -> httpx.Response:
        return await self.http.post(
            "/api/w/term/%s/closed" % session_id,
            json={"exit_code": exit_code},
            headers=self.headers,
        )

    async def bench(self, score: float = 12.5) -> httpx.Response:
        return await self.http.post(
            "/api/w/bench",
            json={"score": score, "loop_iters": int(score * 1e6), "hash_mb_s": 100.0,
                  "kind": "python"},
            headers=self.headers,
        )


@contextlib.asynccontextmanager
async def running_device(fake: FakeDevice, **status: Any):
    """Run ``fake``'s poll loop for the duration of the ``async with`` block."""
    stop = asyncio.Event()
    task = asyncio.create_task(fake.serve(stop, **status))
    try:
        yield fake
    finally:
        stop.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


@pytest_asyncio.fixture()
async def device(client: httpx.AsyncClient, app: Any):
    """One registered :class:`FakeDevice`."""
    fake = FakeDevice(client, app.state.cluster.config.pin)
    await fake.register()
    return fake


@pytest.fixture()
def make_device(client: httpx.AsyncClient, app: Any) -> Callable[..., Any]:
    """Factory for extra :class:`FakeDevice` instances."""

    async def factory(**kwargs: Any) -> FakeDevice:
        fake = FakeDevice(client, app.state.cluster.config.pin, **kwargs)
        await fake.register()
        return fake

    return factory
