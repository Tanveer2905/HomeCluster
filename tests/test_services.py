"""Services: registry + request relay (coordinator/services.py, api_services.py)."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import httpx
import pytest

from conftest import FakeDevice, wait_for

pytestmark = pytest.mark.asyncio

SERVICE_CODE = "def load(ctx):\n    return 'model'\n\ndef serve(request, ctx):\n    return {'n': len(request.read())}\n"


async def submit_service(client: httpx.AsyncClient, name: str = "echo", **extra: Any) -> Dict[str, Any]:
    spec = {"type": "service", "name": "svc " + name,
            "params": dict({"name": name, "code": SERVICE_CODE, "request_timeout": 30}, **extra)}
    res = await client.post("/api/jobs", json=spec)
    assert res.status_code == 200, res.text
    return res.json()


async def bring_up(client: httpx.AsyncClient, cluster: Any, device: FakeDevice, name: str = "echo") -> Dict[str, Any]:
    """Submit a service job and play the device up to 'ready'. Returns the task command."""
    job = await submit_service(client, name)
    await cluster.scheduler.tick()
    cmds = await device.poll()
    task = [c for c in cmds if c.get("cmd") == "task"][0]["task"]
    assert task["payload"]["service"]["name"] == name
    assert task["payload"]["timeout"] == 0
    await device.start(task["id"])
    res = await client.post("/api/w/service/%s/ready" % task["id"], json={"name": name, "meta": {}},
                            headers=device.headers)
    assert res.status_code == 200, res.text
    task["job_id"] = job["id"]
    return task


async def serve_one(client: httpx.AsyncClient, device: FakeDevice, partials: List[Any] = (),
                    result: Any = None, ok: bool = True) -> Dict[str, Any]:
    """Play the device side of one request: pull input, post partials, post done."""
    cmd = None
    for _ in range(40):
        for c in await device.poll():
            if c.get("cmd") == "service_request":
                cmd = c
        if cmd:
            break
    assert cmd is not None, "device never received the request"
    rid = cmd["request"]["id"]
    got = await client.get("/api/w/requests/%s/input" % rid, headers=device.headers)
    assert got.status_code == 200
    for p in partials:
        await client.post("/api/w/requests/%s/partial" % rid, json={"data": p}, headers=device.headers)
    body = {"ok": ok, "result": result if result is not None else {"n": len(got.content)},
            "error": None if ok else "boom", "outputs": [], "ms": 5}
    done = await client.post("/api/w/requests/%s/done" % rid, json=body, headers=device.headers)
    assert done.status_code == 200, done.text
    return {"id": rid, "input": got.content, "content_type": got.headers.get("content-type")}


# --------------------------------------------------------------------------

async def test_service_task_is_only_for_service_capable_devices(client: httpx.AsyncClient, cluster: Any, make_device: Any) -> None:
    browser = await make_device(name="tab", platform="browser", caps=["js", "browser", "python"])
    await submit_service(client)
    await cluster.scheduler.tick()
    assert [c for c in await browser.poll() if c.get("cmd") == "task"] == []
    phone = await make_device(name="phone", caps=["python", "shell", "service", "termux"])
    await cluster.scheduler.tick()
    assert any(c.get("cmd") == "task" for c in await phone.poll())


async def test_ready_registers_an_instance(client: httpx.AsyncClient, cluster: Any, device: FakeDevice) -> None:
    assert (await client.get("/api/services")).json() == []
    task = await bring_up(client, cluster, device)
    services = (await client.get("/api/services")).json()
    assert len(services) == 1
    svc = services[0]
    assert svc["name"] == "echo" and svc["ready"] == 1 and svc["busy"] == 0
    inst = svc["instances"][0]
    assert inst["task_id"] == task["id"] and inst["device_name"] == device.name
    # ready from a device that does not hold the task is refused
    other = cluster.db.get("devices", device.device_id)
    res = await client.post("/api/w/service/%s/ready" % "nope", json={"name": "x"}, headers=device.headers)
    assert res.status_code == 409


async def test_call_relays_bytes_partials_and_result(client: httpx.AsyncClient, cluster: Any, device: FakeDevice) -> None:
    await bring_up(client, cluster, device)
    payload = b"hello " * 1000

    async def call() -> httpx.Response:
        return await client.post("/api/services/echo/call", content=payload,
                                 headers={"Content-Type": "audio/wav", "X-File-Name": "a.wav"},
                                 params={"params": json.dumps({"lang": "en"}), "request_id": "req-1"})

    caller = asyncio.create_task(call())
    served = await serve_one(client, device, partials=["hel", "hello"], result={"text": "hello"})
    res = await caller
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "done" and body["result"] == {"text": "hello"}
    assert body["partials"] == ["hel", "hello"] and body["id"] == "req-1"
    assert served["input"] == payload and served["content_type"].startswith("audio/wav")
    # the device saw the params and name
    detail = (await client.get("/api/requests/req-1")).json()
    assert detail["name"] == "a.wav" and detail["bytes_in"] == len(payload)
    # instance stats and free again
    svc = (await client.get("/api/services")).json()[0]
    assert svc["ready"] == 1 and svc["instances"][0]["requests_done"] == 1
    # request id reuse is refused
    dup = await client.post("/api/services/echo/call", content=b"x", params={"request_id": "req-1"})
    assert dup.status_code == 409


async def test_multipart_and_json_calls(client: httpx.AsyncClient, cluster: Any, device: FakeDevice) -> None:
    await bring_up(client, cluster, device)
    caller = asyncio.create_task(client.post(
        "/api/services/echo/call",
        files={"file": ("pic.png", b"\x89PNG....", "image/png")},
        data={"params": json.dumps({"top": 3})}))
    served = await serve_one(client, device)
    assert (await caller).status_code == 200
    assert served["input"] == b"\x89PNG...." and served["content_type"].startswith("image/png")

    caller = asyncio.create_task(client.post("/api/services/echo/call", json={"text": "bonjour"}))
    served = await serve_one(client, device)
    assert (await caller).status_code == 200
    assert json.loads(served["input"]) == {"text": "bonjour"}


async def test_partials_long_poll(client: httpx.AsyncClient, cluster: Any, device: FakeDevice) -> None:
    await bring_up(client, cluster, device)
    caller = asyncio.create_task(client.post("/api/services/echo/call", content=b"abc",
                                             params={"request_id": "lp"}))
    # wait until the device has it, then watch partials on a second connection
    cmd = None
    while cmd is None:
        for c in await device.poll():
            if c.get("cmd") == "service_request":
                cmd = c
    await client.get("/api/w/requests/lp/input", headers=device.headers)
    watcher = asyncio.create_task(client.get("/api/requests/lp/partials", params={"since": 0, "wait": 5}))
    await asyncio.sleep(0.1)
    await client.post("/api/w/requests/lp/partial", json={"data": "first"}, headers=device.headers)
    seen = (await watcher).json()
    assert seen["partials"] == ["first"] and seen["next"] == 1 and seen["status"] == "running"
    await client.post("/api/w/requests/lp/done", json={"ok": True, "result": "fin"}, headers=device.headers)
    final = (await client.get("/api/requests/lp/partials", params={"since": 1})).json()
    assert final["status"] == "done" and final["result"] == "fin" and final["partials"] == []
    assert (await caller).json()["result"] == "fin"


async def test_second_call_waits_for_a_free_instance(client: httpx.AsyncClient, cluster: Any, device: FakeDevice) -> None:
    await bring_up(client, cluster, device)
    first = asyncio.create_task(client.post("/api/services/echo/call", content=b"1", params={"request_id": "a"}))
    second = asyncio.create_task(client.post("/api/services/echo/call", content=b"2",
                                             params={"request_id": "b", "queue_timeout": 10}))
    served_a = await serve_one(client, device)
    assert served_a["id"] == "a"
    assert (await first).status_code == 200
    served_b = await serve_one(client, device)
    assert served_b["id"] == "b"
    assert (await second).status_code == 200
    # with no patience, a busy service answers 503
    third = asyncio.create_task(client.post("/api/services/echo/call", content=b"3", params={"request_id": "c"}))
    await wait_for(lambda: (cluster.services.requests.get("c") or type("x", (), {"status": ""})).status == "sending", timeout=5)
    fourth = await client.post("/api/services/echo/call", content=b"4", params={"queue_timeout": 0})
    assert fourth.status_code == 503
    await serve_one(client, device)
    await third


async def test_unknown_service_is_404(client: httpx.AsyncClient) -> None:
    res = await client.post("/api/services/nothing/call", content=b"x")
    assert res.status_code == 404


async def test_failed_request_and_instance_loss(client: httpx.AsyncClient, cluster: Any, device: FakeDevice) -> None:
    task = await bring_up(client, cluster, device)
    caller = asyncio.create_task(client.post("/api/services/echo/call", content=b"x", params={"request_id": "f"}))
    await serve_one(client, device, ok=False)
    res = await caller
    assert res.status_code == 502 and res.json()["error"] == "boom"

    # a request in flight when the service task ends fails cleanly
    caller = asyncio.create_task(client.post("/api/services/echo/call", content=b"y", params={"request_id": "g"}))
    cmd = None
    while cmd is None:
        for c in await device.poll():
            if c.get("cmd") == "service_request":
                cmd = c
    await client.post("/api/jobs/%s/cancel" % task["job_id"])
    res = await caller
    assert res.status_code == 502 and "went away" in res.json()["error"]
    assert (await client.get("/api/services")).json() == []


async def test_cancel_request(client: httpx.AsyncClient, cluster: Any, device: FakeDevice) -> None:
    await bring_up(client, cluster, device)
    caller = asyncio.create_task(client.post("/api/services/echo/call", content=b"z", params={"request_id": "c1"}))
    cmd = None
    while cmd is None:
        for c in await device.poll():
            if c.get("cmd") == "service_request":
                cmd = c
    res = await client.post("/api/requests/c1/cancel")
    assert res.json()["status"] == "cancelled"
    assert (await caller).status_code == 502
    # the device is told and the instance is free again
    cancels = [c for c in await device.poll() if c.get("cmd") == "service_cancel"]
    assert cancels and cancels[0]["request_id"] == "c1"
    assert (await client.get("/api/services")).json()[0]["ready"] == 1


async def test_device_token_may_call_services(client: httpx.AsyncClient, cluster: Any, device: FakeDevice, anon: httpx.AsyncClient) -> None:
    await bring_up(client, cluster, device)
    assert (await anon.get("/api/services")).status_code == 401
    listing = await anon.get("/api/services", headers=device.headers)
    assert listing.status_code == 200 and listing.json()[0]["name"] == "echo"
    caller = asyncio.create_task(anon.post("/api/services/echo/call", content=b"tok", headers=device.headers,
                                           params={"request_id": "t1"}))
    await serve_one(client, device)
    assert (await caller).status_code == 200


async def test_lease_is_refreshed_while_the_device_reports_the_task(client: httpx.AsyncClient, cluster: Any, device: FakeDevice) -> None:
    import time as _time

    task = await bring_up(client, cluster, device)
    cluster.db.update("tasks", {"id": task["id"]}, {"lease_expires": 1.0})  # long expired
    await device.poll(running=[task["id"]])
    refreshed = cluster.db.get("tasks", task["id"])
    assert refreshed["lease_expires"] > _time.time() + 30  # extended past now
    assert cluster.scheduler.expire_leases() == []
    # a task the device no longer reports does expire (the background tick or this call)
    cluster.db.update("tasks", {"id": task["id"]}, {"lease_expires": 1.0})
    await device.poll(running=[])
    cluster.scheduler.expire_leases()
    cluster.scheduler.assign()  # requeued, then handed out again (attempt 2)
    row = cluster.db.get("tasks", task["id"])
    assert row["attempts"] == 2 and row["status"] in ("queued", "assigned")
    assert (await client.get("/api/services")).json() == []  # instance forgotten until it says ready again
