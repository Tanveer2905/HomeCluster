"""Automatic task sizing, speed memory, requests first, models on call,
more copies of busy services (SPEC §9.5-§9.7, §6.4c/d)."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest

from conftest import FakeDevice, wait_for
from test_llm import _ready, commands, model, phone, tools, write_gguf  # noqa: F401  (fixtures)
from test_scheduler import add_device, drain, tasks_of
from test_services import bring_up, serve_one

MAPPER = "def fn(item, ctx):\n    return item\n"


def submit_items(state: Any, n: int, **params: Any) -> Dict[str, Any]:
    spec = {"type": "python_map", "name": "map", "params": dict({"code": MAPPER, "items": list(range(n))}, **params)}
    return state.submit_job(spec)


def finish(state: Any, task: Dict[str, Any], device: Dict[str, Any], per_item: float) -> None:
    items = task["payload"]["impls"]["python"]["args"]["items"]
    state.scheduler.task_done(task["id"], device, {"ok": True, "result": list(items),
                                                    "duration": max(0.001, per_item * len(items))})


def dyn(state: Any, job_id: str) -> Dict[str, Any]:
    return state.db.get("jobs", job_id)["options"]["_dynamic"]


# --------------------------------------------------------------------------
# automatic task sizing (SPEC §9.6)
# --------------------------------------------------------------------------

def test_a_job_without_a_chunk_size_is_cut_as_devices_free_up(state: Any) -> None:
    pc = add_device(state, name="pc", slots=4)
    phone_ = add_device(state, name="phone", slots=1)
    speed = {pc["id"]: 0.01, phone_["id"]: 0.1}           # seconds per item
    job = submit_items(state, 1000)
    state.scheduler.split_pending()
    row = state.db.get("jobs", job["id"])
    assert row["status"] == "running" and row["total_tasks"] == 0
    assert dyn(state, job["id"])["units"] == 1000 and tasks_of(state, job["id"]) == []

    # first round: small probe pieces for every free slot
    assert state.scheduler.assign() == 5
    first = tasks_of(state, job["id"])
    assert all(15 <= t["payload"]["meta"]["units"] <= 20 for t in first)   # ~ 1000 / (10 x 5 slots)
    sizes: Dict[str, List[int]] = {pc["id"]: [], phone_["id"]: []}
    rounds = 0
    while state.db.get("jobs", job["id"])["status"] == "running" and rounds < 200:
        rounds += 1
        for task in tasks_of(state, job["id"]):
            if task["status"] == "assigned":
                holder = pc if task["device_id"] == pc["id"] else phone_
                sizes[holder["id"]].append(task["payload"]["meta"]["units"])
                finish(state, task, holder, speed[holder["id"]])
        state.scheduler.assign()
        state.scheduler.finish_jobs()
    done = state.db.get("jobs", job["id"])
    assert done["status"] == "done"
    assert done["result"] == list(range(1000))              # order kept across pieces
    assert dyn(state, job["id"])["done"] == 1000
    # once speeds were known the PC got much bigger pieces than the phone
    assert max(sizes[pc["id"]][4:] or [0]) > 5 * max(sizes[phone_["id"]][1:] or [1])
    assert done["total_tasks"] < 60
    assert done["report"]["fastest_device"]["name"] == "pc"


def test_a_fixed_chunk_size_still_splits_up_front(state: Any) -> None:
    add_device(state)
    job = submit_items(state, 100, chunk_size=10)
    state.scheduler.split_pending()
    assert len(tasks_of(state, job["id"])) == 10
    assert "_dynamic" not in state.db.get("jobs", job["id"])["options"]


def test_numbers_jobs_are_cut_too(state: Any) -> None:
    add_device(state, slots=2)
    job = state.submit_job({"type": "prime_range", "params": {"start": 0, "end": 200000}})
    state.scheduler.split_pending()
    assert dyn(state, job["id"])["units"] == 200000 and dyn(state, job["id"])["grain"] == 1000
    state.scheduler.assign()
    pieces = tasks_of(state, job["id"])
    assert len(pieces) == 2
    args = pieces[0]["payload"]["impls"]["python"]["args"]
    assert args["start"] == 0 and args["end"] == pieces[1]["payload"]["impls"]["python"]["args"]["start"]
    assert pieces[0]["payload"]["impls"]["js"]["args"] == args          # browsers get the same piece


def test_cutting_survives_a_coordinator_restart(state: Any) -> None:
    device = add_device(state, slots=1)
    job = submit_items(state, 50)
    state.scheduler.split_pending()
    state.scheduler.assign()
    finish(state, tasks_of(state, job["id"])[0], device, 0.01)
    state.scheduler._work.clear()                         # memory lost; cursor kept in the job
    assert state.scheduler.assign() == 1
    second = tasks_of(state, job["id"])[1]
    assert second["payload"]["impls"]["python"]["args"]["start"] == tasks_of(state, job["id"])[0]["payload"]["meta"]["units"]


# --------------------------------------------------------------------------
# speed memory (SPEC §9.5)
# --------------------------------------------------------------------------

def test_speeds_are_remembered_per_kind_of_work_and_device(state: Any) -> None:
    pc = add_device(state, name="pc", slots=1)
    job = submit_items(state, 40)
    state.scheduler.split_pending()
    state.scheduler.assign()
    finish(state, tasks_of(state, job["id"])[0], pc, 0.01)
    key = state.plugins.require("python_map").speed_key({"code": MAPPER, "fn_name": "fn"})
    assert key.startswith("python_map:")
    rate = state.speeds.rate(key, pc["id"])
    assert rate == pytest.approx(100.0, rel=0.01)
    listing = state.speeds.listing()
    assert listing[0]["device"] == "pc" and listing[0]["key"] == key
    # a different function is a different kind of work
    assert state.plugins.require("python_map").speed_key({"code": MAPPER + "# v2\n"}) != key
    # smoothing: a slower run pulls the rate down, not all the way
    for _ in range(4):
        state.speeds.record(key, pc["id"], 10, 1.0)
    assert 10.0 < state.speeds.rate(key, pc["id"]) < 100.0


def test_a_new_job_starts_with_what_was_learned(state: Any) -> None:
    pc = add_device(state, name="pc", slots=1)
    phone_ = add_device(state, name="phone", slots=1)
    key = state.plugins.require("python_map").speed_key({"code": MAPPER, "fn_name": "fn"})
    state.speeds.record(key, pc["id"], 1000, 1.0)        # 1000 items/s
    state.speeds.record(key, phone_["id"], 10, 1.0)      # 10 items/s
    job = submit_items(state, 10000)
    state.scheduler.split_pending()
    state.scheduler.assign()
    pieces = {t["device_id"]: t["payload"]["meta"]["units"] for t in tasks_of(state, job["id"])}
    assert pieces[pc["id"]] >= 1000                      # no probing: about 2 s of work or more
    assert pieces[phone_["id"]] < pieces[pc["id"]] / 20


def test_new_devices_are_estimated_from_the_same_runtime_only(state: Any) -> None:
    from coordinator.scheduler import SpeedBook

    ref = add_device(state, name="pc", bench_score=20.0)
    twin = add_device(state, name="laptop", bench_score=10.0)
    tab = add_device(state, name="tab", bench_score=10.0, platform="browser", caps=["js", "browser"])
    state.speeds.record("k", ref["id"], 100, 1.0)
    book = SpeedBook(state.scheduler)
    assert book.rate("job", "k", state.db.get("devices", twin["id"])) == pytest.approx(50.0)
    assert book.rate("job", "k", state.db.get("devices", tab["id"])) is None   # JS scores do not compare


def test_a_removed_device_is_forgotten(state: Any) -> None:
    device = add_device(state)
    state.speeds.record("k", device["id"], 1, 1)
    state.scheduler.remove_device(device["id"])
    assert state.speeds.rates("k") == {}


# --------------------------------------------------------------------------
# requests first (SPEC §9.7)
# --------------------------------------------------------------------------

def test_a_device_answering_a_request_gets_no_new_tasks(state: Any) -> None:
    device = add_device(state, slots=2)
    job = submit_items(state, 4, chunk_size=1)
    state.scheduler.split_pending()
    state.scheduler.hold([device["id"]], "req-1")
    assert drain(state, device["id"]) == [{"cmd": "hold", "request_id": "req-1", "seconds": 120.0}]
    assert state.scheduler.assign() == 0
    state.scheduler.release("req-1")
    assert drain(state, device["id"]) == [{"cmd": "release", "request_id": "req-1"}]
    assert state.scheduler.assign() == 2
    # a hold ends on its own
    state.scheduler.hold([device["id"]], "req-2", seconds=0.05)
    assert state.scheduler.held(device["id"])
    time.sleep(0.1)
    assert not state.scheduler.held(device["id"])


def test_requests_first_can_be_switched_off(state: Any) -> None:
    device = add_device(state)
    state.save_settings({"scheduler": {"requests_first": False}})
    state.scheduler.hold([device["id"]], "r")
    assert not state.scheduler.held(device["id"]) and drain(state, device["id"]) == []


@pytest.mark.asyncio
async def test_a_service_call_holds_its_device_until_it_is_answered(
        client: httpx.AsyncClient, cluster: Any, device: FakeDevice) -> None:
    await bring_up(client, cluster, device)
    caller = asyncio.create_task(client.post("/api/services/echo/call", content=b"hi",
                                             params={"request_id": "held-1"}))
    await wait_for(lambda: cluster.scheduler.held(device.device_id), timeout=5)
    seen: List[Dict[str, Any]] = []
    cmd = None
    for _ in range(40):
        for c in await device.poll():
            seen.append(c)
            if c.get("cmd") == "service_request":
                cmd = c
        if cmd:
            break
    assert {"cmd": "hold", "request_id": "held-1", "seconds": 120.0} in seen
    await client.get("/api/w/requests/held-1/input", headers=device.headers)
    await client.post("/api/w/requests/held-1/done", json={"ok": True, "result": 1}, headers=device.headers)
    assert (await caller).status_code == 200
    assert not cluster.scheduler.held(device.device_id)
    assert any(c.get("cmd") == "release" for c in await device.poll())


# --------------------------------------------------------------------------
# models load on call, unload when idle (SPEC 6.4d)
# --------------------------------------------------------------------------

async def _bring_head_up(client: httpx.AsyncClient, cluster: Any, dev: FakeDevice, name: str) -> Dict[str, Any]:
    cluster.llm.reconcile()
    await cluster.scheduler.tick()
    head = None
    for _ in range(40):
        for c in await dev.poll():
            if c.get("cmd") == "task" and c["task"]["payload"]["service"]["name"] == name:
                head = c["task"]
        if head:
            break
    assert head is not None, "the model never started on %s" % dev.name
    await _ready(client, dev, head, {"port": 1})
    cluster.llm.reconcile()
    return head


@pytest.mark.asyncio
async def test_calling_a_model_by_name_loads_it(client: httpx.AsyncClient, cluster: Any, make_device: Any,
                                               tools: Path, model: Path) -> None:
    note = await phone(make_device, "note10", 8192, 3000)
    listed = (await client.get("/api/services")).json()
    assert [s["name"] for s in listed if s.get("on_demand")] == ["tiny"]
    caller = asyncio.create_task(client.post("/api/services/tiny/call", json={"prompt": "hi"},
                                             params={"request_id": "od-1"}))
    await wait_for(lambda: cluster.llm.get("tiny") is not None, timeout=5)
    dep = cluster.llm.get("tiny")
    assert dep["options"]["on_demand"] and dep["options"]["placement"] == "single"
    await _bring_head_up(client, cluster, note, "tiny")
    served = await serve_one(client, note, partials=[{"token": "hello"}], result={"text": "hello"})
    res = await caller
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["result"] == {"text": "hello"}
    assert body["partials"][0]["status"].startswith("loading tiny")  # the caller was told
    assert served["id"] == "od-1"


@pytest.mark.asyncio
async def test_chat_reaches_the_default_model(client: httpx.AsyncClient, cluster: Any, tools: Path,
                                              model: Path) -> None:
    assert cluster.llm.resolve_alias("chat") == "tiny"          # the only model
    write_gguf(Path(cluster.data) / "models" / "other.gguf")
    assert cluster.llm.resolve_alias("chat") == "chat"          # two models: no guess
    res = await client.put("/api/llm/default", json={"model": "other.gguf"})
    assert res.status_code == 200 and res.json()["name"] == "other"
    assert cluster.llm.resolve_alias("chat") == "other"
    assert cluster.llm.resolve_alias("tiny.gguf") == "tiny"
    assert (await client.put("/api/llm/default", json={"model": "nope.gguf"})).status_code == 404


@pytest.mark.asyncio
async def test_a_model_that_cannot_fit_answers_503(client: httpx.AsyncClient, cluster: Any, make_device: Any,
                                                   tools: Path, model: Path) -> None:
    await phone(make_device, "tinyphone", 1024, 258)             # 2 MB usable
    res = await client.post("/api/services/tiny/call", content=b"hi", params={"queue_timeout": 1})
    assert res.status_code == 503 and "cannot load tiny" in res.json()["error"]


@pytest.mark.asyncio
async def test_an_unused_model_unloads_and_comes_back(client: httpx.AsyncClient, cluster: Any, make_device: Any,
                                                      tools: Path, model: Path) -> None:
    note = await phone(make_device, "note10", 8192, 3000)
    dep = (await client.post("/api/llm/deployments", json={"name": "tiny", "model": "tiny.gguf"})).json()
    await _bring_head_up(client, cluster, note, "tiny")
    assert cluster.llm.get("tiny")["status"] == "ready"
    cluster.llm.reconcile()
    assert cluster.llm.get("tiny")["status"] == "ready"         # 10 minutes have not passed
    cluster.db.update("llm_deployments", {"id": dep["id"]}, {"ready_at": time.time() - 3600})
    for inst in cluster.services.instances.values():
        inst.last_used = time.time() - 3600
    # kept loaded: nothing happens
    kept = (await client.post("/api/llm/deployments/tiny/keep", json={"keep": True})).json()
    assert kept["idle_minutes"] == 0
    cluster.llm.reconcile()
    assert cluster.llm.get("tiny")["status"] == "ready"
    await client.post("/api/llm/deployments/tiny/keep", json={"keep": False})
    cluster.llm.reconcile()
    idle = cluster.llm.get("tiny")
    assert idle["status"] == "idle"
    assert any(c.get("cmd") == "cancel" for c in await commands(note, "cancel"))
    listed = (await client.get("/api/services")).json()
    assert [s["name"] for s in listed if s.get("on_demand")] == ["tiny"]
    assert "loading tiny" in cluster.llm.ensure("tiny")
    assert cluster.llm.get("tiny")["status"] == "starting"


# --------------------------------------------------------------------------
# more copies of busy services (SPEC 6.4c)
# --------------------------------------------------------------------------

def _queue(cluster: Any, name: str, n: int, age: float = 5.0) -> List[Any]:
    reqs = []
    for i in range(n):
        req = cluster.services.create_request(name, {}, "text/plain", 1, None, None)
        req.created = time.time() - age
        reqs.append(req)
    return reqs


@pytest.mark.asyncio
async def test_a_busy_model_gets_a_second_copy_and_loses_it_when_quiet(
        client: httpx.AsyncClient, cluster: Any, make_device: Any, tools: Path, model: Path) -> None:
    a = await phone(make_device, "note10", 8192, 3000)
    b = await phone(make_device, "redmi", 8192, 3000)
    await client.post("/api/llm/deployments", json={"name": "tiny", "model": "tiny.gguf",
                                                     "devices": [a.device_id], "idle_minutes": 0})
    await _bring_head_up(client, cluster, a, "tiny")
    reqs = _queue(cluster, "tiny", 2)
    cluster.autoscaler.tick()
    copies = cluster.llm.describe(cluster.llm.get("tiny"))["copies"]
    assert len(copies) == 1 and copies[0]["host_name"] == "redmi"
    assert [d["name"] for d in cluster.llm.deployments()] == ["tiny"]   # copies stay out of the list
    cluster.autoscaler.tick()                                           # one copy at a time
    assert len(cluster.llm.describe(cluster.llm.get("tiny"))["copies"]) == 1
    await _bring_head_up(client, cluster, b, "tiny")
    assert sum(1 for i in cluster.services.instances.values() if i.name == "tiny") == 2
    # quiet again: the extra copy goes
    for req in reqs:
        cluster.services._finish(req, ok=False, error="test")
    cluster.config.set("services", dict(cluster.config.get("services"), scale_down_minutes=0))
    cluster.autoscaler.tick()
    assert cluster.llm.describe(cluster.llm.get("tiny"))["copies"] == []
    assert any(c.get("cmd") == "cancel" for c in await commands(b, "cancel"))


@pytest.mark.asyncio
async def test_a_busy_service_gets_another_task_on_a_free_device(
        client: httpx.AsyncClient, cluster: Any, device: FakeDevice, make_device: Any) -> None:
    task = await bring_up(client, cluster, device)
    other = await make_device(name="spare")
    _queue(cluster, "echo", 1)
    cluster.autoscaler.tick()
    copies = [t for t in cluster.db.select("tasks", "WHERE job_id=?", (task["job_id"],))
              if (t["payload"].get("meta") or {}).get("autoscaled")]
    assert len(copies) == 1 and copies[0]["payload"]["device_id"] == other.device_id
    # nothing anywhere: no copy, and a cool-down before trying again
    cluster.autoscaler.pending.clear()
    assert cluster.autoscaler.scale_up("echo") is False


# --------------------------------------------------------------------------
# worker side: pausing a task's processes
# --------------------------------------------------------------------------

def _load_worker() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "pc_worker_speed", str(Path(__file__).resolve().parents[1] / "worker" / "worker.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


COUNTER = "import time,sys\nn=0\nwhile True:\n    n+=1\n    open(sys.argv[1],'w').write(str(n))\n    time.sleep(0.02)\n"


def _count(path: Path) -> int:
    """The counter's value; the file is briefly empty while the counter rewrites it."""
    for _ in range(50):
        text = path.read_text()
        if text:
            return int(text)
        time.sleep(0.01)
    return 0


def test_a_paused_task_stops_running_and_resumes(tmp_path: Path) -> None:
    w = _load_worker()
    out = tmp_path / "n.txt"
    proc = subprocess.Popen([sys.executable, "-c", COUNTER, str(out)], **w.batch_popen_kwargs())
    try:
        for _ in range(100):
            if out.exists() and out.read_text():
                break
            time.sleep(0.05)
        assert w.pause_tree(proc)
        time.sleep(0.3)
        frozen = out.read_text()
        time.sleep(0.4)
        assert out.read_text() == frozen                     # nothing ran while paused
        assert w.pause_tree(proc, resume=True)
        time.sleep(0.4)
        assert _count(out) > int(frozen or 0)
    finally:
        w.kill_tree(proc)


def test_worker_holds_pause_and_resume_its_tasks(tmp_path: Path) -> None:
    w = _load_worker()
    worker = w.Worker("http://127.0.0.1:9", pin="1", name="t", state_dir=str(tmp_path / "s"))

    class Run(object):
        def __init__(self) -> None:
            self.paused = 0
            self.resumed = 0

        def pause(self) -> None:
            self.paused += 1

        def unpause(self) -> None:
            self.resumed += 1

    run = Run()
    worker.tasks["t1"] = run
    worker.cmd_hold({"request_id": "a", "seconds": 30})
    worker.cmd_hold({"request_id": "b", "seconds": 30})
    assert run.paused == 1 and worker.held()
    worker.cmd_release({"request_id": "a"})
    assert run.resumed == 0                                  # b still answers
    worker.cmd_release({"request_id": "b"})
    assert run.resumed == 1 and not worker.held()
    worker.cmd_hold({"request_id": "c", "seconds": 1})
    time.sleep(2.5)
    assert run.resumed == 2 and not worker.held()            # it ended on its own
    worker.stopping = True
