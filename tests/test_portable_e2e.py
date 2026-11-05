"""End-to-end with two real ``worker/worker.py`` processes on the phone path:
HTTPS with the cluster's own certificate and one-time join codes (SPEC 3),
WebAssembly jobs run by wasmtime (SPEC 6.9), and a big input travelling from
one worker to the other over HTTPS instead of from the coordinator (SPEC 8.3)."""

from __future__ import annotations

import os
import shutil
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import httpx
import pytest
import uvicorn

from conftest import ROOT, free_port

WORKER = ROOT / "worker" / "worker.py"
WASMTIME = ROOT / "tools" / "wasmtime" / ("wasmtime.exe" if sys.platform == "win32" else "wasmtime")
EXAMPLES = ROOT / "examples" / "wasm"
POLL_TIMEOUT = 2.0
JOB_TIMEOUT = 120.0
MB = 1024 * 1024


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory):
    data = tmp_path_factory.mktemp("portable-data")
    os.environ["PC_ROOT"] = str(ROOT)
    os.environ["PC_DATA_DIR"] = str(data)
    from coordinator.app import create_app

    app = create_app(data)
    cluster = app.state.cluster
    cluster.config.set("local_worker", False)
    cluster.config.set("poll_timeout", POLL_TIMEOUT)
    cluster.config.set("notifications", {"desktop": False, "phone": False, "webhook_url": None, "on": []})
    cluster.config.set("cache.enabled", False)
    cluster.config.set("verify.share", 0.0)
    cluster.config.set("transfers", {"peer": True, "min_mb": 1})
    port = free_port()
    cluster.port = port
    cluster.security.ensure_tls()
    tls = cluster.security.tls
    instance = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                             access_log=False, lifespan="on", timeout_graceful_shutdown=3,
                                             ssl_certfile=str(tls.server_pem),
                                             ssl_keyfile=str(tls.server_key)))
    thread = threading.Thread(target=instance.run, name="portable-uvicorn", daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not instance.started and time.time() < deadline:
        time.sleep(0.05)
    assert instance.started
    try:
        yield SimpleNamespace(url="https://127.0.0.1:%d" % port, pin=cluster.config.pin, cluster=cluster,
                              tls=tls)
    finally:
        instance.should_exit = True
        thread.join(timeout=20)


def cluster_context(server: Any) -> ssl.SSLContext:
    """Trust only the cluster's CA, like a worker does."""
    ctx = ssl.create_default_context(cafile=str(server.tls.ca_pem))
    ctx.check_hostname = False
    return ctx


@pytest.fixture(scope="module")
def api(server: Any):
    with httpx.Client(base_url=server.url, headers={"X-Pin": server.pin}, timeout=60.0,
                      verify=cluster_context(server)) as client:
        yield client


@pytest.fixture(scope="module")
def workers(server: Any, api: httpx.Client, tmp_path_factory: pytest.TempPathFactory):
    procs: List[Any] = []
    outputs: Dict[str, List[str]] = {}
    for name in ("alpha", "beta"):
        state_dir = tmp_path_factory.mktemp("portable-" + name)
        env = dict(os.environ, PC_ROOT=str(ROOT), PYTHONUNBUFFERED="1")
        env.pop("PC_PIN", None)
        code = server.cluster.security.codes.create("e2e")["code"]
        # alpha joins like the Termux one-liner (CA file), beta by hand (fingerprint)
        trust = (["--ca", str(server.tls.ca_pem)] if name == "alpha"
                 else ["--fp", server.tls.ca_fingerprint()])
        proc = subprocess.Popen(
            [sys.executable, str(WORKER), "--url", server.url, "--join", code] + trust +
            ["--state-dir", str(state_dir), "--name", name, "--slots", "2", "--verbose"],
            env=env, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace")
        lines: List[str] = []
        outputs[name] = lines
        threading.Thread(target=lambda p=proc, out=lines: [out.append(l) for l in p.stdout], daemon=True).start()
        procs.append(proc)
    devices: Dict[str, Any] = {}
    deadline = time.time() + 90
    while time.time() < deadline and len(devices) < 2:
        for device in api.get("/api/devices").json():
            if device.get("status") == "online":
                devices[device["name"]] = device
        time.sleep(0.25)
    if len(devices) < 2:
        for proc in procs:
            proc.terminate()
        pytest.fail("workers did not come online:\n%s" % "".join(sum(outputs.values(), [])[-40:]))
    try:
        yield SimpleNamespace(devices=devices, outputs=outputs)
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()


def upload(api: httpx.Client, name: str, data: bytes) -> str:
    reply = api.post("/api/files", files={"file": (name, data, "application/octet-stream")})
    reply.raise_for_status()
    return reply.json()["id"]


def wait_job(api: httpx.Client, job_id: str) -> Dict[str, Any]:
    deadline = time.time() + JOB_TIMEOUT
    while time.time() < deadline:
        job = api.get("/api/jobs/%s" % job_id).json()
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        time.sleep(0.25)
    raise AssertionError("job %s did not finish: %s" % (job_id, api.get("/api/jobs/%s" % job_id).json()))


needs_wasmtime = pytest.mark.skipif(not WASMTIME.is_file(), reason="needs tools/wasmtime")


@needs_wasmtime
def test_workers_offer_webassembly_and_peer_sending(workers: Any) -> None:
    for device in workers.devices.values():
        assert "wasm" in device["caps"] and "peer" in device["caps"]


@needs_wasmtime
def test_a_range_of_calls_is_shared_and_summed(api: httpx.Client, workers: Any) -> None:
    module = upload(api, "primes.wasm", (EXAMPLES / "primes.wasm").read_bytes())
    job = api.post("/api/jobs", json={"type": "wasm", "params": {
        "module": module, "call": "count_primes", "range": [2, 200000], "grain": 100}}).json()
    done = wait_job(api, job["id"])
    assert done["status"] == "done", done.get("error")
    assert done["result"]["sum"] == 17984  # primes below 200 000
    tasks = api.get("/api/jobs/%s/tasks" % job["id"]).json()
    assert all(t["impl"] == "wasm" for t in tasks if t["status"] == "done")


@needs_wasmtime
def test_a_program_per_input_file_with_outputs(api: httpx.Client, workers: Any) -> None:
    module = upload(api, "upper.wasm", (EXAMPLES / "upper.wasm").read_bytes())
    first = upload(api, "one.txt", b"first file\n")
    second = upload(api, "two.txt", b"and the second one\n")
    job = api.post("/api/jobs", json={"type": "wasm", "params": {"module": module,
                                                                 "inputs": [first, second]}}).json()
    done = wait_job(api, job["id"])
    assert done["status"] == "done", done.get("error")
    assert [r["input"] for r in done["result"]] == ["one.txt", "two.txt"]
    assert [r["result"] for r in done["result"]] == [{"bytes": 11}, {"bytes": 19}]
    texts = []
    for entry in done["result"]:
        (output,) = entry["outputs"]
        assert output["name"] == "upper.txt"
        texts.append(api.get("/api/files/%s/download" % output["file_id"]).content)
    assert texts == [b"FIRST FILE\n", b"AND THE SECOND ONE\n"]


@needs_wasmtime
def test_a_failing_program_reports_its_exit(api: httpx.Client, workers: Any) -> None:
    module = upload(api, "primes.wasm", (EXAMPLES / "primes.wasm").read_bytes())
    reply = api.post("/api/jobs", json={"type": "wasm", "params": {"module": module, "call": "missing",
                                                                   "args_list": [[1, 2]]}})
    job = reply.json()
    done = wait_job(api, job["id"])
    assert done["status"] == "failed" and "exports no function 'missing'" in done["error"]


@needs_wasmtime
def test_built_in_number_crunching_runs_as_webassembly(api: httpx.Client, workers: Any) -> None:
    """prime_range and monte_carlo_pi offer WebAssembly; workers with wasmtime use it."""
    job = api.post("/api/jobs", json={"type": "prime_range", "params": {"start": 2, "end": 3000000}}).json()
    done = wait_job(api, job["id"])
    assert done["status"] == "done", done.get("error")
    assert done["result"] == {"count": 216816}  # primes below three million
    tasks = [t for t in api.get("/api/jobs/%s/tasks" % job["id"]).json() if t["status"] == "done"]
    assert tasks and all(t["impl"] == "wasm" for t in tasks)
    job = api.post("/api/jobs", json={"type": "monte_carlo_pi", "params": {"samples": 4000000}}).json()
    done = wait_job(api, job["id"])
    assert done["status"] == "done", done.get("error")
    assert done["result"]["samples"] == 4000000 and abs(done["result"]["pi"] - 3.14159) < 0.01
    tasks = [t for t in api.get("/api/jobs/%s/tasks" % job["id"]).json() if t["status"] == "done"]
    assert tasks and all(t["impl"] == "wasm" for t in tasks)


def test_a_big_input_comes_from_the_worker_that_has_it(api: httpx.Client, server: Any, workers: Any) -> None:
    data = os.urandom(int(6.5 * MB))
    big = upload(api, "dataset.bin", data)
    code = ("def fn(item, ctx):\n"
            "    import hashlib\n"
            "    return hashlib.sha256(open(ctx.input(%r), 'rb').read()).hexdigest()\n" % big)
    results = []
    for name in ("alpha", "beta"):  # alpha downloads it first, beta then gets it from alpha
        job = api.post("/api/jobs", json={"type": "python_map", "params": {
            "code": code, "items": [1], "inputs": [big], "chunk_size": 1},
            "options": {"targets": [workers.devices[name]["id"]]}}).json()
        done = wait_job(api, job["id"])
        assert done["status"] == "done", done.get("error")
        results.append(done["result"][0])
    import hashlib

    assert results == [hashlib.sha256(data).hexdigest()] * 2
    stats = server.cluster.peers.stats()
    assert stats["fetches"] >= 1 and stats["peer_bytes"] > 0 and stats["open_offers"] == 0
    alpha_log = "".join(workers.outputs["alpha"])
    assert "sending a file to another device" in alpha_log  # its port opened for this ...
    deadline = time.time() + 10
    while "sending port closed" not in "".join(workers.outputs["alpha"]) and time.time() < deadline:
        time.sleep(0.2)
    assert "sending port closed" in "".join(workers.outputs["alpha"])  # ... and closed again


def test_workers_joined_with_one_time_codes(server: Any, workers: Any) -> None:
    rows = server.cluster.db.select("join_codes", "WHERE note='e2e'")
    assert len(rows) == 2 and all(r["used"] for r in rows)
    assert {r["device_id"] for r in rows} == {d["id"] for d in workers.devices.values()}


def test_a_worker_refuses_a_coordinator_with_the_wrong_certificate(server: Any, tmp_path: Path) -> None:
    code = server.cluster.security.codes.create("e2e-wrong")["code"]
    proc = subprocess.run(
        [sys.executable, str(WORKER), "--url", server.url, "--join", code, "--fp", "ab" * 32,
         "--state-dir", str(tmp_path / "w")],
        capture_output=True, text=True, timeout=60, env=dict(os.environ, PC_ROOT=str(ROOT)))
    assert proc.returncode == 2 and "does not match the fingerprint" in proc.stderr
    assert server.cluster.security.codes.valid(code)  # never even tried to join


def test_the_termux_one_liner_pins_the_coordinator_key(server: Any) -> None:
    curl = shutil.which("curl")
    if not curl:
        pytest.skip("no curl")
    code = server.cluster.security.codes.create("e2e-curl")["code"]
    url = "%s/install.sh?code=%s" % (server.url, code)
    good = subprocess.run([curl, "-fsSk", "--pinnedpubkey", server.tls.server_pin(), url],
                          capture_output=True, text=True, timeout=60)
    if good.returncode == 2 or "pinnedpubkey" in good.stderr.lower():
        pytest.skip("this curl cannot pin keys: %s" % good.stderr.strip())
    assert good.returncode == 0, good.stderr
    assert "--join \"%s\"" % code in good.stdout and "BEGIN CERTIFICATE" in good.stdout
    wrong = subprocess.run([curl, "-fsSk", "--pinnedpubkey",
                            "sha256//AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=", url],
                           capture_output=True, text=True, timeout=60)
    assert wrong.returncode != 0 and code not in wrong.stdout


def run_logged(command: Any, **kwargs: Any) -> SimpleNamespace:
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", **kwargs)
    lines: List[str] = []
    threading.Thread(target=lambda: [lines.append(l) for l in proc.stdout], daemon=True).start()
    return SimpleNamespace(proc=proc, lines=lines)


def wait_output(run: SimpleNamespace, text: str, seconds: float) -> None:
    deadline = time.time() + seconds
    while text not in "".join(run.lines) and time.time() < deadline and run.proc.poll() is None:
        time.sleep(0.25)
    time.sleep(0.5)
    assert text in "".join(run.lines), "".join(run.lines)[-4000:]


def stop_tree(run: SimpleNamespace) -> None:
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(run.proc.pid)], capture_output=True)
    run.proc.wait(timeout=30)


def test_a_worker_with_an_older_file_updates_itself(server: Any, tmp_path: Path) -> None:
    """SPEC 8.4: the PC offers its worker.py, the worker swaps it in and comes back."""
    import hashlib

    folder = tmp_path / "phone"
    folder.mkdir()
    mine = folder / "worker.py"
    original = WORKER.read_bytes()
    mine.write_bytes(original + b"\n# an older version\n")
    old = mine.read_bytes()
    code = server.cluster.security.codes.create("e2e-update")
    env = dict(os.environ)
    for name in ("PC_PIN", "PC_JOIN_CODE", "PC_STATE_DIR", "PC_URL", "PC_SUPERVISED", "PC_ROOT"):
        env.pop(name, None)
    run = run_logged([sys.executable, "-u", str(mine), "--url", server.url, "--join", code["code"],
                      "--ca", str(server.tls.ca_pem), "--state-dir", str(folder / "state"),
                      "--name", "updater", "--verbose"], env=env, cwd=str(folder))
    try:
        wait_output(run, "worker.py updated to", 90)
        wait_output(run, "starting the new worker.py", 30)
        (row,) = server.cluster.db.select("join_codes", "WHERE note='e2e-update'")
        deadline = time.time() + 60
        while time.time() < deadline and not server.cluster.db.select(
                "events", "WHERE kind='worker_updated' AND device_id=?", (row["device_id"],)):
            time.sleep(0.25)
        device = server.cluster.db.get("devices", row["device_id"])
        assert device["info"]["worker_sha256"] == hashlib.sha256(original).hexdigest(), "".join(run.lines)[-3000:]
        assert server.cluster.updates.view(device) == {"state": "current"}
    finally:
        if sys.platform == "win32":
            stop_tree(run)
        else:
            run.proc.terminate()
            run.proc.wait(timeout=30)
    assert mine.read_bytes() == original and (folder / "worker.py.bak").read_bytes() == old
    # it came back as the same device, without its used-up join code
    assert len(server.cluster.db.select("devices", "WHERE name='updater'")) == 1


@pytest.mark.skipif(sys.platform != "win32" or not (ROOT / "tools" / "python" / "python.exe").is_file(),
                    reason="needs Windows and the cluster's own Python (tools/python)")
def test_the_windows_one_liner_sets_up_a_worker_in_one_folder(server: Any, api: httpx.Client, tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    # the script names the address the laptop used; this test server listens on 127.0.0.1 only
    monkeypatch.setattr(server.cluster, "primary_url", lambda: server.url)
    code = server.cluster.security.codes.create("e2e-windows")
    command = server.cluster.security.join_info(server.url, code)["windows_cmd"]
    # as on a fresh Windows, where PowerShell runs no script files
    command = command.replace("-NoProfile", "-NoProfile -ExecutionPolicy Restricted", 1)
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ, USERPROFILE=str(home))
    for name in ("PC_PIN", "PC_JOIN_CODE", "PC_STATE_DIR", "PC_URL"):
        env.pop(name, None)
    folder = home / "phonecluster"
    run = run_logged(command, env=env, cwd=str(tmp_path))
    try:
        wait_output(run, "registered as", 120)
    finally:
        stop_tree(run)
    (row,) = server.cluster.db.select("join_codes", "WHERE note='e2e-windows'")
    assert row["used"] and row["device_id"]
    device = api.get("/api/devices/%s" % row["device_id"]).json()
    assert device["platform"] == "windows"
    assert "copying Python from the PC" in "".join(run.lines)
    # everything in one folder: the PC's Python without its libraries, the worker, its state
    assert sorted(p.name for p in folder.iterdir()) == [
        "cluster-ca.pem", "python", "start-worker.cmd", "state", "worker.py"]
    assert (folder / "python" / "python.exe").is_file() and not (folder / "python" / "Lib").exists()
    assert (folder / "state" / "device.json").is_file()
    assert (folder / "cluster-ca.pem").read_text().strip() == server.tls.ca_text().strip()
    launcher = (folder / "start-worker.cmd").read_text()
    assert '"python\\python.exe" -u -X utf8 worker.py --state-dir state %*' in launcher
    # start-worker.cmd brings the same device back, wherever it is started from
    again = run_logged(["cmd", "/c", str(folder / "start-worker.cmd")], env=env, cwd=str(tmp_path))
    try:
        wait_output(again, "(%s)" % row["device_id"], 60)
    finally:
        stop_tree(again)
    assert len(server.cluster.db.select("devices", "WHERE id=?", (row["device_id"],))) == 1
