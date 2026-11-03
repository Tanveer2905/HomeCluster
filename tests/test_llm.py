"""Distributed LLMs: models, planning, deployments (coordinator/llm.py, api_llm.py, gguf.py)."""

from __future__ import annotations

import asyncio
import http.server
import struct
import threading
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest

from conftest import FakeDevice, wait_for
from coordinator.gguf import summarize
from coordinator.llm import LLMManager

pytestmark = pytest.mark.asyncio

MB = 1024 * 1024


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _gguf_string(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


def write_gguf(path: Path, arch: str = "llama", layers: int = 4, embd: int = 64, heads: int = 4,
               heads_kv: int = 2, vocab: int = 100, pad_mb: float = 4, name: str = "tiny",
               tensors: Dict[str, int] = None, ff: int = 0) -> Path:
    """A minimal valid GGUF v3 header followed by padding standing in for weights.

    ``tensors`` ({name: bytes}) adds a tensor directory whose data follows
    the header back to back (then the padding is not written)."""
    kvs = [
        ("general.architecture", 8, _gguf_string(arch)),
        ("general.name", 8, _gguf_string(name)),
        ("%s.block_count" % arch, 4, struct.pack("<I", layers)),
        ("%s.embedding_length" % arch, 4, struct.pack("<I", embd)),
        ("%s.attention.head_count" % arch, 4, struct.pack("<I", heads)),
        ("%s.attention.head_count_kv" % arch, 4, struct.pack("<I", heads_kv)),
        ("%s.context_length" % arch, 4, struct.pack("<I", 4096)),
    ]
    if ff:
        kvs.append(("%s.feed_forward_length" % arch, 4, struct.pack("<I", ff)))
    tokens = struct.pack("<IQ", 8, vocab) + b"".join(_gguf_string("t%d" % i) for i in range(vocab))
    kvs.append(("tokenizer.ggml.tokens", 9, tokens))
    tensors = tensors or {}
    body = b"GGUF" + struct.pack("<IQQ", 3, len(tensors), len(kvs))
    for key, kind, value in kvs:
        body += _gguf_string(key) + struct.pack("<I", kind) + value
    offset = 0
    for tname, size in tensors.items():
        body += _gguf_string(tname) + struct.pack("<I", 1) + struct.pack("<Q", size) \
            + struct.pack("<I", 0) + struct.pack("<Q", offset)
        offset += size
    path.parent.mkdir(parents=True, exist_ok=True)
    if tensors:
        body += b"\0" * ((32 - len(body) % 32) % 32)  # data starts aligned
        path.write_bytes(body + b"\0" * offset)
    else:
        path.write_bytes(body + b"\0" * int(pad_mb * MB))
    return path


def fake_tools(tmp: Path, tag: str = "b9", android: bool = True) -> Path:
    tools = tmp / "tools-llama"
    tools.mkdir(parents=True, exist_ok=True)
    (tools / "VERSION").write_text(tag + "\n")
    (tools / ("llama-%s-bin-win-cpu-x64.zip" % tag)).write_bytes(b"PK zip " * 10)
    if android:
        (tools / ("llama-%s-bin-android-arm64.tar.gz" % tag)).write_bytes(b"tar " * 10)
    return tools


@pytest.fixture()
def tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = fake_tools(tmp_path)
    monkeypatch.setattr(LLMManager, "tools_dir", property(lambda self: path))
    return path


@pytest.fixture()
def model(cluster: Any) -> Path:
    return write_gguf(Path(cluster.data) / "models" / "tiny.gguf")


async def phone(make_device: Any, name: str, mem_mb: int, free_mb: int, api: int = 30,
                abi: str = "arm64-v8a", platform: str = "termux", **extra: Any) -> FakeDevice:
    info = {"cpu_count": 8, "mem_mb": mem_mb, "os": "test", "abi": abi,
            "android_api": api if platform == "termux" else None}
    dev = await make_device(name=name, platform=platform, info=info, **extra)
    dev.mem_free_mb = free_mb
    await dev.poll()
    return dev


async def commands(dev: FakeDevice, kind: str, tries: int = 20) -> List[Dict[str, Any]]:
    for _ in range(tries):
        found = [c for c in await dev.poll() if c.get("cmd") == kind]
        if found:
            return found
    return []


# --------------------------------------------------------------------------
# GGUF header reader
# --------------------------------------------------------------------------

async def test_gguf_summary(tmp_path: Path) -> None:
    path = write_gguf(tmp_path / "m.gguf", layers=4, embd=64, heads=4, heads_kv=2, vocab=100)
    info = summarize(path)
    assert info["architecture"] == "llama" and info["name"] == "tiny"
    assert info["layers"] == 4 and info["vocab"] == 100 and info["context_length"] == 4096
    # 4 layers x 2 kv heads x (16 + 16) dims x 2 bytes
    assert info["kv_bytes_per_token"] == 4 * 2 * 32 * 2
    # compute buffer: one micro-batch of the feed-forward (4 x embd when unknown)
    assert info["compute_bytes_base"] == 512 * 4 * 64 * 16
    assert info["layer_bytes"] is None and info["embd_bytes"] is None  # no tensor directory
    (tmp_path / "bad.gguf").write_bytes(b"nope")
    assert "error" in summarize(tmp_path / "bad.gguf")


async def test_gguf_tensor_sizes(tmp_path: Path) -> None:
    """Sizes come from the gaps between tensor offsets; tied embeddings are
    counted again for the output copy on the last device."""
    sizes = {"token_embd.weight": 4000, "blk.0.attn_q.weight": 1000, "blk.0.ffn_up.weight": 3000,
             "blk.1.attn_q.weight": 1200, "blk.1.ffn_up.weight": 2800, "output_norm.weight": 64}
    path = write_gguf(tmp_path / "t.gguf", layers=2, tensors=sizes, ff=256)
    info = summarize(path)
    assert info["embd_bytes"] == 4000
    assert info["output_bytes"] == 4000          # tied: no output.weight
    assert info["layer_bytes"] == 4000           # (1000+3000+1200+2800) / 2
    assert info["feed_forward"] == 256
    assert info["compute_bytes_base"] == 512 * 256 * 16
    sizes["output.weight"] = 5000
    info = summarize(write_gguf(tmp_path / "u.gguf", layers=2, tensors=sizes))
    assert info["output_bytes"] == 5000


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------

async def test_models_list_upload_and_delete(client: httpx.AsyncClient, cluster: Any, model: Path) -> None:
    listing = (await client.get("/api/llm/models")).json()
    assert [m["name"] for m in listing] == ["tiny.gguf"]
    assert listing[0]["layers"] == 4 and listing[0]["title"] == "tiny" and listing[0]["used_by"] == []

    data = model.read_bytes()[:200000]
    ok = await client.put("/api/llm/models", params={"name": "up.gguf"}, content=data)
    assert ok.status_code == 200, ok.text and ok.json()["name"] == "up.gguf"
    assert (Path(cluster.data) / "models" / "up.gguf").read_bytes() == data
    assert (await client.put("/api/llm/models", params={"name": "up.gguf"}, content=data)).status_code == 400
    bad = await client.put("/api/llm/models", params={"name": "x.gguf"}, content=b"not gguf")
    assert bad.status_code == 400 and "GGUF" in bad.json()["error"]
    assert not (Path(cluster.data) / "models" / "x.gguf.part").exists()
    for name in ("../evil.gguf", "model.bin"):
        assert (await client.put("/api/llm/models", params={"name": name}, content=data)).status_code == 400

    assert (await client.delete("/api/llm/models/up.gguf")).status_code == 200
    assert (await client.delete("/api/llm/models/up.gguf")).status_code == 404


async def test_fetch_model_from_a_url(client: httpx.AsyncClient, cluster: Any, tmp_path: Path) -> None:
    served = write_gguf(tmp_path / "srv" / "remote.gguf", pad_mb=1)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a: Any, **k: Any) -> None:
            super().__init__(*a, directory=str(served.parent), **k)

        def log_message(self, *a: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = "http://127.0.0.1:%d/remote.gguf" % server.server_address[1]
        started = await client.post("/api/llm/models/fetch", json={"url": url})
        assert started.status_code == 200 and started.json()["name"] == "remote.gguf"

        async def done() -> bool:
            return any(m["name"] == "remote.gguf" and not m.get("status")
                       for m in (await client.get("/api/llm/models")).json())

        assert await wait_for(done, timeout=20)
        assert (Path(cluster.data) / "models" / "remote.gguf").read_bytes() == served.read_bytes()
        again = await client.post("/api/llm/models/fetch", json={"url": url})
        assert again.status_code == 400  # already there
        assert (await client.post("/api/llm/models/fetch", json={"url": "ftp://x/y.gguf"})).status_code == 400
    finally:
        server.shutdown()


# --------------------------------------------------------------------------
# devices and planning
# --------------------------------------------------------------------------

async def test_device_eligibility(client: httpx.AsyncClient, make_device: Any, tools: Path) -> None:
    await phone(make_device, "note10", 4096, 3000, api=30)
    await phone(make_device, "redmi5a", 2048, 1200, api=25)
    await phone(make_device, "j2", 1024, 400, platform="browser", caps=["js", "browser"])
    await phone(make_device, "oldworker", 2048, 1000, caps=["python", "shell", "termux"])
    listing = {d["name"]: d for d in (await client.get("/api/llm/devices")).json()}
    assert listing["note10"]["eligible"] and listing["note10"]["llama_build"] == "android-arm64"
    assert listing["redmi5a"]["eligible"] and listing["redmi5a"]["llama_build"] == "source"
    assert not listing["j2"]["eligible"] and "browser" in listing["j2"]["reason"]
    assert not listing["oldworker"]["eligible"] and "service" in listing["oldworker"]["reason"]
    # usable = free - max(256, 10% of RAM), and at most 70% of free (Balanced)
    assert listing["note10"]["usable_mb"] == min(3000 - 409, 2100)
    assert listing["redmi5a"]["usable_mb"] == min(1200 - 256, 840)
    # a device the user switched AI off for stays connected but is not offered
    await client.patch("/api/devices/%s" % listing["note10"]["id"], json={"settings": {"roles": {"ai": False}}})
    listing = {d["name"]: d for d in (await client.get("/api/llm/devices")).json()}
    assert not listing["note10"]["eligible"] and "switched off" in listing["note10"]["reason"]
    assert "_device" not in listing["note10"]  # no raw rows (token hashes) in the API
    await client.patch("/api/devices/%s" % listing["note10"]["id"], json={"settings": {"roles": {"ai": True}}})
    # without the Android build on the coordinator, prebuilt phones cannot join
    (tools / "llama-b9-bin-android-arm64.tar.gz").unlink()
    listing = {d["name"]: d for d in (await client.get("/api/llm/devices")).json()}
    assert not listing["note10"]["eligible"] and "get-llama" in listing["note10"]["reason"]


async def test_manual_plan_splits_by_memory_largest_last(client: httpx.AsyncClient, make_device: Any,
                                                         tools: Path, model: Path) -> None:
    big = await phone(make_device, "big", 8192, 3000)
    small = await phone(make_device, "small", 2048, 1256)
    body = {"model": "tiny.gguf", "devices": [big.device_id, small.device_id], "ctx": 2048,
            "host": big.device_id}
    plan = (await client.post("/api/llm/plan", json=body)).json()
    assert plan["ok"] and not plan["errors"] and plan["mode"] == "manual"
    assert plan["placement"] == "split"
    names = [d["name"] for d in plan["devices"]]
    assert names == ["small", "big"]                       # ascending: the biggest takes the output layer
    usable = {"small": min(1256 - 256, int(1256 * 0.7)), "big": min(3000 - 819, 3000 * 0.7)}
    assert [d["weight"] for d in plan["devices"]] == [usable["small"], usable["big"]]
    assert plan["tensor_split"] == [1, 3]                  # layers, in rpc order
    assert sum(d["layers"] for d in plan["devices"]) == 4
    assert plan["host"] == big.device_id
    assert plan["devices"][-1]["need_mb"] > plan["devices"][0]["need_mb"]

    # manual weights, host outside the shards
    host = await phone(make_device, "pc", 16384, 8000, platform="windows", abi="x86_64")
    body.update({"weights": {small.device_id: 3, big.device_id: 1}, "host": host.device_id})
    plan = (await client.post("/api/llm/plan", json=body)).json()
    assert [d["name"] for d in plan["devices"]] == ["big", "small"]
    assert [d["share"] for d in plan["devices"]] == [0.25, 0.75]
    assert all(d["role"] == "shard" for d in plan["devices"]) and plan["host_name"] == "pc"

    # one device that is also the host runs the model alone, no rpc
    plan = (await client.post("/api/llm/plan", json={
        "model": "tiny.gguf", "devices": [small.device_id], "ctx": 2048, "mode": "manual"})).json()
    assert plan["ok"] and plan["placement"] == "single" and plan["rpc"] == []
    # too big once the phone's free memory drops
    small.mem_free_mb = 258
    await small.poll()
    plan = (await client.post("/api/llm/plan", json={"model": "tiny.gguf", "devices": [small.device_id],
                                                     "mode": "manual"})).json()
    assert not plan["ok"] and plan["warnings"] and not plan["devices"][0]["fits"]

    # errors
    plan = (await client.post("/api/llm/plan", json={"model": "tiny.gguf", "devices": ["nope"]})).json()
    assert plan["errors"] and not plan["ok"]
    plan = (await client.post("/api/llm/plan", json={"model": "tiny.gguf", "devices": [], "mode": "manual"})).json()
    assert any("at least one" in e for e in plan["errors"])
    assert (await client.post("/api/llm/plan", json={"model": "tiny.gguf", "mode": "fast"})).status_code == 400
    missing = await client.post("/api/llm/plan", json={"model": "missing.gguf", "devices": [big.device_id]})
    assert missing.status_code == 404


def pc_gpu(vram_total: int = 4096, vram_free: int = 3800, temp: int = 50) -> Dict[str, Any]:
    """What the PC worker reports for an RTX-class card with the GPU build present."""
    return {"info": {"gpus": [{"name": "Test RTX", "vram_mb": vram_total, "vk_index": 0}],
                     "llama_gpu": "win-vulkan-x64", "coordinator_local": True},
            "gpu": {"name": "Test RTX", "vram_total_mb": vram_total, "vram_free_mb": vram_free,
                    "temp": temp, "util": 3}}


async def gpu_pc(make_device: Any, cluster: Any, name: str = "pc", free_mb: int = 8000,
                 mem_mb: int = 16384, **gpu: Any) -> FakeDevice:
    dev = await phone(make_device, name, mem_mb, free_mb, platform="windows", abi="x86_64")
    extra = pc_gpu(**gpu)
    row = cluster.db.get("devices", dev.device_id)
    info = dict(row["info"])
    info.update(extra["info"])
    cluster.db.update("devices", {"id": dev.device_id}, {"info": info, "gpu": extra["gpu"], "bench_score": 20.0})
    return dev


async def test_auto_plan_runs_a_small_model_alone_on_the_fastest_device(
        client: httpx.AsyncClient, cluster: Any, make_device: Any, tools: Path, model: Path) -> None:
    note = await phone(make_device, "note10", 8192, 3000)
    pc = await gpu_pc(make_device, cluster)
    plan = (await client.post("/api/llm/plan", json={"model": "tiny.gguf"})).json()
    assert plan["ok"] and plan["mode"] == "auto" and plan["placement"] == "single"
    assert plan["host_name"] == "pc"
    assert plan["gpu_layers"] == 4 and plan["gpu_full"]          # all of it fits on the card
    assert "pc alone" in plan["summary"] and plan["tokens_per_second"] > 0
    # the GPU share is the user's: 0 keeps the card out
    await client.patch("/api/devices/%s" % pc.device_id, json={"settings": {"gpu_pct": 0}})
    plan = (await client.post("/api/llm/plan", json={"model": "tiny.gguf"})).json()
    assert plan["placement"] == "single" and plan["host_name"] == "pc" and plan["gpu_layers"] == 0
    # narrowed to the phone: it runs there alone
    plan = (await client.post("/api/llm/plan", json={"model": "tiny.gguf", "devices": [note.device_id]})).json()
    assert plan["ok"] and plan["placement"] == "single" and plan["host_name"] == "note10"


async def test_auto_plan_splits_a_model_no_device_holds_alone(
        client: httpx.AsyncClient, cluster: Any, make_device: Any, tools: Path) -> None:
    write_gguf(Path(cluster.data) / "models" / "big.gguf", layers=10, pad_mb=1500)  # ~150 MB a layer
    note = await phone(make_device, "note10", 4096, 1700)    # 1190 MB usable
    redmi = await phone(make_device, "redmi5a", 2048, 1200)  # 840 MB usable
    plan = (await client.post("/api/llm/plan", json={"model": "big.gguf", "ctx": 512})).json()
    assert plan["ok"], plan
    assert plan["placement"] == "split" and len(plan["devices"]) == 2
    assert sum(plan["tensor_split"]) == 10
    assert plan["host"] == note.device_id                   # the roomier phone keeps the file
    # a PC too small to hold it alone joins with its GPU: CPU and card are two
    # parts of one shard, the card last (it takes the output layer)
    pc = await gpu_pc(make_device, cluster, free_mb=1200, mem_mb=8192, vram_free=1000)
    plan = (await client.post("/api/llm/plan", json={"model": "big.gguf", "ctx": 512})).json()
    assert plan["ok"] and plan["host_name"] == "pc", plan
    assert plan["rpc"][-1]["kind"] == "gpu" and plan["rpc"][-1]["device_id"] == pc.device_id
    kinds = [(p["device_id"], p["kind"]) for p in plan["rpc"]]
    assert (pc.device_id, "cpu") in kinds
    # nothing fits: a clear error
    plan = (await client.post("/api/llm/plan", json={"model": "big.gguf", "devices": [redmi.device_id]})).json()
    assert not plan["ok"] and "does not fit" in plan["errors"][0]


# --------------------------------------------------------------------------
# deployments
# --------------------------------------------------------------------------

async def _ready(client: httpx.AsyncClient, dev: FakeDevice, task: Dict[str, Any], meta: Dict[str, Any]) -> None:
    await dev.start(task["id"])
    res = await client.post("/api/w/service/%s/ready" % task["id"],
                            json={"name": task["payload"]["service"]["name"], "meta": meta},
                            headers=dev.headers)
    assert res.status_code == 200, res.text


async def test_deployment_lifecycle(client: httpx.AsyncClient, cluster: Any, make_device: Any,
                                    tools: Path, model: Path) -> None:
    big = await phone(make_device, "note10", 8192, 3000)
    small = await phone(make_device, "redmi5a", 2048, 1256, api=25)
    host = await phone(make_device, "pc", 16384, 8000, platform="windows", abi="x86_64")
    res = await client.post("/api/llm/deployments", json={
        "name": "tiny-chat", "model": "tiny.gguf", "devices": [big.device_id, small.device_id],
        "host": host.device_id, "ctx": 1024})
    assert res.status_code == 200, res.text
    dep = res.json()
    assert dep["status"] == "starting" and [s["name"] for s in dep["shards"]] == ["redmi5a", "note10"]

    # 1. shards start on exactly the chosen devices, with the pinned llama.cpp
    await cluster.scheduler.tick()
    shard_tasks = {}
    for dev in (big, small):
        cmd = (await commands(dev, "task"))[0]["task"]
        service = cmd["payload"]["service"]
        assert service["name"] == "_llm.%s.%s" % (dep["id"], dev.device_id)
        assert service["needs"] == {"llama": "b9"} and cmd["payload"]["device_id"] == dev.device_id
        shard_tasks[dev.name] = cmd
    assert await commands(host, "task", tries=2) == []     # the head waits for the shards
    await _ready(client, big, shard_tasks["note10"], {"endpoint": "10.0.0.2:5001"})
    cluster.llm.reconcile()
    assert cluster.llm.get("tiny-chat")["status"] == "starting"
    await _ready(client, small, shard_tasks["redmi5a"], {"endpoint": "10.0.0.3:5002"})
    cluster.llm.reconcile()

    # 2. the head starts on the host with the shards in split order
    dep = cluster.llm.get("tiny-chat")
    assert dep["status"] == "loading" and dep["head_endpoints"] == ["10.0.0.3:5002", "10.0.0.2:5001"]
    await cluster.scheduler.tick()
    head = (await commands(host, "task"))[0]["task"]
    service = head["payload"]["service"]
    assert service["name"] == "tiny-chat" and head["payload"]["device_id"] == host.device_id
    assert service["params"]["rpc"] == ["10.0.0.3:5002", "10.0.0.2:5001"]
    assert service["params"]["tensor_split"] == [p["layers"] for s in dep["shards"] for p in s["parts"]]
    assert sum(service["params"]["tensor_split"]) == 4
    assert "llama_variant" not in service["needs"]           # no GPU here: the CPU build
    assert service["params"]["ctx"] == 1024
    assert service["needs"]["model"] == {"name": "tiny.gguf", "size": model.stat().st_size}
    await _ready(client, host, head, {"port": 1234})
    cluster.llm.reconcile()
    assert cluster.llm.get("tiny-chat")["status"] == "ready"
    names = [s["name"] for s in (await client.get("/api/services")).json()]
    assert names == ["tiny-chat"]                             # shards stay hidden
    listing = (await client.get("/api/llm/models")).json()
    assert listing[0]["used_by"] == ["tiny-chat"]
    assert (await client.delete("/api/llm/models/tiny.gguf")).status_code == 409

    # 3. a shard device drops off -> degraded, the head is stopped
    cluster.db.update("devices", {"id": small.device_id}, {"last_seen": 1.0})
    cluster.scheduler.mark_offline_devices()
    cluster.llm.reconcile()
    dep = cluster.llm.get("tiny-chat")
    assert dep["status"] == "degraded" and "redmi5a" in dep["error"]
    assert any(c.get("task_id") == head["id"] for c in await commands(host, "cancel"))

    # 4. it comes back on a new port -> a new head with the new address
    # (the background scheduler may hand the task over in this very poll)
    first = [c for c in await small.poll() if c.get("cmd") == "task"]
    await cluster.scheduler.tick()
    again = (first or await commands(small, "task"))[0]["task"]
    assert again["id"] == shard_tasks["redmi5a"]["id"] and again["attempt"] == 2
    await _ready(client, small, again, {"endpoint": "10.0.0.3:6000"})
    cluster.llm.reconcile()
    dep = cluster.llm.get("tiny-chat")
    assert dep["status"] == "loading" and dep["head_endpoints"] == ["10.0.0.3:6000", "10.0.0.2:5001"]
    await cluster.scheduler.tick()
    head2 = (await commands(host, "task"))[0]["task"]
    assert head2["id"] != head["id"]
    assert head2["payload"]["service"]["params"]["rpc"] == ["10.0.0.3:6000", "10.0.0.2:5001"]
    await _ready(client, host, head2, {"port": 1235})
    cluster.llm.reconcile()
    assert cluster.llm.get("tiny-chat")["status"] == "ready"

    # 5. stop: every piece is cancelled, nothing keeps listening
    stopped = (await client.post("/api/llm/deployments/tiny-chat/stop")).json()
    assert stopped["status"] == "stopped"
    for dev in (big, small, host):
        assert await commands(dev, "cancel")
    listed = (await client.get("/api/services")).json()
    assert [s for s in listed if s["instances"]] == []             # nothing runs any more ...
    assert [s["name"] for s in listed if s.get("on_demand")] == ["tiny-chat"]  # ... but a call loads it
    assert (await client.delete("/api/llm/deployments/tiny-chat")).status_code == 200
    assert (await client.get("/api/llm/deployments/tiny-chat")).status_code == 404


async def test_shard_that_ends_fails_the_deployment(client: httpx.AsyncClient, cluster: Any,
                                                    make_device: Any, tools: Path, model: Path) -> None:
    dev = await phone(make_device, "note10", 8192, 3000)
    host = await phone(make_device, "pc", 16384, 8000, platform="windows", abi="x86_64")
    dep = (await client.post("/api/llm/deployments", json={
        "name": "solo", "model": "tiny.gguf", "devices": [dev.device_id], "host": host.device_id})).json()
    assert dep["mode"] == "manual" and dep["placement"] == "split"
    shard_job = dep["shard_jobs"][dev.device_id]
    cluster.scheduler.cancel_job(shard_job, "someone cancelled the shard job")
    cluster.llm.reconcile()
    dep = cluster.llm.get("solo")
    assert dep["status"] == "failed" and "note10" in dep["error"]
    # restart brings it back from scratch
    dep = (await client.post("/api/llm/deployments/solo/restart")).json()
    assert dep["status"] == "starting" and dep["shard_jobs"][dev.device_id] != shard_job


async def test_deploy_validation(client: httpx.AsyncClient, cluster: Any, make_device: Any,
                                 tools: Path, model: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dev = await phone(make_device, "note10", 8192, 3000)
    tiny = await phone(make_device, "tinyphone", 1024, 258)   # 2 MB usable after the 256 MB reserve

    async def deploy(**body: Any) -> httpx.Response:
        base = {"name": "x", "model": "tiny.gguf", "devices": [dev.device_id]}
        base.update(body)
        return await client.post("/api/llm/deployments", json=base)

    assert (await deploy(name="Bad Name!")).status_code == 400
    assert (await deploy(model="missing.gguf")).status_code == 404
    assert (await deploy(devices=["nope"])).status_code == 400
    too_big = await deploy(devices=[tiny.device_id])
    assert too_big.status_code == 400 and "does not fit" in too_big.json()["error"]
    assert (await deploy(name="forced", devices=[tiny.device_id], force=True)).status_code == 200
    assert (await deploy(name="first")).status_code == 200
    assert (await deploy(name="first")).status_code == 400   # already running
    assert (await client.delete("/api/llm/deployments/first")).status_code == 409  # stop first
    monkeypatch.setattr(LLMManager, "tag", lambda self: None)
    missing = await deploy(name="nollama")
    assert missing.status_code == 400 and "get-llama" in missing.json()["error"]


async def test_auto_deploy_alone_on_the_gpu(client: httpx.AsyncClient, cluster: Any, make_device: Any,
                                            tools: Path, model: Path) -> None:
    pc = await gpu_pc(make_device, cluster)
    dep = (await client.post("/api/llm/deployments", json={"name": "fast", "model": "tiny.gguf"})).json()
    assert dep["mode"] == "auto" and dep["placement"] == "single" and dep["shards"] == []
    cluster.llm.reconcile()                      # nothing to wait for: the head starts at once
    assert cluster.llm.get("fast")["status"] == "loading"
    await cluster.scheduler.tick()
    head = (await commands(pc, "task"))[0]["task"]
    service = head["payload"]["service"]
    assert service["params"]["rpc"] == [] and service["params"]["gpu_layers"] == 999
    assert service["params"]["gpu"] == {"vk_index": 0}
    assert service["needs"]["llama_variant"] == "gpu"
    assert service["params"]["threads"] == 7     # 8 cores, one left idle


async def test_auto_model_moves_off_a_device_that_gets_too_hot(
        client: httpx.AsyncClient, cluster: Any, make_device: Any, tools: Path, model: Path) -> None:
    note = await phone(make_device, "note10", 8192, 3000)
    redmi = await phone(make_device, "redmi5a", 4096, 2500)
    dep = (await client.post("/api/llm/deployments", json={
        "name": "roam", "model": "tiny.gguf", "devices": [note.device_id, redmi.device_id]})).json()
    assert dep["placement"] == "single" and dep["host_device"] == note.device_id
    cluster.llm.reconcile()
    await cluster.scheduler.tick()
    assert await commands(note, "task")
    note.temp = 44.0
    await note.poll()
    await cluster.scheduler.tick()               # the scheduler moves the model's task off the phone
    cluster.llm.reconcile()                      # the manager places it again
    moved = cluster.llm.get("roam")
    assert moved["host_device"] == redmi.device_id and moved["status"] in ("starting", "loading")
    events = [e["message"] for e in cluster.db.select("events", "WHERE kind='llm_moved'")]
    assert events and "too hot" in events[0]
    await cluster.scheduler.tick()
    head = (await commands(redmi, "task"))[0]["task"]
    assert head["payload"]["service"]["name"] == "roam"


async def test_auto_model_waits_when_nothing_else_fits(
        client: httpx.AsyncClient, cluster: Any, make_device: Any, tools: Path, model: Path) -> None:
    note = await phone(make_device, "note10", 8192, 3000)
    dep = (await client.post("/api/llm/deployments", json={"name": "solo", "model": "tiny.gguf"})).json()
    cluster.llm.reconcile()
    await client.patch("/api/devices/%s" % note.device_id, json={"settings": {"roles": {"ai": False}}})
    cluster.llm.reconcile()
    dep = cluster.llm.get("solo")
    assert dep["status"] == "degraded" and "AI models off" in dep["error"]
    # switched back on: it is placed again
    await client.patch("/api/devices/%s" % note.device_id, json={"settings": {"roles": {"ai": True}}})
    cluster.llm._replan_at.clear()
    cluster.llm.reconcile()
    assert cluster.llm.get("solo")["status"] in ("starting", "loading")


async def test_manual_model_waits_for_its_device(client: httpx.AsyncClient, cluster: Any, make_device: Any,
                                                tools: Path, model: Path) -> None:
    note = await phone(make_device, "note10", 8192, 3000)
    host = await phone(make_device, "pc", 16384, 8000, platform="windows", abi="x86_64")
    dep = (await client.post("/api/llm/deployments", json={
        "name": "pinned", "model": "tiny.gguf", "devices": [note.device_id], "host": host.device_id})).json()
    await cluster.scheduler.tick()
    shard = (await commands(note, "task"))[0]["task"]
    await _ready(client, note, shard, {"endpoint": "10.0.0.2:5001"})
    cluster.llm.reconcile()
    assert cluster.llm.get("pinned")["status"] == "loading"
    await client.patch("/api/devices/%s" % note.device_id, json={"paused": True})
    await cluster.scheduler.tick()               # shard moved off: the device was switched off
    cluster.llm.reconcile()
    dep = cluster.llm.get("pinned")
    assert dep["status"] == "degraded" and "note10" in dep["error"]
    assert dep["host_device"] == host.device_id  # nothing was moved
    await client.patch("/api/devices/%s" % note.device_id, json={"paused": False})
    await cluster.scheduler.tick()
    again = (await commands(note, "task"))[0]["task"]
    assert again["id"] == shard["id"] and again["attempt"] == 1  # the pause cost no attempt


async def test_a_hot_gpu_hands_its_layers_back_to_the_cpu(
        client: httpx.AsyncClient, cluster: Any, make_device: Any, tools: Path, model: Path) -> None:
    pc = await gpu_pc(make_device, cluster)
    await client.post("/api/llm/deployments", json={"name": "gpu", "model": "tiny.gguf"})
    cluster.llm.reconcile()
    first = cluster.llm.get("gpu")
    assert first["options"]["gpu_layers"] == 4
    cluster.db.update("devices", {"id": pc.device_id}, {"gpu": dict(pc_gpu()["gpu"], temp=86)})
    cluster.llm.reconcile()
    dep = cluster.llm.get("gpu")
    assert dep["host_device"] == pc.device_id and dep["options"]["gpu_layers"] == 0
    assert dep["head_job"] != first["head_job"]
    events = [e["message"] for e in cluster.db.select("events", "WHERE kind='llm_moved'")]
    assert any("GPU too hot" in m for m in events)


async def test_llm_api_needs_the_pin(anon: httpx.AsyncClient) -> None:
    for path in ("/api/llm/models", "/api/llm/devices", "/api/llm/deployments", "/api/llm/runtime"):
        assert (await anon.get(path)).status_code == 401


# --------------------------------------------------------------------------
# worker endpoints
# --------------------------------------------------------------------------

async def test_worker_runtime_and_model_downloads(client: httpx.AsyncClient, device: FakeDevice,
                                                  anon: httpx.AsyncClient, tools: Path, model: Path) -> None:
    manifest = (await client.get("/api/w/runtime/llama", headers=device.headers)).json()
    assert manifest["tag"] == "b9"
    assert set(manifest["assets"]) == {"win-x64", "android-arm64"}
    name = manifest["assets"]["android-arm64"]["file"]
    got = await client.get("/api/w/runtime/llama/%s" % name, headers=device.headers)
    assert got.status_code == 200 and got.content == (tools / name).read_bytes()
    assert (await client.get("/api/w/runtime/llama/VERSION", headers=device.headers)).status_code == 404
    assert (await anon.get("/api/w/runtime/llama")).status_code == 401

    part = await client.get("/api/w/models/tiny.gguf", headers=dict(device.headers, Range="bytes=4-9"))
    assert part.status_code == 206 and part.content == model.read_bytes()[4:10]
    assert (await client.get("/api/w/models/nope.gguf", headers=device.headers)).status_code == 404
    assert (await client.get("/api/w/models/..%2Fsecret.gguf", headers=device.headers)).status_code == 404
