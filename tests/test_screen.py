"""Showing a phone's screen on the PC with scrcpy (coordinator/screen.py, SPEC §10).

adb and scrcpy are replaced by fakes: nothing here needs a phone or starts a window.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from coordinator import screen
from coordinator.screen import ScreenError, ScreenManager, parse_devices, same_model

ADB_LIST = """* daemon not running; starting now at tcp:5039
* daemon started successfully
List of devices attached
7d9f1a2b               device usb:1-4 product:riva model:Redmi_5A device:riva transport_id:3
192.168.1.9:5555      device product:j2lte model:SM_J200G device:j2lte transport_id:4
0123456789ABCDEF       unauthorized usb:1-2 transport_id:5

"""


def test_parse_devices():
    found = parse_devices(ADB_LIST)
    assert [d["serial"] for d in found] == ["7d9f1a2b", "192.168.1.9:5555", "0123456789ABCDEF"]
    assert found[0]["model"] == "Redmi_5A" and found[0]["state"] == "device"
    assert found[2]["state"] == "unauthorized"
    assert parse_devices("List of devices attached\n\n") == []
    assert parse_devices("") == []


def test_same_model():
    assert same_model("Redmi_5A", "Redmi 5A")
    assert same_model("redmi 5a", "Redmi_5A")
    assert not same_model("Redmi_5A", "Redmi 5")
    assert not same_model(None, None)
    assert not same_model("", "")


class FakeState:
    def __init__(self, root: Path, data: Path) -> None:
        self.root = root
        self.data = data


def tools(root: Path) -> Path:
    folder = root / "tools" / "scrcpy"
    folder.mkdir(parents=True)
    for name in ("scrcpy.exe", "adb.exe", "scrcpy", "adb"):
        (folder / name).write_text("")
    return folder


@pytest.fixture()
def manager(tmp_path: Path):
    tools(tmp_path)
    return ScreenManager(FakeState(tmp_path, tmp_path / "data"))


def fake_adb(manager: ScreenManager, listing: str, connect: str = "", after_connect: Optional[str] = None):
    calls: List[tuple] = []

    def adb(*args: str, timeout: float = 10.0) -> str:
        calls.append(args)
        if args[0] == "devices":
            return after_connect if (after_connect and any(c[0] == "connect" for c in calls)) else listing
        if args[0] == "connect":
            return connect
        return ""

    manager.adb = adb  # type: ignore[assignment]
    return calls


def phone(model: str = "Redmi 5A", platform: str = "termux", device_id: str = "dev1") -> Dict[str, Any]:
    return {"id": device_id, "name": model, "platform": platform, "info": {"model": model}}


def test_finds_the_phone_by_its_wifi_address_first(manager):
    fake_adb(manager, ADB_LIST)
    manager.hosts["j2"] = "192.168.1.9"
    assert manager.find(phone("localhost", "linux", "j2")) == "192.168.1.9:5555"


def test_finds_a_usb_phone_by_its_model(manager):
    fake_adb(manager, ADB_LIST)
    assert manager.find(phone("Redmi 5A")) == "7d9f1a2b"


def test_two_usb_phones_of_one_model_are_ambiguous(manager):
    fake_adb(manager, ADB_LIST + "abcdef12 device usb:1-5 model:Redmi_5A\n")
    with pytest.raises(ScreenError, match="2 phones of model Redmi 5A"):
        manager.find(phone("Redmi 5A"))


def test_an_unapproved_phone_says_what_to_tap(manager):
    fake_adb(manager, "List of devices attached\nX1 unauthorized usb:1 model:Redmi_5A\n")
    with pytest.raises(ScreenError, match="Allow USB debugging"):
        manager.find(phone("Redmi 5A"))


def test_tries_adb_over_wifi_at_the_phones_address(manager):
    calls = fake_adb(manager, "List of devices attached\n",
                     connect="connected to 192.168.1.20:5555",
                     after_connect="List of devices attached\n192.168.1.20:5555 device model:Redmi_5A\n")
    manager.hosts["dev1"] = "192.168.1.20"
    assert manager.find(phone("Redmi 5A")) == "192.168.1.20:5555"
    assert ("connect", "192.168.1.20:5555") in calls


def test_no_way_to_reach_it_explains_usb_debugging(manager):
    fake_adb(manager, "List of devices attached\n", connect="failed to connect to '192.168.1.20:5555'")
    manager.hosts["dev1"] = "192.168.1.20"
    with pytest.raises(ScreenError, match="USB debugging"):
        manager.find(phone("Redmi 5A"))


class FakeProc:
    def __init__(self, code: Optional[int] = None) -> None:
        self.code = code
        self.terminated = False

    def poll(self) -> Optional[int]:
        return self.code

    def terminate(self) -> None:
        self.terminated = True


def test_open_starts_scrcpy_for_that_phone(manager, monkeypatch):
    fake_adb(manager, ADB_LIST)
    started: List[Dict[str, Any]] = []

    def popen(cmd, **kwargs):
        started.append({"cmd": cmd, **kwargs})
        return FakeProc()

    monkeypatch.setattr(screen.subprocess, "Popen", popen)
    monkeypatch.setattr(screen.time, "sleep", lambda s: None)
    times = iter([0.0] + [10.0] * 10)
    monkeypatch.setattr(screen.time, "time", lambda: next(times))
    result = manager.open(phone("Redmi 5A"))
    assert result == {"ok": True, "serial": "7d9f1a2b", "via": "usb"}
    cmd = started[0]["cmd"]
    assert cmd[1:3] == ["-s", "7d9f1a2b"] and "Redmi 5A - PhoneCluster" in cmd
    assert "--force-adb-forward" in cmd and cmd[cmd.index("--port") + 1] == str(manager.ports["dev1"])
    env = started[0]["env"]
    assert env["ANDROID_ADB_SERVER_PORT"] == "5039"        # our own adb server
    assert env["ADB"].endswith(("adb.exe", "adb"))
    assert Path(env["TEMP"]) == manager.state.data / "tmp"
    # a second click while the window is open does not start another
    assert manager.open(phone("Redmi 5A")) == {"ok": True, "already_open": True}
    assert len(started) == 1
    manager.used_adb = False
    manager.close()
    assert manager.procs == {}


def test_two_phones_get_their_own_tunnel_ports(manager, monkeypatch):
    """Both screens open at once: a second phone must not take the first one's port
    (it did: scrcpy's fallback tunnel used 27183 for every phone)."""
    fake_adb(manager, ADB_LIST)
    manager.hosts["j2"] = "192.168.1.9"
    procs: Dict[str, FakeProc] = {}

    def popen(cmd, **kwargs):
        proc = FakeProc()
        procs[cmd[2]] = proc
        return proc

    busy = {screen.TUNNEL_PORTS[0]}  # something else on the PC already uses the first port
    monkeypatch.setattr(screen, "port_free", lambda port: port not in busy)
    monkeypatch.setattr(screen.subprocess, "Popen", popen)
    monkeypatch.setattr(screen.time, "sleep", lambda s: None)
    clock = iter([0.0, 10.0, 20.0, 30.0, 40.0, 50.0])
    monkeypatch.setattr(screen.time, "time", lambda: next(clock))
    manager.open(phone("Redmi 5A"))
    manager.open(phone("localhost", "linux", "j2"))
    first, second = manager.ports["dev1"], manager.ports["j2"]
    assert first != second and screen.TUNNEL_PORTS[0] not in (first, second)
    # a closed window gives its port back
    procs["7d9f1a2b"].code = 0
    assert manager._tunnel_port() == first


def test_port_free_sees_a_port_in_use():
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        assert screen.port_free(held.getsockname()[1]) is False


def test_open_reports_why_scrcpy_stopped(manager, monkeypatch):
    fake_adb(manager, ADB_LIST)

    def popen(cmd, stdout=None, **kwargs):
        stdout.write(b"INFO: scrcpy 5.0.1\nERROR: Could not find any ADB device\n")
        return FakeProc(code=1)

    monkeypatch.setattr(screen.subprocess, "Popen", popen)
    with pytest.raises(ScreenError, match="Could not find any ADB device"):
        manager.open(phone("Redmi 5A"))


def test_open_refuses_what_it_cannot_show(tmp_path):
    manager = ScreenManager(FakeState(tmp_path, tmp_path / "data"))
    with pytest.raises(ScreenError, match="only Android phones"):
        manager.open(phone("PC", "windows"))
    with pytest.raises(ScreenError, match="get-scrcpy"):
        manager.open(phone("Redmi 5A"))


@pytest.mark.asyncio
async def test_screen_api(client: Any, device: Any, cluster: Any, monkeypatch) -> None:
    await device.poll()
    assert cluster.screens.hosts[device.device_id]  # remembered for adb over Wi-Fi
    assert (await client.post("/api/devices/nope/screen")).status_code == 404

    def refuse(found: Dict[str, Any]) -> Dict[str, Any]:
        raise ScreenError("scrcpy is not set up yet: run scripts\\get-scrcpy.ps1")

    monkeypatch.setattr(cluster.screens, "open", refuse)
    response = await client.post("/api/devices/%s/screen" % device.device_id)
    assert response.status_code == 400 and "get-scrcpy" in response.json()["error"]

    seen: List[str] = []

    def show(found: Dict[str, Any]) -> Dict[str, Any]:
        seen.append(found["id"])
        return {"ok": True, "serial": "abc", "via": "usb"}

    monkeypatch.setattr(cluster.screens, "open", show)
    response = await client.post("/api/devices/%s/screen" % device.device_id)
    assert response.status_code == 200 and response.json()["via"] == "usb"
    assert seen == [device.device_id]
