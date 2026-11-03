"""REST surface: auth, jobs, pipelines, files, templates, schedules, settings,
events and export (SPEC §3, §5, §8, §10, §11)."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import zipfile
from typing import Any, Dict, List

import pytest

from conftest import FakeDevice, running_device, wait_for

pytestmark = pytest.mark.asyncio

MAPPER = "def fn(item, ctx):\n    return item * 2\n"


async def submit(client: Any, **spec: Any) -> Dict[str, Any]:
    response = await client.post("/api/jobs", json=spec)
    assert response.status_code == 200, response.text
    return response.json()


async def job_of(client: Any, job_id: str) -> Dict[str, Any]:
    response = await client.get("/api/jobs/%s" % job_id)
    assert response.status_code == 200, response.text
    return response.json()


async def finished(client: Any, job_id: str) -> Any:
    job = await job_of(client, job_id)
    return job if job["status"] in ("done", "failed", "cancelled") else None


# --------------------------------------------------------------------------
# auth (SPEC §3)
# --------------------------------------------------------------------------

async def test_public_endpoints_need_no_pin(anon: Any, app: Any) -> None:
    info = await anon.get("/api/info")
    assert info.status_code == 200
    body = info.json()
    assert body["name"] == "phonecluster"
    assert body["has_pin"] is True
    assert all(u.startswith("https://") and not u.startswith("https://127.") for u in body["urls"])
    assert (await anon.get("/")).status_code == 200          # this PC: the page asks for the PIN
    assert (await anon.get("/join")).status_code == 200
    assert (await anon.get("/worker.py")).status_code == 200
    assert (await anon.get("/ca.pem")).text.startswith("-----BEGIN CERTIFICATE-----")
    assert (await anon.get("/qr.svg", params={"text": "x"})).status_code == 200
    # the installer exists only for a valid one-time code
    assert (await anon.get("/install.sh")).status_code == 403


@pytest.mark.parametrize(
    "path",
    ["/api/devices", "/api/jobs", "/api/files", "/api/storage", "/api/settings",
     "/api/events", "/api/plugins", "/api/schedules", "/api/templates", "/api/stats",
     "/api/join"],
)
async def test_every_api_endpoint_is_pin_protected(anon: Any, path: str) -> None:
    response = await anon.get(path)
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


async def test_wrong_pin_is_unauthorized(anon: Any) -> None:
    response = await anon.get("/api/devices", headers={"X-Pin": "000000"})
    assert response.status_code == 401


async def test_login_sets_a_cookie(anon: Any, app: Any) -> None:
    pin = app.state.cluster.config.pin
    assert (await anon.post("/api/login", json={"pin": "999999"})).status_code == 401
    response = await anon.post("/api/login", json={"pin": pin})
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    cookie = response.cookies.get("pc_session") or anon.cookies.get("pc_session")
    assert cookie and pin not in cookie  # a random session, never the PIN
    assert (await anon.get("/api/devices")).status_code == 200


async def test_worker_needs_a_valid_token(anon: Any, app: Any) -> None:
    response = await anon.post("/api/w/poll", json={"status": {}})
    assert response.status_code == 401
    response = await anon.post(
        "/api/w/poll", json={"status": {}}, headers={"X-Device-Token": "nope"}
    )
    assert response.status_code == 401


async def test_register_needs_the_pin(client: Any) -> None:
    response = await client.post(
        "/api/w/register", json={"pin": "000000", "name": "x", "caps": ["python"]}
    )
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


async def test_removed_device_gets_410(client: Any, device: FakeDevice) -> None:
    assert (await client.delete("/api/devices/%s" % device.device_id)).status_code == 200
    response = await client.post(
        "/api/w/poll", json={"status": {}}, headers=device.headers
    )
    assert response.status_code == 410
    assert response.json() == {"error": "removed"}


async def test_reattach_with_a_saved_token(client: Any, device: FakeDevice) -> None:
    original = device.device_id
    again = await device.register(device_id=original, token=device.token)
    assert again["device_id"] == original
    assert again["token"] == device.token

    bad = await client.post(
        "/api/w/register",
        json={"device_id": original, "token": "f" * 64, "name": "x", "caps": ["python"]},
    )
    assert bad.status_code == 401


async def test_register_reports_bench_done(client: Any, device: FakeDevice) -> None:
    assert "bench_done" not in await device.register(
        device_id=device.device_id, token=device.token
    )
    await device.bench(7.5)
    again = await device.register(device_id=device.device_id, token=device.token)
    assert again["bench_done"] is True
    detail = await client.get("/api/devices/%s" % device.device_id)
    assert detail.json()["bench_score"] == 7.5


# --------------------------------------------------------------------------
# devices (SPEC §11)
# --------------------------------------------------------------------------

async def test_device_listing_and_patching(client: Any, device: FakeDevice) -> None:
    listing = await client.get("/api/devices")
    assert listing.status_code == 200
    rows = listing.json()
    assert len(rows) == 1
    assert rows[0]["id"] == device.device_id
    assert "token_hash" not in rows[0]
    assert rows[0]["status"] == "online"
    assert rows[0]["healthy"] is True

    patched = await client.patch(
        "/api/devices/%s" % device.device_id,
        json={"name": "kitchen", "slots": 3, "paused": True,
              "settings": {"min_battery": 5}},
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "kitchen"
    assert patched.json()["slots"] == 3
    assert patched.json()["paused"] == 1
    assert patched.json()["settings"]["min_battery"] == 5

    commands = await device.poll()
    assert commands[0]["cmd"] == "config"
    assert commands[0]["slots"] == 3
    assert commands[0]["paused"] is True

    detail = await client.get("/api/devices/%s" % device.device_id)
    assert detail.json()["tasks_done"] == 0
    assert (await client.get("/api/devices/nope")).status_code == 404
    assert (await client.patch("/api/devices/nope", json={"name": "x"})).status_code == 404


async def test_device_roles_and_limits(client: Any, device: FakeDevice) -> None:
    url = "/api/devices/%s" % device.device_id
    control = (await client.get(url)).json()["control"]
    assert control["roles"] == {"jobs": True, "ai": True, "storage": True}
    assert control["limits"] == {"cpu_pct": 100, "mem_pct": 70, "gpu_pct": 80}
    patched = await client.patch(url, json={"settings": {"roles": {"ai": False}, "cpu_pct": 50, "mem_pct": 95}})
    assert patched.status_code == 200
    control = patched.json()["control"]
    assert control["roles"]["ai"] is False and control["roles"]["jobs"] is True
    assert control["limits"]["cpu_pct"] == 50 and control["limits"]["mem_pct"] == 70  # Balanced ceiling
    assert "AI models off" in control["safety"]["notes"]
    # roles merge: switching storage off keeps AI off
    patched = await client.patch(url, json={"settings": {"roles": {"storage": False}}})
    assert patched.json()["control"]["roles"] == {"jobs": True, "ai": False, "storage": False}
    assert (await client.patch(url, json={"settings": {"roles": {"mining": True}}})).status_code == 400
    assert (await client.patch(url, json={"settings": {"gpu_pct": "lots"}})).status_code == 400
    listed = (await client.get("/api/devices")).json()[0]
    assert listed["control"]["roles"]["storage"] is False


async def test_device_benchmark_and_notify(client: Any, device: FakeDevice) -> None:
    assert (await client.post("/api/devices/%s/benchmark" % device.device_id)).status_code == 200
    assert (
        await client.post(
            "/api/devices/%s/notify" % device.device_id,
            json={"title": "hi", "body": "there"},
        )
    ).status_code == 200
    commands = await device.poll()
    kinds = [c["cmd"] for c in commands]
    assert "benchmark" in kinds and "notify" in kinds
    assert (await client.post("/api/devices/nope/benchmark")).status_code == 404
    assert (
        await client.post("/api/devices/nope/notify", json={"title": "x", "body": "y"})
    ).status_code == 404


async def test_leave_marks_the_device_offline(client: Any, device: FakeDevice) -> None:
    assert (await device.leave()).status_code == 200
    detail = await client.get("/api/devices/%s" % device.device_id)
    assert detail.json()["status"] == "offline"


async def test_remove_device_sends_the_removed_command(client: Any, device: FakeDevice, cluster: Any) -> None:
    await client.delete("/api/devices/%s" % device.device_id)
    queue = cluster.queues[device.device_id]
    assert queue.get_nowait() == {"cmd": "removed"}
    assert (await client.get("/api/devices")).json() == []
    assert (await client.delete("/api/devices/nope")).status_code == 404


# --------------------------------------------------------------------------
# job lifecycle (SPEC §5, §9)
# --------------------------------------------------------------------------

async def test_job_runs_end_to_end_on_a_fake_device(client: Any, device: FakeDevice) -> None:
    async with running_device(device):
        job = await submit(
            client,
            type="python_map",
            name="doubler",
            params={"code": MAPPER, "items": [1, 2, 3, 4, 5], "chunk_size": 2},
        )
        done = await wait_for(lambda: finished(client, job["id"]), timeout=20)
    assert done is not None, "the job never finished"
    assert done["status"] == "done"
    assert done["result"] == [2, 4, 6, 8, 10]
    assert done["total_tasks"] == 3
    assert done["done_tasks"] == 3

    tasks = (await client.get("/api/jobs/%s/tasks" % job["id"])).json()
    assert [t["status"] for t in tasks] == ["done"] * 3
    assert all(t["device_id"] == device.device_id for t in tasks)
    assert all("payload" not in t for t in tasks)

    result = (await client.get("/api/jobs/%s/result" % job["id"])).json()
    assert result["result"] == [2, 4, 6, 8, 10]
    assert result["status"] == "done"

    single = (await client.get("/api/tasks/%s" % tasks[0]["id"])).json()
    assert single["idx"] == 0
    assert (await client.get("/api/tasks/nope")).status_code == 404


async def test_job_listing_and_filtering(client: Any, device: FakeDevice) -> None:
    async with running_device(device):
        job = await submit(client, type="python_map",
                           params={"code": MAPPER, "items": [1], "chunk_size": 1})
        await wait_for(lambda: finished(client, job["id"]), timeout=20)
    listing = await client.get("/api/jobs")
    assert [j["id"] for j in listing.json()] == [job["id"]]
    assert (await client.get("/api/jobs", params={"status": "done"})).json()
    assert (await client.get("/api/jobs", params={"status": "running"})).json() == []
    assert (await client.get("/api/jobs/nope")).status_code == 404


async def test_unknown_job_type_is_a_400(client: Any) -> None:
    response = await client.post("/api/jobs", json={"type": "nope", "params": {}})
    assert response.status_code == 400
    assert "nope" in response.json()["error"]


async def test_job_cancel_and_delete(client: Any, device: FakeDevice, cluster: Any) -> None:
    job = await submit(client, type="python_map",
                       params={"code": MAPPER, "items": [1, 2], "chunk_size": 1})
    await cluster.scheduler.tick()
    cancelled = await client.post("/api/jobs/%s/cancel" % job["id"])
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    tasks = (await client.get("/api/jobs/%s/tasks" % job["id"])).json()
    assert all(t["status"] == "cancelled" for t in tasks)

    assert (await client.delete("/api/jobs/%s" % job["id"])).status_code == 200
    assert (await client.get("/api/jobs/%s" % job["id"])).status_code == 404
    assert (await client.get("/api/jobs/%s/tasks" % job["id"])).status_code == 404
    assert (await client.post("/api/jobs/nope/cancel")).status_code == 404


async def test_job_retry_after_a_failure(client: Any, device: FakeDevice, cluster: Any) -> None:
    job = await submit(
        client,
        type="python_map",
        params={"code": "def fn(item, ctx):\n    raise RuntimeError('boom')\n",
                "items": [1], "chunk_size": 1},
        options={"max_attempts": 1},
    )
    async with running_device(device):
        done = await wait_for(lambda: finished(client, job["id"]), timeout=20)
    assert done["status"] == "failed"
    assert "boom" in done["error"]

    retried = await client.post("/api/jobs/%s/retry" % job["id"])
    assert retried.status_code == 200
    assert retried.json()["status"] == "running"
    assert (await client.post("/api/jobs/nope/retry")).status_code == 404


async def test_task_cancel_and_log(client: Any, device: FakeDevice, cluster: Any) -> None:
    job = await submit(client, type="python_map",
                       params={"code": MAPPER, "items": [1], "chunk_size": 1})
    await cluster.scheduler.tick()
    commands = await device.poll()
    task_id = commands[0]["task"]["id"]
    await device.start(task_id)
    await device.log(task_id, ["line one", "line two"])

    log = (await client.get("/api/tasks/%s/log" % task_id)).json()
    assert log["lines"] == ["line one", "line two"]
    assert (await client.get("/api/tasks/%s/log" % task_id, params={"tail": 1})).json()["lines"] == [
        "line two"
    ]

    assert (await client.post("/api/tasks/%s/cancel" % task_id)).status_code == 200
    assert (await client.get("/api/tasks/%s" % task_id)).json()["status"] == "cancelled"
    assert (await client.post("/api/tasks/nope/cancel")).status_code == 404


async def test_shell_job_targets_all(client: Any, make_device: Any) -> None:
    first = await make_device(name="one")
    second = await make_device(name="two")
    job = await submit(client, type="shell", params={"cmd": "echo hi"},
                       options={"targets": "all"})
    tasks = await wait_for(lambda: _two_tasks(client, job["id"]), timeout=10)
    assert tasks is not None and len(tasks) == 2
    assert {t["device_id"] for t in tasks} == {first.device_id, second.device_id}
    assert all("payload" not in t for t in tasks)  # payloads are not exposed


async def _two_tasks(client: Any, job_id: str) -> Any:
    tasks = (await client.get("/api/jobs/%s/tasks" % job_id)).json()
    return tasks if len(tasks) == 2 and all(t["device_id"] for t in tasks) else None


# --------------------------------------------------------------------------
# pipelines (SPEC §5.1)
# --------------------------------------------------------------------------

async def test_pipeline_with_from_job(client: Any, device: FakeDevice) -> None:
    async with running_device(device):
        response = await client.post(
            "/api/pipelines",
            json={
                "name": "chain",
                "jobs": [
                    {"key": "first", "type": "python_map",
                     "params": {"code": MAPPER, "items": [1, 2], "chunk_size": 2}},
                    {"key": "second", "type": "python_map", "depends_on": ["first"],
                     "params": {"code": MAPPER, "items": {"$from_job": "first"},
                                "chunk_size": 4}},
                ],
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert set(body["jobs"]) == {"first", "second"}
        second = await wait_for(lambda: finished(client, body["jobs"]["second"]), timeout=25)
    assert second["status"] == "done"
    assert second["result"] == [4, 8]  # [1,2] -> [2,4] -> [4,8]
    first = await job_of(client, body["jobs"]["first"])
    assert first["result"] == [2, 4]
    assert first["pipeline_id"] == body["pipeline_id"]


async def test_pipeline_rejects_an_unknown_type(client: Any) -> None:
    response = await client.post(
        "/api/pipelines", json={"jobs": [{"key": "a", "type": "nope", "params": {}}]}
    )
    assert response.status_code == 400


async def test_pipeline_needs_jobs(client: Any) -> None:
    assert (await client.post("/api/pipelines", json={"jobs": []})).status_code == 400


# --------------------------------------------------------------------------
# files (SPEC §10)
# --------------------------------------------------------------------------

async def test_files_multipart_and_raw_put(client: Any) -> None:
    multipart = await client.post(
        "/api/files", files={"file": ("hello.txt", b"hello there", "text/plain")}
    )
    assert multipart.status_code == 200
    meta = multipart.json()
    assert meta["name"] == "hello.txt"
    assert meta["size"] == 11
    assert len(meta["id"]) == 64

    raw = await client.put("/api/files", params={"name": "raw.bin"}, content=b"raw bytes")
    assert raw.status_code == 200
    assert raw.json()["name"] == "raw.bin"

    listing = (await client.get("/api/files")).json()
    assert {f["name"] for f in listing} == {"hello.txt", "raw.bin"}

    assert (await client.get("/api/files/%s" % meta["id"])).json()["size"] == 11
    download = await client.get("/api/files/%s/download" % meta["id"])
    assert download.content == b"hello there"
    assert "hello.txt" in download.headers["content-disposition"]

    assert (await client.delete("/api/files/%s" % meta["id"])).status_code == 200
    assert (await client.get("/api/files/%s" % meta["id"])).status_code == 404
    assert (await client.delete("/api/files/%s" % meta["id"])).status_code == 404
    assert (await client.get("/api/files/%s/download" % meta["id"])).status_code == 404


async def test_file_range_requests(client: Any, device: FakeDevice) -> None:
    """SPEC §6.6: workers fetch slices of big inputs with HTTP Range."""
    body = b"0123456789" * 10
    meta = (await client.put("/api/files", params={"name": "big.bin"}, content=body)).json()
    fid = meta["id"]
    part = await client.get("/api/w/files/%s" % fid, headers=dict(device.headers, Range="bytes=10-19"))
    assert part.status_code == 206
    assert part.content == body[10:20]
    assert part.headers["content-range"] == "bytes 10-19/100"
    assert part.headers["content-length"] == "10"
    tail = await client.get("/api/files/%s/download" % fid, headers={"Range": "bytes=-5"})
    assert tail.status_code == 206 and tail.content == body[-5:]
    open_ended = await client.get("/api/files/%s/download" % fid, headers={"Range": "bytes=95-"})
    assert open_ended.content == body[95:]
    beyond = await client.get("/api/files/%s/download" % fid, headers={"Range": "bytes=500-600"})
    assert beyond.status_code == 416
    whole = await client.get("/api/files/%s/download" % fid)
    assert whole.status_code == 200 and whole.content == body
    assert whole.headers["accept-ranges"] == "bytes"


async def test_lines_file_map_streams_input_and_output(client: Any, cluster: Any, device: FakeDevice) -> None:
    """lines_file splits a big file by byte ranges; stream_results writes JSONL."""
    lines = "".join("line %d\n" % i for i in range(25)).encode("utf-8")
    fid = (await client.put("/api/files", params={"name": "big.txt"}, content=lines)).json()["id"]
    job = await submit(
        client, type="python_map",
        params={"code": "def fn(item, ctx):\n    return item.upper()\n",
                "lines_file": fid, "lines_per_task": 10},
        options={"stream_results": True},
    )
    await cluster.scheduler.tick()
    tasks = cluster.db.select("tasks", "WHERE job_id=? ORDER BY idx", (job["id"],))
    assert len(tasks) == 3  # 10 + 10 + 5 lines
    rng = tasks[0]["payload"]["inputs"][-1]
    assert rng["offset"] == 0 and lines[rng["offset"]:rng["offset"] + rng["length"]].count(b"\n") == 10
    assert tasks[2]["payload"]["impls"]["python"]["args"]["start"] == 20

    # Play the worker: fetch each task's range with HTTP Range and run the code.
    from conftest import SimpleCtx, run_python_impl  # noqa: WPS433
    import os as _os  # noqa: WPS433
    import tempfile as _tempfile  # noqa: WPS433

    served = 0
    while served < 3:
        for cmd in await device.poll():
            if cmd.get("cmd") != "task":
                continue
            task = cmd["task"]
            entry = task["payload"]["inputs"][-1]
            res = await client.get(
                "/api/w/files/%s" % entry["file_id"],
                headers=dict(device.headers,
                             Range="bytes=%d-%d" % (entry["offset"], entry["offset"] + entry["length"] - 1)),
            )
            assert res.status_code == 206
            fd, path = _tempfile.mkstemp()
            _os.write(fd, res.content)
            _os.close(fd)
            ctx = SimpleCtx(task["id"], "fake", inputs=[path])
            out = run_python_impl(task["payload"]["impls"]["python"], ctx)
            await device.done(task["id"], ok=True, result=out, attempt=task["attempt"])
            served += 1

    done = await wait_for(lambda: finished(client, job["id"]), timeout=20)
    assert done["status"] == "done", done.get("error")
    assert done["result"]["count"] == 25
    streamed = await client.get("/api/jobs/%s/results" % job["id"])
    assert streamed.status_code == 200
    got = [json.loads(line) for line in streamed.text.splitlines()]
    assert got == ["LINE %d" % i for i in range(25)]
    # the results file is a normal file-store entry too
    listing = (await client.get("/api/files/%s" % done["result"]["results_file"])).json()
    assert listing["name"].endswith(".jsonl")


async def test_results_endpoint_for_plain_jobs(client: Any, cluster: Any, device: FakeDevice) -> None:
    job = await submit(client, type="python_map", params={"code": MAPPER, "items": [1, 2], "chunk_size": 2})
    early = await client.get("/api/jobs/%s/results" % job["id"])
    assert early.status_code == 204
    await cluster.scheduler.tick()
    command = (await device.poll())[0]
    await device.run_task(command)
    await wait_for(lambda: finished(client, job["id"]), timeout=10)
    res = await client.get("/api/jobs/%s/results" % job["id"])
    assert res.status_code == 200
    assert [json.loads(l) for l in res.text.splitlines()] == [2, 4]


async def test_worker_checkpoint_endpoint(client: Any, cluster: Any, device: FakeDevice) -> None:
    job = await submit(client, type="python_map", params={"code": MAPPER, "items": [1, 2, 3], "chunk_size": 3})
    await cluster.scheduler.tick()
    task = (await device.poll())[0]["task"]
    res = await client.post("/api/w/task/%s/checkpoint" % task["id"],
                            json={"state": {"n": 3, "out": [2]}, "attempt": task["attempt"]},
                            headers=device.headers)
    assert res.status_code == 200
    row = (await client.get("/api/tasks/%s" % task["id"])).json()
    assert row["checkpoint"] == {"n": 3, "out": [2]}
    stale = await client.post("/api/w/task/%s/checkpoint" % task["id"],
                              json={"state": {}, "attempt": 99}, headers=device.headers)
    assert stale.status_code == 409


async def test_same_content_is_stored_once(client: Any) -> None:
    first = await client.put("/api/files", params={"name": "a.txt"}, content=b"same")
    second = await client.put("/api/files", params={"name": "b.txt"}, content=b"same")
    assert first.json()["id"] == second.json()["id"]
    assert len((await client.get("/api/files")).json()) == 1


async def test_worker_file_endpoints(client: Any, device: FakeDevice) -> None:
    uploaded = await client.put(
        "/api/w/files", params={"name": "out.txt"}, content=b"from the worker",
        headers=device.headers,
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["name"] == "out.txt"
    file_id = uploaded.json()["file_id"]

    fetched = await client.get("/api/w/files/%s" % file_id, headers=device.headers)
    assert fetched.content == b"from the worker"
    assert "out.txt" in fetched.headers["content-disposition"]
    missing = await client.get("/api/w/files/%s" % ("a" * 64), headers=device.headers)
    assert missing.status_code == 404


async def test_python_map_from_an_items_file(client: Any, device: FakeDevice) -> None:
    upload = await client.put(
        "/api/files", params={"name": "items.json"}, content=json.dumps([3, 4]).encode()
    )
    async with running_device(device):
        job = await submit(
            client,
            type="python_map",
            params={"code": MAPPER, "items_file": upload.json()["id"], "chunk_size": 10},
        )
        done = await wait_for(lambda: finished(client, job["id"]), timeout=20)
    assert done["result"] == [6, 8]


# --------------------------------------------------------------------------
# templates and schedules (SPEC §11)
# --------------------------------------------------------------------------

async def test_templates_crud(client: Any) -> None:
    spec = {"type": "shell", "params": {"cmd": "uptime"}}
    created = await client.post("/api/templates", json={"name": "uptime", "job_spec": spec})
    assert created.status_code == 200
    template_id = created.json()["id"]
    assert created.json()["job_spec"] == spec

    listing = (await client.get("/api/templates")).json()
    assert [t["id"] for t in listing] == [template_id]
    assert (await client.delete("/api/templates/%s" % template_id)).status_code == 200
    assert (await client.get("/api/templates")).json() == []
    assert (await client.delete("/api/templates/%s" % template_id)).status_code == 404


async def test_schedules_crud_and_run(client: Any, device: FakeDevice) -> None:
    spec = {"type": "python_map",
            "params": {"code": MAPPER, "items": [5], "chunk_size": 1}}
    created = await client.post(
        "/api/schedules",
        json={"name": "nightly", "cron": "0 3 * * *", "job_spec": spec, "enabled": True},
    )
    assert created.status_code == 200
    schedule = created.json()
    assert schedule["cron"] == "0 3 * * *"
    assert schedule["next_run"] > 0
    assert schedule["last_run"] is None

    bad = await client.post(
        "/api/schedules", json={"name": "x", "cron": "not a cron", "job_spec": spec}
    )
    assert bad.status_code == 400

    patched = await client.patch(
        "/api/schedules/%s" % schedule["id"], json={"cron": "*/5 * * * *", "enabled": False}
    )
    assert patched.status_code == 200
    assert patched.json()["cron"] == "*/5 * * * *"
    assert patched.json()["next_run"] is None
    assert (
        await client.patch("/api/schedules/%s" % schedule["id"], json={"cron": "bad"})
    ).status_code == 400

    async with running_device(device):
        fired = await client.post("/api/schedules/%s/run" % schedule["id"])
        assert fired.status_code == 200
        job_id = fired.json()["job_id"]
        done = await wait_for(lambda: finished(client, job_id), timeout=20)
    assert done["status"] == "done"
    assert done["result"] == [10]
    assert "@" in done["name"]

    refreshed = (await client.get("/api/schedules")).json()[0]
    assert refreshed["last_run"] is not None
    events = (await client.get("/api/events", params={"kind": "schedule_fired"})).json()
    assert events and events[0]["job_id"] == job_id

    assert (await client.delete("/api/schedules/%s" % schedule["id"])).status_code == 200
    assert (await client.get("/api/schedules")).json() == []
    assert (await client.delete("/api/schedules/%s" % schedule["id"])).status_code == 404
    assert (await client.post("/api/schedules/nope/run")).status_code == 404
    assert (await client.patch("/api/schedules/nope", json={"name": "x"})).status_code == 404


async def test_schedule_tick_fires_due_jobs(client: Any, cluster: Any) -> None:
    created = await client.post(
        "/api/schedules",
        json={"name": "due", "cron": "* * * * *",
              "job_spec": {"type": "shell", "params": {"cmd": "echo hi"}}},
    )
    schedule_id = created.json()["id"]
    cluster.db.update("schedules", {"id": schedule_id}, {"next_run": 1.0})
    fired = await cluster.schedules.tick()
    assert len(fired) == 1
    assert cluster.db.get("schedules", schedule_id)["next_run"] > 1.0


async def test_schedule_with_a_broken_spec_records_an_error(client: Any, cluster: Any) -> None:
    cluster.db.insert(
        "schedules",
        {"id": "s1", "name": "broken", "cron": "* * * * *",
         "job_spec": {"type": "nope"}, "enabled": 1, "created": 0.0, "next_run": 1.0},
    )
    fired = await cluster.schedules.tick()
    assert fired == []
    kinds = [e["kind"] for e in (await client.get("/api/events")).json()]
    assert "error" in kinds


# --------------------------------------------------------------------------
# settings, events, stats, plugins
# --------------------------------------------------------------------------

async def test_settings_patch_is_persisted(client: Any, cluster: Any, data_dir: Any) -> None:
    current = (await client.get("/api/settings")).json()
    assert current["scheduler"]["min_battery"] == 20
    assert current["storage"]["chunk_mb"] == 4

    patched = await client.patch(
        "/api/settings",
        json={"scheduler": {"min_battery": 42}, "storage": {"replication": 3},
              "notifications": {"desktop": False, "on": ["job_failed"]}},
    )
    assert patched.status_code == 200
    assert patched.json()["scheduler"]["min_battery"] == 42
    assert patched.json()["storage"]["replication"] == 3
    assert cluster.setting("scheduler.min_battery") == 42
    assert cluster.db.get_setting("scheduler")["min_battery"] == 42

    import yaml

    saved = yaml.safe_load((data_dir / "config.yaml").read_text(encoding="utf-8"))
    assert saved["scheduler"]["min_battery"] == 42
    assert saved["storage"]["replication"] == 3
    assert saved["scheduler"]["max_temp"] == 40.0  # untouched keys survive


async def test_settings_can_change_the_pin(client: Any, anon: Any, cluster: Any) -> None:
    assert (await client.patch("/api/settings", json={"pin": "abcdef"})).status_code == 400
    response = await client.patch("/api/settings", json={"pin": "135790"})
    assert response.status_code == 200
    assert cluster.config.pin == "135790"
    assert (await anon.get("/api/devices", headers={"X-Pin": "135790"})).status_code == 200


async def test_saved_settings_are_reloaded(data_dir: Any, monkeypatch: Any) -> None:
    from coordinator.state import ClusterState

    first = ClusterState(data_dir)
    first.save_settings({"scheduler": {"min_battery": 11}, "pin": "246810"})
    first.db.close()

    second = ClusterState(data_dir)
    try:
        assert second.setting("scheduler.min_battery") == 11
        assert second.config.pin == "246810"
    finally:
        second.db.close()


async def test_events_listing(client: Any, device: FakeDevice) -> None:
    events = (await client.get("/api/events")).json()
    kinds = [e["kind"] for e in events]
    assert "device_joined" in kinds
    filtered = (await client.get("/api/events", params={"kind": "device_joined"})).json()
    assert filtered and all(e["kind"] == "device_joined" for e in filtered)
    assert len((await client.get("/api/events", params={"limit": 1})).json()) == 1


async def test_stats(client: Any, device: FakeDevice) -> None:
    stats = (await client.get("/api/stats")).json()
    assert stats["devices"]["online"] == 1
    assert stats["devices"]["total"] == 1
    assert stats["devices"]["cores"] == 4
    assert stats["devices"]["slots_free"] == 1
    assert set(stats["jobs"]) == {"running", "pending", "done_24h", "failed_24h"}
    assert stats["leaderboard"][0]["device_id"] == device.device_id
    assert "tasks_per_min" in stats


async def test_plugins_listing(client: Any) -> None:
    plugins = (await client.get("/api/plugins")).json()
    types = {p["type"] for p in plugins}
    assert {"python_map", "shell", "media", "hash_search", "monte_carlo_pi",
            "prime_range", "sensor", "benchmark", "custom"} <= types
    python_map = next(p for p in plugins if p["type"] == "python_map")
    assert any(param["name"] == "chunk_size" for param in python_map["params"])
    assert python_map["description"]


async def test_join_json_and_install_script(client: Any, anon: Any, app: Any) -> None:
    pin = app.state.cluster.config.pin
    info = (await client.get("/api/join")).json()
    assert "pin" not in info and "code" not in info and len(info["ca_fingerprint"]) == 64
    payload = (await client.post("/api/join/code")).json()
    code = payload["code"]
    assert len(code) == 9 and code[4] == "-" and pin not in str(payload)
    assert payload["install_cmd"].startswith('curl -fsSk --pinnedpubkey "sha256//')
    assert "/install.sh?code=%s" % code in payload["install_cmd"]
    assert payload["windows_cmd"].startswith(
        "powershell -NoProfile -Command \"iex ((curl.exe -fsSk --pinnedpubkey 'sha256//")
    assert "/install.ps1?code=%s' | Out-String) + ' ')\"" % code in payload["windows_cmd"]
    assert payload["browser_url"].endswith("/w?code=%s" % code)
    assert payload["manual_cmd"].endswith("--fp %s --join %s" % (info["ca_fingerprint"], code))
    assert payload["worker_url"].endswith("/worker.py")
    assert payload["qr_svg"].lstrip().startswith("<svg")
    assert "<?xml" not in payload["qr_svg"]
    # a second QR holds exactly the Termux command (scan, copy, paste)
    from coordinator.qr import qr_svg

    assert payload["install_qr_svg"] == qr_svg(payload["install_cmd"], error="L")
    import qrcode

    fit = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L)
    fit.add_data(payload["install_cmd"])
    fit.make(fit=True)
    assert fit.version <= 9  # at most 53x53 modules: easy to scan off a screen

    as_json = await anon.get("/join", headers={"Accept": "application/json"})
    assert as_json.headers["content-type"].startswith("application/json")
    assert as_json.json()["code"] != code and "pin" not in as_json.json()

    as_html = await anon.get("/join")
    assert as_html.headers["content-type"].startswith("text/html")
    assert pin not in as_html.text and as_html.text.count("<svg") == 2  # browser + Termux QR
    assert "/install.ps1?code=" in as_html.text and "Windows PC or laptop" in as_html.text

    script = await anon.get("/install.sh", params={"code": code})
    assert script.status_code == 200
    assert pin not in script.text and code in script.text
    assert "-----BEGIN CERTIFICATE-----" in script.text and "--pinnedpubkey" in script.text
    assert "http://cluster.test/worker.py" in script.text
    assert script.text.startswith("#!/bin/sh")


async def test_dashboard_and_worker_assets(anon: Any) -> None:
    index = await anon.get("/")
    assert index.status_code == 200
    assert "PhoneCluster" in index.text
    assert '/static/app.js' in index.text
    for asset in ("app.js", "api.js", "ws.js", "util.js", "views.js", "views2.js",
                  "jobform.js", "style.css"):
        response = await anon.get("/static/%s" % asset)
        assert response.status_code == 200, asset
        assert response.content
    worker = await anon.get("/worker.py")
    assert b"PhoneCluster worker" in worker.content
    assert (await anon.get("/w")).status_code == 200


# --------------------------------------------------------------------------
# export (SPEC §11)
# --------------------------------------------------------------------------

async def test_export_json_csv_and_zip(client: Any, device: FakeDevice) -> None:
    async with running_device(device):
        job = await submit(
            client,
            type="python_map",
            name="exported",
            params={"code": MAPPER, "items": [1, 2], "chunk_size": 1},
        )
        await wait_for(lambda: finished(client, job["id"]), timeout=20)
        tasks = (await client.get("/api/jobs/%s/tasks" % job["id"])).json()
        await device.log(tasks[0]["id"], ["hello from the task"])

    as_json = await client.get("/api/jobs/%s/export" % job["id"], params={"format": "json"})
    assert as_json.status_code == 200
    body = as_json.json()
    assert body["job"]["id"] == job["id"]
    assert body["result"] == [2, 4]
    assert len(body["tasks"]) == 2
    assert "%s.json" % job["id"] in as_json.headers["content-disposition"]

    as_csv = await client.get("/api/jobs/%s/export" % job["id"], params={"format": "csv"})
    assert as_csv.status_code == 200
    rows = list(csv.reader(io.StringIO(as_csv.text)))
    assert rows[0][:4] == ["idx", "task_id", "device_id", "status"]
    assert len(rows) == 3
    assert rows[1][3] == "done"

    as_zip = await client.get("/api/jobs/%s/export" % job["id"], params={"format": "zip"})
    assert as_zip.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(as_zip.content))
    names = archive.namelist()
    assert "result.json" in names
    assert "tasks.csv" in names
    assert "job.json" in names
    assert any(n.startswith("logs/") for n in names)
    assert json.loads(archive.read("result.json"))["result"] == [2, 4]

    assert (
        await client.get("/api/jobs/%s/export" % job["id"], params={"format": "xml"})
    ).status_code == 400
    assert (await client.get("/api/jobs/nope/export")).status_code == 404


async def test_export_zip_contains_outputs(client: Any, device: FakeDevice, cluster: Any) -> None:
    upload = await client.put(
        "/api/w/files", params={"name": "result.bin"}, content=b"produced",
        headers=device.headers,
    )
    file_id = upload.json()["file_id"]
    job = await submit(client, type="python_map",
                       params={"code": MAPPER, "items": [1], "chunk_size": 1})
    await cluster.scheduler.tick()
    command = (await device.poll())[0]
    await device.done(
        command["task"]["id"], ok=True, result=[2],
        outputs=[{"file_id": file_id, "name": "result.bin", "size": 8}],
    )
    await wait_for(lambda: finished(client, job["id"]), timeout=10)

    as_zip = await client.get("/api/jobs/%s/export" % job["id"], params={"format": "zip"})
    archive = zipfile.ZipFile(io.BytesIO(as_zip.content))
    outputs = [n for n in archive.namelist() if n.startswith("outputs/")]
    assert outputs
    assert archive.read(outputs[0]) == b"produced"


# --------------------------------------------------------------------------
# terminal (SPEC §10)
# --------------------------------------------------------------------------

async def test_terminal_open_input_and_close(client: Any, device: FakeDevice, cluster: Any) -> None:
    opened = await client.post("/api/terminal", json={"device_id": device.device_id})
    assert opened.status_code == 200
    session_id = opened.json()["session_id"]
    assert opened.json()["device_id"] == device.device_id

    commands = await device.poll()
    assert commands[0]["cmd"] == "term_open"
    assert commands[0]["session_id"] == session_id

    assert cluster.terminals.input(session_id, "echo hello\n") is True
    assert (await device.poll())[0] == {
        "cmd": "term_input", "session_id": session_id, "data": "echo hello\n"
    }
    await device.term_output(session_id, "hello\n")
    # the scrollback keeps the command typed as well as what came back
    assert cluster.terminals.get(session_id).buffer == ["$ echo hello\n", "hello\n"]

    assert (await client.get("/api/terminal")).json()[0]["session_id"] == session_id
    assert (await client.delete("/api/terminal/%s" % session_id)).status_code == 200
    assert (await device.poll())[0]["cmd"] == "term_close"
    assert (await client.delete("/api/terminal/%s" % session_id)).status_code == 404
    assert cluster.terminals.input(session_id, "x") is False


async def test_terminal_errors(client: Any, make_device: Any) -> None:
    assert (await client.post("/api/terminal", json={"device_id": "nope"})).status_code == 404
    browser = await make_device(name="chrome", platform="browser", caps=["js", "browser"])
    response = await client.post("/api/terminal", json={"device_id": browser.device_id})
    assert response.status_code == 400
    assert "browser" in response.json()["error"]


async def test_terminal_closed_by_the_worker(client: Any, device: FakeDevice, cluster: Any) -> None:
    opened = await client.post("/api/terminal", json={"device_id": device.device_id})
    session_id = opened.json()["session_id"]
    await device.term_closed(session_id, 0)
    assert cluster.terminals.get(session_id) is None


async def test_terminal_scrollback_lets_the_page_come_back(
    client: Any, device: FakeDevice, cluster: Any, monkeypatch: Any
) -> None:
    """Leaving the Terminal page keeps the shell; coming back replays it (SPEC §10)."""
    sent: List[Dict[str, Any]] = []

    async def capture(message: Dict[str, Any]) -> None:
        sent.append(message)

    monkeypatch.setattr(cluster.hub, "broadcast", capture)
    opened = await client.post("/api/terminal", json={"device_id": device.device_id})
    session_id = opened.json()["session_id"]
    await device.poll()
    await device.term_output(session_id, "Microsoft Windows\n")
    assert cluster.terminals.input(session_id, "cd data\n") is True
    await device.term_output(session_id, "D:\\data>\n")

    back = await client.get("/api/terminal/%s" % session_id)
    assert back.status_code == 200
    body = back.json()
    assert body["device_id"] == device.device_id and body["closed"] is False
    assert body["output"] == "Microsoft Windows\n$ cd data\nD:\\data>\n"
    assert body["pos"] == len(body["output"])
    # live output carries the same counter, so a page can skip what it replayed
    live = [m for m in sent if m.get("ev") == "term"]
    assert [m["pos"] for m in live] == [len("Microsoft Windows\n"), body["pos"]]

    assert (await client.get("/api/terminal/nope")).status_code == 404
    assert (await client.delete("/api/terminal/%s" % session_id)).status_code == 200
    assert (await client.get("/api/terminal/%s" % session_id)).status_code == 404


async def test_terminal_scrollback_counts_what_scrolled_away(device: FakeDevice, cluster: Any) -> None:
    from coordinator.terminal import MAX_SCROLLBACK

    session = cluster.terminals.open(device.device_id)
    for i in range(MAX_SCROLLBACK + 5):
        session.append("%d\n" % i)
    view = session.scrollback()
    assert view["output"].startswith("5\n")
    assert view["pos"] == sum(len("%d\n" % i) for i in range(MAX_SCROLLBACK + 5))


async def test_pages_hear_when_a_shell_ends_without_its_worker(
    client: Any, device: FakeDevice, cluster: Any, monkeypatch: Any
) -> None:
    published: List[Dict[str, Any]] = []
    monkeypatch.setattr(cluster.hub, "publish", published.append)
    first = (await client.post("/api/terminal", json={"device_id": device.device_id})).json()["session_id"]
    second = (await client.post("/api/terminal", json={"device_id": device.device_id})).json()["session_id"]
    import time as _time

    cluster.terminals.get(first).last = _time.time() - 3600
    assert cluster.terminals.reap() == [first]
    assert cluster.terminals.drop_device(device.device_id) == [second]
    assert [m for m in published if m.get("ev") == "term_closed"] == [
        {"ev": "term_closed", "session_id": first, "exit_code": None},
        {"ev": "term_closed", "session_id": second, "exit_code": None},
    ]


async def test_terminals_are_reaped_when_idle(client: Any, device: FakeDevice, cluster: Any) -> None:
    opened = await client.post("/api/terminal", json={"device_id": device.device_id})
    session_id = opened.json()["session_id"]
    import time as _time

    assert cluster.terminals.reap(_time.time() + 10) == []
    assert cluster.terminals.reap(_time.time() + 3600) == [session_id]
    assert cluster.terminals.get(session_id) is None


# --------------------------------------------------------------------------
# websocket hub (the socket itself is exercised in test_e2e.py)
# --------------------------------------------------------------------------

async def test_hub_snapshot_and_log_subscription(cluster: Any, device: FakeDevice) -> None:
    snapshot = await cluster.hub.snapshot()
    assert snapshot["ev"] == "snapshot"
    assert [d["id"] for d in snapshot["devices"]] == [device.device_id]
    assert "stats" in snapshot and "events" in snapshot

    class Sink:
        def __init__(self) -> None:
            self.messages: List[Dict[str, Any]] = []
            self.log_subs: set = set()

        async def send(self, message: Dict[str, Any]) -> bool:
            self.messages.append(message)
            return True

    sink = Sink()
    cluster.hub.connections.append(sink)
    await cluster.hub.broadcast({"ev": "test"})
    assert sink.messages[-1] == {"ev": "test"}

    cluster.append_task_log("task1", ["old line"])
    await cluster.hub.handle_client_message(sink, json.dumps({"op": "sub_logs", "task_id": "task1"}))
    assert sink.messages[-1] == {"ev": "log", "task_id": "task1", "lines": ["old line"]}
    await cluster.hub.send_log("task1", ["new line"])
    assert sink.messages[-1]["lines"] == ["new line"]

    await cluster.hub.handle_client_message(sink, json.dumps({"op": "unsub_logs", "task_id": "task1"}))
    await cluster.hub.send_log("task1", ["ignored"])
    assert sink.messages[-1]["lines"] == ["new line"]

    await cluster.hub.handle_client_message(sink, "not json")
    await cluster.hub.handle_client_message(sink, json.dumps({"op": "ping"}))
    assert sink.messages[-1] == {"ev": "pong"}
    cluster.hub.remove(sink)
    assert sink not in cluster.hub.connections
