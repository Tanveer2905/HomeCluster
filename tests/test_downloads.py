"""Saving into the project's data/downloads folder (SPEC §10): the dashboard's Save buttons.

The PC writes the file itself, so nothing goes to the browser's Downloads folder."""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path
from typing import Any, Dict

import httpx
import pytest

from conftest import running_device, wait_for
from coordinator import downloads as downloads_module
from coordinator.downloads import safe_name

MAPPER = "def fn(item, ctx):\n    return item * 2\n"


def test_names_stay_inside_the_folder_and_valid_on_windows() -> None:
    assert safe_name("clip.mp4") == "clip.mp4"
    assert safe_name("..\\..\\Windows\\evil.exe") == "evil.exe"
    assert safe_name("../../etc/passwd") == "passwd"
    assert safe_name('a<b>c:d"e|f?g*h.txt') == "a_b_c_d_e_f_g_h.txt"
    assert safe_name("CON.txt") == "_CON.txt" and safe_name("nul") == "_nul"
    assert safe_name("  .hidden. ") == "hidden"
    assert safe_name("") == "download" and safe_name(None) == "download"
    long = safe_name("x" * 300 + ".mp4")
    assert len(long) == 150 and long.endswith(".mp4")


@pytest.mark.asyncio
async def test_a_stored_file_is_saved_into_data_downloads(client: Any, cluster: Any) -> None:
    file_id = (await client.put("/api/files", params={"name": "clip.mp4"}, content=b"video bytes")).json()["id"]
    folder = cluster.data / "downloads"
    first = (await client.post("/api/files/%s/save" % file_id)).json()
    assert first["name"] == "clip.mp4" and Path(first["path"]) == folder / "clip.mp4"
    assert first["size"] == 11 and Path(first["folder"]) == folder
    assert (folder / "clip.mp4").read_bytes() == b"video bytes"
    # the same name again keeps the earlier files
    again = [(await client.post("/api/files/%s/save" % file_id)).json()["name"] for _ in range(2)]
    assert again == ["clip (1).mp4", "clip (2).mp4"]
    # a job output keeps the name the job gave it, but never leaves the folder
    named = await client.post("/api/files/%s/save" % file_id, json={"name": "..\\..\\out.mp4"})
    assert named.json()["name"] == "out.mp4"
    assert sorted(p.name for p in folder.iterdir()) == ["clip (1).mp4", "clip (2).mp4", "clip.mp4", "out.mp4"]
    assert (await client.post("/api/files/%s/save" % ("f" * 64))).status_code == 404


@pytest.mark.asyncio
async def test_a_job_export_is_saved(client: Any, device: Any, cluster: Any) -> None:
    upload = await client.put("/api/w/files", params={"name": "result.bin"}, content=b"produced",
                              headers=device.headers)
    file_id = upload.json()["file_id"]
    job = (await client.post("/api/jobs", json={"type": "python_map", "name": "exported", "params": {
        "code": MAPPER, "items": [1], "chunk_size": 1}})).json()
    await cluster.scheduler.tick()
    command = (await device.poll())[0]
    await device.done(command["task"]["id"], ok=True, result=[2],
                      outputs=[{"file_id": file_id, "name": "result.bin", "size": 8}])

    async def done() -> Any:
        found = (await client.get("/api/jobs/%s" % job["id"])).json()
        return found if found["status"] == "done" else None

    await wait_for(done, timeout=10)
    folder = cluster.data / "downloads"
    saved: Dict[str, Any] = {}
    for fmt in ("json", "csv", "zip"):
        reply = await client.post("/api/jobs/%s/export/save" % job["id"], params={"format": fmt})
        assert reply.status_code == 200, reply.text
        saved[fmt] = reply.json()
        assert saved[fmt]["name"] == "exported-%s.%s" % (job["id"][:8], fmt)
    assert json.loads((folder / saved["json"]["name"]).read_text())["result"] == [2]
    assert (folder / saved["csv"]["name"]).read_text().startswith("idx,task_id,device_id,status")
    with zipfile.ZipFile(folder / saved["zip"]["name"]) as archive:
        assert archive.read("outputs/0_result.bin") == b"produced"
        assert json.loads(archive.read("result.json"))["result"] == [2]
    # the GET export still answers the SDK and CLI the same way
    as_zip = await client.get("/api/jobs/%s/export" % job["id"], params={"format": "zip"})
    assert zipfile.ZipFile(io.BytesIO(as_zip.content)).read("outputs/0_result.bin") == b"produced"
    assert (await client.post("/api/jobs/%s/export/save" % job["id"], params={"format": "xml"})).status_code == 400
    assert (await client.post("/api/jobs/nope/export/save")).status_code == 404


@pytest.mark.asyncio
async def test_a_pooled_file_is_read_back_from_the_phones(cluster: Any, client: Any, make_device: Any) -> None:
    cluster.config.set("storage", {"chunk_mb": 1, "replication": 1, "cache_mb": 512, "include_pc": False})
    phone = await make_device(name="phone1", caps=["python", "shell", "termux", "storage"])
    phone.storage_free_mb = 8192
    await phone.poll()
    data = os.urandom(int(2.5 * 1024 * 1024))
    folder = cluster.data / "downloads"
    async with running_device(phone):
        blob = await cluster.storage.put_bytes(data, "holiday.mp4", replication=1, wait=True)
        for path in cluster.storage.cache_dir.glob("*"):
            path.unlink()  # only the phone has it now
        saved = await client.post("/api/storage/%s/save" % blob["id"])
        assert saved.status_code == 200, saved.text
    assert saved.json()["name"] == "holiday.mp4" and (folder / "holiday.mp4").read_bytes() == data
    # the phone is gone: a clear error, and no half-written file is left behind
    for path in cluster.storage.cache_dir.glob("*"):
        path.unlink()
    cluster.db.update("devices", {"id": phone.device_id}, {"status": "offline"})
    failed = await client.post("/api/storage/%s/save" % blob["id"])
    assert failed.status_code == 409 and "phones" in failed.json()["error"]
    assert sorted(p.name for p in folder.iterdir()) == ["holiday.mp4"]
    assert (await client.post("/api/storage/nope/save")).status_code == 404


@pytest.mark.asyncio
async def test_open_shows_the_folder_on_the_pc(client: Any, cluster: Any, monkeypatch: Any) -> None:
    shown = []
    monkeypatch.setattr(downloads_module, "reveal", shown.append)
    folder = cluster.data / "downloads"
    assert (await client.post("/api/downloads/open")).json()["folder"] == str(folder)
    file_id = (await client.put("/api/files", params={"name": "a.txt"}, content=b"a")).json()["id"]
    await client.post("/api/files/%s/save" % file_id)
    await client.post("/api/downloads/open", json={"name": "a.txt"})
    await client.post("/api/downloads/open", json={"name": "not-there.txt"})
    assert shown == [folder, folder / "a.txt", folder]


@pytest.mark.asyncio
async def test_only_this_pc_can_save_or_open(client: Any, app: Any) -> None:
    transport = httpx.ASGITransport(app=app, client=("192.168.1.50", 40000))
    async with httpx.AsyncClient(transport=transport, base_url="http://cluster.test") as outsider:
        for path in ("/api/files/%s/save" % ("f" * 64), "/api/storage/x/save",
                     "/api/jobs/x/export/save", "/api/downloads/open"):
            assert (await outsider.post(path, headers={"X-Pin": app.state.cluster.config.pin})).status_code == 403
