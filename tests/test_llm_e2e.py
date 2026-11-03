"""Real distributed inference: coordinator + 3 worker processes + llama.cpp.

Skipped unless tools\\llama.cpp (scripts\\get-llama.ps1) and the small test
model (scripts\\get-model.ps1) are present.  Only reads the project's data\\models.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import psutil
import pytest
import uvicorn

from conftest import free_port

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "worker" / "worker.py"
MODELS = ROOT / "data" / "models"
MODEL = "qwen2.5-0.5b-instruct-q4_k_m.gguf"
LLAMA = ROOT / "tools" / "llama.cpp" / "win-x64" / "llama-server.exe"

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not LLAMA.is_file() or not (MODELS / MODEL).is_file(),
    reason="needs Windows, tools\\llama.cpp (get-llama.ps1) and the test model (get-model.ps1)",
)

sys.path.insert(0, str(ROOT / "sdk"))
from phonecluster import Cluster  # noqa: E402


def wait_until(check: Any, timeout: float = 60.0, interval: float = 0.5) -> Any:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = check()
        if last:
            return last
        time.sleep(interval)
    return last


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory):
    data = tmp_path_factory.mktemp("llm-e2e-data")
    saved = {key: os.environ.get(key) for key in ("PC_ROOT", "PC_DATA_DIR")}
    os.environ["PC_ROOT"] = str(ROOT)
    os.environ["PC_DATA_DIR"] = str(data)
    from coordinator.app import create_app

    app = create_app(data)
    cluster = app.state.cluster
    cluster.config.set("local_worker", False)
    cluster.config.set("discovery", False)
    cluster.config.set("poll_timeout", 1)
    cluster.config.set("offline_after", 6)
    cluster.config.set("notifications", {"desktop": False, "phone": False, "webhook_url": None, "on": []})
    cluster.config.set("llm", {"models_dir": str(MODELS)})
    port = free_port()
    cluster.port = port
    instance = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                             access_log=False, lifespan="on", timeout_graceful_shutdown=3))
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    assert wait_until(lambda: instance.started, 30)
    try:
        yield SimpleNamespace(url="http://127.0.0.1:%d" % port, pin=cluster.config.pin, cluster=cluster)
    finally:
        instance.should_exit = True
        thread.join(timeout=20)
        for key, value in saved.items():  # later tests must not inherit them
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class Workers(object):
    def __init__(self, server: Any, base: Path) -> None:
        self.server = server
        self.base = base
        self.procs: Dict[str, subprocess.Popen] = {}
        self.output: Dict[str, List[str]] = {}

    def start(self, name: str) -> None:
        env = dict(os.environ, PC_ROOT=str(ROOT), PYTHONUNBUFFERED="1")
        proc = subprocess.Popen(
            [sys.executable, str(WORKER), "--url", self.server.url, "--pin", self.server.pin,
             "--state-dir", str(self.base / name), "--name", name, "--slots", "2"],
            env=env, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace")
        lines: List[str] = self.output.setdefault(name, [])
        threading.Thread(target=lambda: [lines.append(l) for l in proc.stdout], daemon=True).start()
        self.procs[name] = proc

    def kill(self, name: str) -> None:
        proc = self.procs.pop(name)
        subprocess.call(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc.wait(timeout=20)

    def stop_all(self) -> None:
        for name in list(self.procs):
            self.kill(name)


@pytest.fixture(scope="module")
def workers(server: Any, tmp_path_factory: pytest.TempPathFactory):
    pool = Workers(server, tmp_path_factory.mktemp("llm-e2e-workers"))
    for name in ("llm-host", "llm-a", "llm-b"):
        pool.start(name)
    c = Cluster(server.url, pin=server.pin)
    ok = wait_until(lambda: sum(1 for d in c.llm.devices() if d["status"] == "online" and d["eligible"]) >= 3, 60)
    assert ok, "workers did not come online: %s" % {k: v[-10:] for k, v in pool.output.items()}
    try:
        yield pool
    finally:
        pool.stop_all()


def rpc_ports() -> List[int]:
    ports = []
    for proc in psutil.process_iter(["name"]):
        try:
            if proc.info["name"] and proc.info["name"].lower().startswith("ggml-rpc-server"):
                ports += [c.laddr.port for c in proc.net_connections(kind="tcp") if c.status == psutil.CONN_LISTEN]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return ports


def test_model_runs_split_across_chosen_devices(server: Any, workers: Workers) -> None:
    c = Cluster(server.url, pin=server.pin)
    plan = c.llm.plan(MODEL, ["llm-a", "llm-b"], host="llm-host", weights={"llm-a": 3, "llm-b": 1})
    assert plan["ok"], plan
    assert [d["name"] for d in plan["devices"]] == ["llm-b", "llm-a"]
    assert [d["layers"] for d in plan["devices"]] == [6, 18]

    dep = c.llm.deploy("e2e-qwen", MODEL, ["llm-a", "llm-b"], host="llm-host",
                       weights={"llm-a": 3, "llm-b": 1}, ctx=1024, wait=True, timeout=240)
    assert dep["status"] == "ready"
    endpoints = [s["endpoint"] for s in dep["shards"]]
    assert all(endpoints), dep
    ports = sorted(int(e.rsplit(":", 1)[1]) for e in endpoints)
    assert set(ports) <= set(rpc_ports())                 # two real rpc servers are listening
    assert dep["head"]["device_name"] == "llm-host"
    assert [s["name"] for s in c.services()] == ["e2e-qwen"]  # shards stay hidden

    tokens: List[str] = []
    answer = c.llm.chat("e2e-qwen", prompt="What is 2+2? Reply with only the number.",
                        max_tokens=8, temperature=0, on_token=tokens.append)
    assert "4" in answer["text"], answer
    assert tokens and "4" in "".join(tokens)             # streamed while generating
    assert answer["served_by"] == "llm-host"

    # a shard device dies: the deployment waits, then heals when it returns
    workers.kill("llm-b")
    degraded = wait_until(lambda: c.llm.deployment("e2e-qwen")["status"] == "degraded", 60)
    assert degraded, c.llm.deployment("e2e-qwen")
    workers.start("llm-b")
    healed = c.llm.wait_ready("e2e-qwen", timeout=180)
    assert healed["status"] == "ready"
    again = c.llm.chat("e2e-qwen", messages=[{"role": "user", "content": "Say the word yes."}],
                       max_tokens=6, temperature=0)
    assert again["text"].strip()

    # stop: no rpc server of this deployment keeps listening
    live = [int(s["endpoint"].rsplit(":", 1)[1]) for s in c.llm.deployment("e2e-qwen")["shards"]]
    c.llm.stop("e2e-qwen")
    assert wait_until(lambda: not (set(live) & set(rpc_ports())), 30), rpc_ports()
    assert [s for s in c.services() if s["instances"]] == []      # nothing runs; a call would load it again


def test_a_pc_shard_offers_its_cpu_and_gpu_as_two_parts(server: Any, workers: Workers) -> None:
    c = Cluster(server.url, pin=server.pin)
    devices = {d["name"]: d for d in c.llm.devices()}
    if not devices["llm-a"].get("gpu"):
        pytest.skip("no NVIDIA GPU with the GPU build (scripts\\get-llama.ps1 -Vulkan)")
    # a small GPU share: part of llm-a's layers on the card, the rest on its CPU
    c.device_controls("llm-a", gpu_pct=14)
    c.device_controls("llm-b", gpu_pct=0)
    try:
        plan = c.llm.plan(MODEL, ["llm-a", "llm-b"], host="llm-host", weights={"llm-a": 3, "llm-b": 1}, ctx=1024)
        assert plan["ok"], plan
        parts = {(p["device_id"], p["kind"]): p["layers"] for p in plan["rpc"]}
        a = devices["llm-a"]["id"]
        assert parts.get((a, "gpu")) and parts.get((a, "cpu")), plan["rpc"]
        assert plan["rpc"][-1]["kind"] == "gpu"                   # the card takes the output layer
        dep = c.llm.deploy("e2e-mixed", MODEL, ["llm-a", "llm-b"], host="llm-host",
                           weights={"llm-a": 3, "llm-b": 1}, ctx=1024, wait=True, timeout=240)
        assert dep["status"] == "ready"
        answer = c.llm.chat("e2e-mixed", prompt="What is 2+2? Reply with only the number.",
                            max_tokens=8, temperature=0)
        assert "4" in answer["text"], answer
    finally:
        c.device_controls("llm-a", gpu_pct=80)
        c.device_controls("llm-b", gpu_pct=80)
        try:
            c.llm.stop("e2e-mixed")
        except Exception:
            pass


def test_a_model_loads_when_called_and_unloads_when_unused(server: Any, workers: Workers) -> None:
    c = Cluster(server.url, pin=server.pin)
    name = "qwen2.5-0.5b-instruct-q4_k_m"
    for dep in c.llm.deployments():   # earlier tests left stopped deployments of this model behind
        if dep["model"] == MODEL and dep["status"] not in ("starting", "loading", "ready", "degraded"):
            c.llm.remove(dep["id"])
    assert any(s["name"] == name and s.get("on_demand") for s in c.services())
    answer = c.llm.chat(name, prompt="What is 2+2? Reply with only the number.", max_tokens=8, temperature=0)
    assert "4" in answer["text"], answer                      # loaded on the first call
    dep = c.llm.deployment(name)
    assert dep["status"] == "ready" and dep["options"]["on_demand"]
    c.update_settings({"llm": {"idle_minutes": 0.05}})       # 3 s
    try:
        assert wait_until(lambda: c.llm.deployment(name)["status"] == "idle", 60), c.llm.deployment(name)
        again = c.llm.chat(name, prompt="Say the word yes.", max_tokens=6, temperature=0)
        assert again["text"].strip()                         # and loaded again
    finally:
        c.update_settings({"llm": {"idle_minutes": 10}})
        c.llm.stop(name)


def gpu_servers() -> set:
    """pids of llama-server processes from the project's GPU build."""
    pids = set()
    for proc in psutil.process_iter(["name", "exe"]):
        try:
            if proc.info["name"] and proc.info["name"].lower().startswith("llama-server") \
                    and "win-vulkan-x64" in (proc.info["exe"] or ""):
                pids.add(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return pids


def test_automatic_placement_runs_a_small_model_alone_on_the_gpu(server: Any, workers: Workers) -> None:
    c = Cluster(server.url, pin=server.pin)
    if not any(d.get("gpu_budget_mb") for d in c.llm.devices()):
        pytest.skip("no NVIDIA GPU with the GPU build (scripts\\get-llama.ps1 -Vulkan)")
    plan = c.llm.plan(MODEL, ctx=1024)
    assert plan["ok"] and plan["mode"] == "auto" and plan["placement"] == "single", plan
    assert plan["gpu_layers"] == 24 and plan["gpu_full"]
    before = gpu_servers()  # e.g. a coordinator running next to the tests
    dep = c.llm.deploy("e2e-gpu", MODEL, ctx=1024, wait=True, timeout=240)
    assert dep["status"] == "ready" and dep["shards"] == []
    ours = gpu_servers() - before
    assert ours                                                    # the project's GPU build, nothing else
    answer = c.llm.chat("e2e-gpu", prompt="What is 2+2? Reply with only the number.", max_tokens=8, temperature=0)
    assert "4" in answer["text"], answer
    # the user takes the GPU away (all test workers share this PC's card): a
    # restart places the model on a CPU
    ids = [d["id"] for d in c.llm.devices() if d.get("gpu")]
    for device_id in ids:
        c.device_controls(device_id, gpu_pct=0)
    try:
        c.llm.restart("e2e-gpu")
        dep = c.llm.wait_ready("e2e-gpu", timeout=240)
        assert not dep["options"].get("gpu_layers")
        again = c.llm.chat("e2e-gpu", prompt="What is 2+2? Reply with only the number.", max_tokens=8, temperature=0)
        assert "4" in again["text"], again
    finally:
        for device_id in ids:
            c.device_controls(device_id, gpu_pct=80)
        c.llm.stop("e2e-gpu")
    assert wait_until(lambda: not (ours & gpu_servers()), 30)
