"""Workers that update themselves (SPEC §8.4).

The coordinator offers its worker.py to idle workers that run another one; a
worker checks the download, test-starts it and swaps it in.  The real round
trip (a worker process replacing its own file and coming back) is in
test_portable_e2e.py."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from conftest import ROOT

WORKER_PY = ROOT / "worker" / "worker.py"
OLD = "0" * 64


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------
# the coordinator's side
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_idle_worker_with_an_older_file_is_offered_the_new_one(cluster: Any, make_device: Any) -> None:
    want = sha(WORKER_PY.read_bytes())
    phone = await make_device(name="old-phone", info={"worker_sha256": OLD})
    device = lambda: cluster.db.get("devices", phone.device_id)  # noqa: E731
    assert cluster.updates.view(device()) == {"state": "due"}
    assert await phone.poll() == [{"cmd": "update", "sha256": want, "size": WORKER_PY.stat().st_size}]
    assert cluster.updates.view(device()) == {"state": "updating"}
    assert await phone.poll() == []  # not offered again while it restarts
    # it comes back running the new file
    await phone.register(device_id=phone.device_id, token=phone.token, info={"worker_sha256": want})
    assert cluster.updates.view(device()) == {"state": "current"}
    assert [e["message"] for e in cluster.db.select("events", "WHERE kind='worker_updated'")] == [
        "old-phone now runs the new worker.py"]


@pytest.mark.asyncio
async def test_a_busy_worker_waits(cluster: Any, make_device: Any) -> None:
    phone = await make_device(name="busy-phone", info={"worker_sha256": OLD})
    assert await phone.poll(running=["some-task"]) == []      # it says it is running something
    cluster.terminals.open(phone.device_id)                   # someone has a shell open on it
    view = cluster.updates.view(cluster.db.get("devices", phone.device_id))
    assert view == {"state": "waiting", "reason": "a terminal is open"}
    commands = await phone.poll()
    assert [c["cmd"] for c in commands] == ["term_open"]


@pytest.mark.asyncio
async def test_up_to_date_old_and_browser_workers_get_no_offer(cluster: Any, client: Any, make_device: Any) -> None:
    current = await make_device(name="new", info={"worker_sha256": sha(WORKER_PY.read_bytes())})
    legacy = await make_device(name="legacy")  # from before self-updates: reports nothing
    tab = await make_device(name="tab", platform="browser", caps=["js"], info={"worker_sha256": OLD})
    assert await current.poll() == [] and await legacy.poll() == []
    assert cluster.updates.view(cluster.db.get("devices", legacy.device_id)) == {"state": "by_hand"}
    devices = {d["name"]: d for d in (await client.get("/api/devices")).json()}
    assert devices["new"]["update"] == {"state": "current"} and devices["tab"]["update"] is None
    # a browser tab gets the page version instead, and reloads itself when it changes
    body = {"status": {"free_slots": 1, "running": []}}
    answer = (await client.post("/api/w/poll", json=body, headers=tab.headers)).json()
    assert answer["commands"] == [] and answer["page"] == cluster.updates.page_version()
    assert len(answer["page"]) == 16
    assert "page" not in (await client.post("/api/w/poll", json=body, headers=current.headers)).json()


@pytest.mark.asyncio
async def test_a_name_given_on_the_dashboard_survives_restarts(cluster: Any, client: Any, make_device: Any) -> None:
    """Every update restarts the worker, which joins again with its own name."""
    phone = await make_device(name="localhost")
    attach = {"device_id": phone.device_id, "token": phone.token}
    # nobody chose a name yet: the worker's own name is used
    assert (await phone.register(name="j2-linux", **attach))["name"] == "j2-linux"
    await client.patch("/api/devices/%s" % phone.device_id, json={"name": "J2"})
    assert (await phone.register(**attach))["name"] == "J2"   # it reports "localhost" again
    assert cluster.db.get("devices", phone.device_id)["name"] == "J2"
    # a rename on the device itself (the browser page's Rename) still works, and sticks too
    assert (await phone.register(name="Kitchen tablet", rename=True, **attach))["name"] == "Kitchen tablet"
    await client.patch("/api/devices/%s" % phone.device_id, json={"settings": {"roles": {"ai": False}}})
    assert (await phone.register(**attach))["name"] == "Kitchen tablet"


def test_the_page_version_follows_the_browser_files(tmp_path: Path) -> None:
    from coordinator.updates import WorkerUpdates

    folder = tmp_path / "worker" / "browser"
    folder.mkdir(parents=True)
    (folder / "worker.js").write_text("one")
    updates = WorkerUpdates(SimpleNamespace(root=tmp_path))
    first = updates.page_version()
    assert updates.page_version() == first
    (folder / "worker.js").write_text("two!")
    assert updates.page_version() != first


# --------------------------------------------------------------------------
# the worker's side
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def w() -> Any:
    spec = importlib.util.spec_from_file_location("pc_worker_update_mod", str(WORKER_PY))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


class FakeWorker:
    def __init__(self, files: Dict[str, bytes], busy: Any = None) -> None:
        self.files = files
        self.busy = busy
        self.asked: List[str] = []
        self.restart_after_update = False
        self.stopping = False
        self.http = SimpleNamespace(request=self.request)

    def request(self, method: str, path: str, timeout: float = 30.0) -> Any:
        self.asked.append(path)
        return (200, self.files[path]) if path in self.files else (404, b"")

    def busy_reason(self) -> Any:
        return self.busy


def installed(tmp_path: Path, w: Any, new: bytes, busy: Any = None) -> Any:
    target = tmp_path / "worker.py"
    target.write_bytes(WORKER_PY.read_bytes() + b"\n# an older version\n")
    worker = FakeWorker({"/worker.py": new}, busy)
    updater = w.Updater(worker, path=str(target), running=sha(target.read_bytes()))
    return target, worker, updater


def test_a_good_update_is_swapped_in_and_the_worker_restarts(tmp_path: Path, w: Any) -> None:
    new = WORKER_PY.read_bytes()
    target, worker, updater = installed(tmp_path, w, new)
    old = target.read_bytes()
    assert updater.offer({"cmd": "update", "sha256": sha(new)}) is True
    assert target.read_bytes() == new and (tmp_path / "worker.py.bak").read_bytes() == old
    assert worker.restart_after_update and worker.stopping
    assert not (tmp_path / "worker.py.new").exists()


def test_a_download_that_does_not_match_is_refused(tmp_path: Path, w: Any) -> None:
    target, worker, updater = installed(tmp_path, w, b"print('tampered')\n")
    old = target.read_bytes()
    assert updater.offer({"cmd": "update", "sha256": "ab" * 32}) is False
    assert target.read_bytes() == old and not worker.restart_after_update
    assert updater.offer({"cmd": "update", "sha256": "ab" * 32}) is False
    assert worker.asked == ["/worker.py"]  # a version that failed here is not tried again


def test_a_version_that_does_not_start_here_is_refused(tmp_path: Path, w: Any) -> None:
    broken = b"import nothing_like_this_exists\n"
    target, worker, updater = installed(tmp_path, w, broken)
    old = target.read_bytes()
    assert updater.offer({"cmd": "update", "sha256": sha(broken)}) is False
    assert target.read_bytes() == old and not (tmp_path / "worker.py.new").exists()
    assert not (tmp_path / "worker.py.bak").exists() and not worker.restart_after_update


def test_a_busy_worker_or_the_same_version_does_nothing(tmp_path: Path, w: Any) -> None:
    new = WORKER_PY.read_bytes()
    target, worker, updater = installed(tmp_path, w, new, busy="1 task(s) running")
    assert updater.offer({"cmd": "update", "sha256": sha(new)}) is False
    assert worker.asked == [] and target.read_bytes() != new
    assert updater.offer({"cmd": "update", "sha256": updater.running}) is False


def test_a_file_already_on_disk_only_restarts(tmp_path: Path, w: Any) -> None:
    """The PC's own worker runs the project's worker.py: nothing to download."""
    target = tmp_path / "worker.py"
    target.write_bytes(WORKER_PY.read_bytes())
    worker = FakeWorker({})
    updater = w.Updater(worker, path=str(target), running=OLD)
    assert updater.offer({"cmd": "update", "sha256": sha(target.read_bytes())}) is True
    assert worker.asked == [] and worker.restart_after_update


def test_the_restart_keeps_the_options_but_not_the_used_join_code(w: Any, monkeypatch: Any) -> None:
    assert w.restart_args(["--url", "https://pc:8765", "--join", "ABCD-EFGH", "--ca", "ca.pem"]) == [
        "--url", "https://pc:8765", "--ca", "ca.pem"]
    assert w.restart_args(["--join=ABCD-EFGH", "--state-dir", "state"]) == ["--state-dir", "state"]
    # a launcher that set PC_SUPERVISED starts it again itself
    monkeypatch.setenv("PC_SUPERVISED", "1")
    monkeypatch.setenv("PC_JOIN_CODE", "ABCD-EFGH")
    assert w.restart_process(["--url", "x"]) == w.RESTART_CODE
    import os

    assert "PC_JOIN_CODE" not in os.environ
    # elsewhere: Windows runs the new file as a child, other systems replace the process
    monkeypatch.delenv("PC_SUPERVISED")
    seen: List[Any] = []
    monkeypatch.setattr(w.subprocess, "call", lambda command: seen.append(command) or 0)
    monkeypatch.setattr(w.os, "execv", lambda exe, command: seen.append(command))
    monkeypatch.setattr(w.signal, "signal", lambda *a: None)
    w.restart_process(["--state-dir", "state", "--join", "X"])
    assert seen[0][0] == sys.executable and seen[0][-3:] == [w.WORKER_FILE, "--state-dir", "state"]


def test_the_worker_reports_which_file_it_runs(w: Any) -> None:
    info = w.collect_info()
    assert info["worker_sha256"] == sha(WORKER_PY.read_bytes()) == w.SELF_SHA256
    assert info["worker_version"] == w.VERSION
