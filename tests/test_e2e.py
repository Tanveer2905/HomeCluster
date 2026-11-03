"""End-to-end: a real uvicorn server plus the real ``worker/worker.py``.

Starts the coordinator on a free port in a thread (temp data dir, no local
worker) and launches ``worker/worker.py`` as a subprocess, then drives the whole
cluster through the public API: python_map, shell, hash_search, benchmark,
cancel, pooled storage, the remote terminal and job export.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import httpx
import pytest
import uvicorn
import websockets.sync.client as ws_client

from conftest import ROOT, free_port

WORKER = ROOT / "worker" / "worker.py"
BOOT_TIMEOUT = 90.0
JOB_TIMEOUT = 120.0
POLL_TIMEOUT = 3.0

pytestmark = pytest.mark.skipif(not WORKER.is_file(), reason="worker/worker.py is missing")


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory):
    """A real uvicorn server on 127.0.0.1:<free port>."""
    data = tmp_path_factory.mktemp("e2e-data")
    os.environ["PC_ROOT"] = str(ROOT)
    os.environ["PC_DATA_DIR"] = str(data)
    from coordinator.app import create_app

    app = create_app(data)
    cluster = app.state.cluster
    cluster.config.set("local_worker", False)
    cluster.config.set("poll_timeout", POLL_TIMEOUT)
    cluster.config.set("offline_after", 30)
    cluster.config.set(
        "notifications", {"desktop": False, "phone": False, "webhook_url": None, "on": []}
    )
    cluster.config.set(
        "storage", {"chunk_mb": 4, "replication": 1, "cache_mb": 512, "include_pc": True}
    )
    port = free_port()
    cluster.port = port
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        lifespan="on",
        timeout_graceful_shutdown=3,
    )
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, name="e2e-uvicorn", daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not instance.started and time.time() < deadline:
        time.sleep(0.05)
    assert instance.started, "uvicorn did not start"

    handle = SimpleNamespace(
        url="http://127.0.0.1:%d" % port,
        port=port,
        pin=cluster.config.pin,
        cluster=cluster,
        data=data,
    )
    try:
        yield handle
    finally:
        instance.should_exit = True
        thread.join(timeout=20)


@pytest.fixture(scope="module")
def api(server: Any):
    with httpx.Client(
        base_url=server.url, headers={"X-Pin": server.pin}, timeout=60.0
    ) as client:
        yield client


@pytest.fixture(scope="module")
def worker(server: Any, api: httpx.Client, tmp_path_factory: pytest.TempPathFactory):
    """The real single-file worker, attached to ``server``."""
    state_dir = tmp_path_factory.mktemp("e2e-worker")
    env = dict(os.environ)
    env["PC_ROOT"] = str(ROOT)
    env["PYTHONUNBUFFERED"] = "1"
    command = [
        sys.executable,
        str(WORKER),
        "--url",
        server.url,
        "--pin",
        server.pin,
        "--state-dir",
        str(state_dir),
        "--name",
        "e2e-worker",
        "--slots",
        "2",
        "--verbose",
    ]
    proc = subprocess.Popen(
        command,
        env=env,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output: List[str] = []
    reader = threading.Thread(
        target=lambda: [output.append(line) for line in proc.stdout], daemon=True
    )
    reader.start()

    device: Optional[Dict[str, Any]] = None
    deadline = time.time() + BOOT_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail("worker exited early:\n%s" % "".join(output[-40:]))
        try:
            devices = api.get("/api/devices").json()
        except httpx.HTTPError:
            devices = []
        online = [d for d in devices if d.get("status") == "online"]
        if online:
            device = online[0]
            break
        time.sleep(0.25)
    if device is None:
        proc.terminate()
        pytest.fail("the worker never came online:\n%s" % "".join(output[-40:]))

    handle = SimpleNamespace(proc=proc, device=device, output=output, state_dir=state_dir)
    try:
        yield handle
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=20)
        except Exception:
            proc.kill()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def submit(api: httpx.Client, **spec: Any) -> Dict[str, Any]:
    response = api.post("/api/jobs", json=spec)
    assert response.status_code == 200, response.text
    return response.json()


def wait_job(api: httpx.Client, job_id: str, timeout: float = JOB_TIMEOUT) -> Dict[str, Any]:
    deadline = time.time() + timeout
    job: Dict[str, Any] = {}
    while time.time() < deadline:
        job = api.get("/api/jobs/%s" % job_id).json()
        if job.get("status") in ("done", "failed", "cancelled"):
            return job
        time.sleep(0.2)
    raise AssertionError("job %s stuck in %r" % (job_id, job.get("status")))


def wait_until(check: Any, timeout: float = 30.0, interval: float = 0.2) -> Any:
    deadline = time.time() + timeout
    value = None
    while time.time() < deadline:
        value = check()
        if value:
            return value
        time.sleep(interval)
    return value


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------

def test_worker_registers_with_the_expected_capabilities(worker: Any, api: httpx.Client) -> None:
    device = api.get("/api/devices/%s" % worker.device["id"]).json()
    assert device["name"] == "e2e-worker"
    assert device["platform"] == "windows" if os.name == "nt" else True
    assert "python" in device["caps"]
    assert "shell" in device["caps"]
    assert device["slots"] == 2
    assert device["status"] == "online"
    assert device["info"]["cpu_count"] >= 1


def test_python_map_returns_squares_in_order(worker: Any, api: httpx.Client) -> None:
    job = submit(
        api,
        type="python_map",
        name="squares",
        params={
            "code": "def fn(item, ctx):\n    ctx.log('squaring', item)\n    return item * item\n",
            "items": list(range(12)),
            "chunk_size": 3,
        },
        options={"timeout": 120},
    )
    done = wait_job(api, job["id"])
    assert done["status"] == "done", done.get("error")
    assert done["result"] == [i * i for i in range(12)]
    assert done["total_tasks"] == 4

    tasks = api.get("/api/jobs/%s/tasks" % job["id"]).json()
    assert all(t["status"] == "done" for t in tasks)
    assert all(t["device_id"] == worker.device["id"] for t in tasks)
    assert all(t["duration"] is not None for t in tasks)
    log = api.get("/api/tasks/%s/log" % tasks[0]["id"]).json()["lines"]
    assert any("squaring" in line for line in log)


def test_shell_job_returns_stdout(worker: Any, api: httpx.Client) -> None:
    job = submit(
        api,
        type="shell",
        name="hello",
        params={"cmd": "echo hi", "cmd_windows": "echo hi", "timeout": 60},
        options={"targets": "any", "timeout": 60},
    )
    done = wait_job(api, job["id"])
    assert done["status"] == "done", done.get("error")
    result = done["result"][worker.device["id"]]
    assert result["exit_code"] == 0
    assert "hi" in result["stdout"]


def test_hash_search_finds_a_match(worker: Any, api: httpx.Client) -> None:
    import hashlib

    job = submit(
        api,
        type="hash_search",
        name="prefix hunt",
        params={"algo": "sha256", "prefix": "00", "start": 0, "count": 4000,
                "chunk_size": 1000, "salt": "", "max_results": 3},
        options={"timeout": 120},
    )
    done = wait_job(api, job["id"])
    assert done["status"] == "done", done.get("error")
    assert done["result"]["searched"] > 0
    matches = done["result"]["matches"]
    assert matches, "no sha256 in 0..3999 started with '00'?"
    for match in matches:
        assert hashlib.sha256(str(match["n"]).encode()).hexdigest() == match["hash"]
        assert match["hash"].startswith("00")


def test_benchmark_sets_the_bench_score(worker: Any, api: httpx.Client) -> None:
    job = submit(api, type="benchmark", name="bench", params={},
                 options={"targets": "all", "timeout": 120})
    done = wait_job(api, job["id"])
    assert done["status"] == "done", done.get("error")
    result = done["result"][worker.device["id"]]
    assert result["kind"] == "python"
    assert result["score"] > 0
    assert result["loop_iters"] > 0
    assert result["hash_mb_s"] > 0

    device = api.get("/api/devices/%s" % worker.device["id"]).json()
    assert device["bench_score"] == pytest.approx(result["score"])
    leaderboard = api.get("/api/stats").json()["leaderboard"]
    assert leaderboard[0]["bench_score"] == pytest.approx(result["score"])


def test_cancelling_a_long_shell_job(worker: Any, api: httpx.Client) -> None:
    long_running = "ping -n 30 127.0.0.1" if os.name == "nt" else "sleep 30"
    job = submit(
        api,
        type="shell",
        name="long ping",
        params={"cmd": "sleep 30", "cmd_windows": long_running, "timeout": 120},
        options={"targets": "any", "timeout": 120},
    )
    started = wait_until(
        lambda: [
            t for t in api.get("/api/jobs/%s/tasks" % job["id"]).json()
            if t["status"] in ("assigned", "running")
        ],
        timeout=30,
    )
    assert started, "the ping task never started"

    assert api.post("/api/jobs/%s/cancel" % job["id"]).status_code == 200
    done = wait_job(api, job["id"], timeout=60)
    assert done["status"] == "cancelled"
    tasks = api.get("/api/jobs/%s/tasks" % job["id"]).json()
    assert all(t["status"] == "cancelled" for t in tasks)


def test_pooled_storage_round_trip(worker: Any, api: httpx.Client, server: Any) -> None:
    """10 MB through the worker's chunk store (SPEC §10).

    ``include_pc`` is on for this cluster, so the Windows worker (which reports
    the ``storage`` capability) is an eligible chunk holder.
    """
    payload = os.urandom(10 * 1024 * 1024)
    response = api.post(
        "/api/storage",
        files={"file": ("big.bin", payload, "application/octet-stream")},
        data={"replication": "1"},
    )
    assert response.status_code == 200, response.text
    blob_id = response.json()["id"]

    status = wait_until(
        lambda: (lambda s: s if s["status"] == "ready" else None)(
            api.get("/api/storage/%s/status" % blob_id).json()
        ),
        timeout=90,
    )
    assert status and status["status"] == "ready", "blob never became ready"
    assert status["n_chunks"] == 3
    assert all(c["locations"][0]["device_id"] == worker.device["id"] for c in status["chunks"])

    # the chunks must come back from the worker, not from the coordinator cache
    store_dir = Path(worker.state_dir) / "store"
    assert len(list(store_dir.glob("*"))) == 3
    for path in server.cluster.storage.cache_dir.glob("*"):
        path.unlink()

    download = api.get("/api/storage/%s" % blob_id)
    assert download.status_code == 200
    assert download.content == payload

    assert api.delete("/api/storage/%s" % blob_id).status_code == 200
    assert wait_until(lambda: not list(store_dir.glob("*")), timeout=30) is not False


def test_remote_terminal_echoes_over_the_websocket(
    worker: Any, api: httpx.Client, server: Any
) -> None:
    url = "ws://127.0.0.1:%d/ws/dashboard?pin=%s" % (server.port, server.pin)
    with ws_client.connect(url, open_timeout=20) as socket:
        first = json.loads(socket.recv(timeout=20))
        assert first["ev"] == "snapshot"

        opened = api.post("/api/terminal", json={"device_id": worker.device["id"]})
        assert opened.status_code == 200, opened.text
        session_id = opened.json()["session_id"]

        time.sleep(1.0)  # let the worker start its shell
        socket.send(json.dumps({
            "op": "term_input", "session_id": session_id, "data": "echo hello\r\n"
        }))

        seen = ""
        deadline = time.time() + 45
        while time.time() < deadline and "hello" not in seen:
            try:
                message = json.loads(socket.recv(timeout=5))
            except TimeoutError:
                continue
            if message.get("ev") == "term" and message.get("session_id") == session_id:
                seen += message.get("data") or ""
        assert "hello" in seen, "terminal never echoed (got %r)" % seen[-400:]

    assert api.delete("/api/terminal/%s" % session_id).status_code == 200


def test_export_zip_contains_result_json(worker: Any, api: httpx.Client) -> None:
    import io
    import zipfile

    job = submit(
        api,
        type="python_map",
        name="export me",
        params={"code": "def fn(item, ctx):\n    return item + 1\n",
                "items": [1, 2, 3], "chunk_size": 2},
        options={"timeout": 120},
    )
    done = wait_job(api, job["id"])
    assert done["status"] == "done", done.get("error")

    export = api.get("/api/jobs/%s/export" % job["id"], params={"format": "zip"})
    assert export.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(export.content))
    assert "result.json" in archive.namelist()
    assert "tasks.csv" in archive.namelist()
    assert json.loads(archive.read("result.json"))["result"] == [2, 3, 4]


TRANSCRIBER = '''
import json

def load(ctx):
    ctx.log("fake transcriber loaded")
    return {"words": ["hello", "cluster"]}

def serve(request, ctx, model):
    total = 0
    for chunk in request.stream():
        total += len(chunk)
        ctx.emit({"heard": total})
    ctx.output_bytes(b"RIFF fake wav", "reply.wav")
    return {"text": " ".join(model["words"]), "bytes": total, "lang": request.params.get("lang")}
'''


def test_service_end_to_end_through_the_sdk(worker: Any, api: httpx.Client, server: Any, tmp_path: Any) -> None:
    """Start a service on the real worker, call it from the SDK with a streamed
    file and live partials, download its binary output, stop it."""
    import sys as _sys

    _sys.path.insert(0, str(ROOT / "sdk"))
    from phonecluster import Cluster

    c = Cluster(server.url, pin=server.pin)
    job = c.start_service("fake-stt", TRANSCRIBER, description="test transcriber",
                          accepts="audio/wav", produces="text", streaming_output=True,
                          request_timeout=60)
    ready = wait_until(lambda: any(s["name"] == "fake-stt" and s["ready"] == 1 for s in c.services()),
                       timeout=60)
    assert ready, "service never became ready"
    svc = [s for s in c.services() if s["name"] == "fake-stt"][0]
    assert svc["instances"][0]["device_name"] == worker.device["name"]

    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"\x01" * 300000)
    partials = []
    result = c.call("fake-stt", path=str(audio), params={"lang": "en"}, on_partial=partials.append)
    assert result["status"] == "done", result
    assert result["result"] == {"text": "hello cluster", "bytes": 300000, "lang": "en"}
    assert result["partials"][-1] == {"heard": 300000} and len(result["partials"]) >= 2
    assert partials and partials[-1] == {"heard": 300000}  # seen live on the second connection
    assert [o["name"] for o in result["outputs"]] == ["reply.wav"]
    dest = tmp_path / "reply.wav"
    c.files.download(result["outputs"][0]["file_id"], str(dest))
    assert dest.read_bytes() == b"RIFF fake wav"

    # second call reuses the loaded model (no reload in the task log)
    again = c.call("fake-stt", text="tiny")
    assert again["result"]["bytes"] == 4
    task_id = svc["instances"][0]["task_id"]

    def loaded_lines() -> Any:
        lines = api.get("/api/tasks/%s/log" % task_id).json()["lines"]
        return [l for l in lines if "fake transcriber loaded" in l]

    # log lines are batched by the worker (500 ms), so give them a moment
    assert wait_until(loaded_lines, timeout=10), "model load never logged"
    assert len(loaded_lines()) == 1  # loaded once, reused for both calls

    assert c.stop_service("fake-stt") == [job.id]
    gone = wait_until(lambda: not any(s["name"] == "fake-stt" for s in c.services()), timeout=30)
    assert gone
    assert wait_job(api, job.id)["status"] == "cancelled"


def test_worker_leaves_cleanly(worker: Any, api: httpx.Client) -> None:
    """Terminating the subprocess makes the coordinator notice it is gone."""
    worker.proc.terminate()
    worker.proc.wait(timeout=30)
    gone = wait_until(
        lambda: api.get("/api/devices/%s" % worker.device["id"]).json()["status"] == "offline",
        timeout=60,
    )
    assert gone, "the device never went offline"
