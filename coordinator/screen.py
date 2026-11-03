"""Show and control a phone's screen on the PC (SPEC §10, scrcpy).

The dashboard asks; the coordinator starts scrcpy from ``tools/scrcpy`` in a
window on this PC.  The phone is reached through adb: over USB, or over Wi-Fi
when its owner turned that on (``adb tcpip 5555`` once, while on USB).  Nothing
on the phone changes and nothing is installed on the PC.

The cluster's adb (the copy that ships with scrcpy) runs its own server on port
5039, so another adb on the PC (Android Studio's, another project's) is left
alone; its log goes to ``data/tmp``.  adb on Windows always uses the Windows
user's adb key (``%USERPROFILE%\\.android``); it only reads it.  The server
stops with the coordinator.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("coordinator.screen")

ADB_PORT = "5039"
WIFI_PORT = 5555
# scrcpy's tunnel to each phone.  Over Wi-Fi older phones refuse adb's "reverse" tunnel, and
# scrcpy's fallback ("forward") would give every window the same PC port, so a second phone
# took the first one's connection away: each window gets a port of its own.
TUNNEL_PORTS = range(27183, 27300)
ANDROID_PLATFORMS = ("termux", "linux")   # Termux phones; Linux running on an Android phone
NO_WINDOW = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW: no console flashes up

HOW_TO = (
    "the PC can't reach this phone through adb. Turn on USB debugging on the phone "
    "(docs/PHONE-SETUP.md) and connect it by USB; for Wi-Fi, once while on USB run "
    "`adb tcpip 5555`, then unplug it. If another adb program is running on this PC, "
    "close it first (`adb kill-server`): only one can use a USB phone at a time."
)


class ScreenError(Exception):
    """Why a phone's screen can't be shown (the dashboard shows the message)."""


def parse_devices(text: str) -> List[Dict[str, str]]:
    """``adb devices -l`` -> ``[{serial, state, model, ...}]``."""
    found: List[Dict[str, str]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("*") or line.lower().startswith("list of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        entry = {"serial": parts[0], "state": parts[1]}
        for part in parts[2:]:
            key, sep, value = part.partition(":")
            if sep:
                entry[key] = value
        found.append(entry)
    return found


def port_free(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def same_model(a: Optional[str], b: Optional[str]) -> bool:
    """adb writes ``Redmi_5A`` where Android says ``Redmi 5A``."""
    def norm(value: Optional[str]) -> str:
        return " ".join((value or "").replace("_", " ").lower().split())

    return bool(norm(a)) and norm(a) == norm(b)


class ScreenManager:
    def __init__(self, state: Any) -> None:
        self.state = state
        self.procs: Dict[str, subprocess.Popen] = {}
        self.ports: Dict[str, int] = {}   # device id -> its window's tunnel port
        self.hosts: Dict[str, str] = {}   # device id -> the address it last polled from
        self.used_adb = False

    # ------------------------------------------------------------- tools
    @property
    def tool_dir(self) -> Path:
        return Path(self.state.root) / "tools" / "scrcpy"

    def _exe(self, name: str) -> Path:
        return self.tool_dir / (name + (".exe" if os.name == "nt" else ""))

    def available(self) -> bool:
        return self._exe("scrcpy").is_file() and self._exe("adb").is_file()

    def env(self) -> Dict[str, str]:
        env = dict(os.environ)
        tmp = Path(self.state.data) / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        env.update({
            "ANDROID_ADB_SERVER_PORT": ADB_PORT,   # our own adb server, not anyone else's
            "ADB": str(self._exe("adb")),         # scrcpy uses this adb
            "TEMP": str(tmp), "TMP": str(tmp),    # adb's log
        })
        return env

    def adb(self, *args: str, timeout: float = 10.0) -> str:
        self.used_adb = True
        proc = subprocess.run(
            [str(self._exe("adb"))] + list(args), capture_output=True, text=True,
            env=self.env(), timeout=timeout, creationflags=NO_WINDOW,
        )
        return (proc.stdout or "") + (proc.stderr or "")

    # ------------------------------------------------------- finding it
    def find(self, device: Dict[str, Any]) -> str:
        """The adb serial of this cluster device: its Wi-Fi address, else the one USB
        phone of its model, else try adb over Wi-Fi at its address."""
        host = self.hosts.get(device["id"])
        model = (device.get("info") or {}).get("model") or device.get("model")
        listed = parse_devices(self.adb("devices", "-l"))
        if host:
            for entry in listed:
                if entry["serial"].rsplit(":", 1)[0] == host:
                    return self._usable(entry)
        same = [e for e in listed if same_model(e.get("model"), model)]
        if len(same) > 1:
            raise ScreenError("%d phones of model %s are connected by USB; unplug the others"
                              % (len(same), model))
        if same:
            return self._usable(same[0])
        if host:
            target = "%s:%d" % (host, WIFI_PORT)
            answer = self.adb("connect", target, timeout=8).lower()
            if "connected to" in answer:
                for entry in parse_devices(self.adb("devices", "-l")):
                    if entry["serial"] == target:
                        return self._usable(entry)
        raise ScreenError(HOW_TO)

    @staticmethod
    def _usable(entry: Dict[str, str]) -> str:
        state = entry.get("state")
        if state == "device":
            return entry["serial"]
        if state == "unauthorized":
            raise ScreenError("the phone hasn't allowed this PC yet: tap 'Allow' on its "
                              "'Allow USB debugging?' prompt (tick 'Always allow'), then try again")
        raise ScreenError("adb sees the phone as %s: unplug it and plug it back in, "
                          "or switch USB debugging off and on" % state)

    # ----------------------------------------------------------- showing
    def _tunnel_port(self) -> int:
        taken = {port for dev, port in self.ports.items()
                 if self.procs.get(dev) is not None and self.procs[dev].poll() is None}
        for port in TUNNEL_PORTS:
            if port not in taken and port_free(port):
                return port
        raise ScreenError("too many screens are open: close some of their windows")

    def open(self, device: Dict[str, Any]) -> Dict[str, Any]:
        if device.get("platform") not in ANDROID_PLATFORMS:
            raise ScreenError("only Android phones can show their screen here")
        if not self.available():
            raise ScreenError("scrcpy is not set up yet: run scripts\\get-scrcpy.ps1")
        running = self.procs.get(device["id"])
        if running is not None and running.poll() is None:
            return {"ok": True, "already_open": True}
        serial = self.find(device)
        port = self._tunnel_port()
        name = device.get("name") or device["id"]
        logs = Path(self.state.data) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log_path = logs / ("screen-%s.log" % device["id"])
        with open(log_path, "wb") as out:
            proc = subprocess.Popen(
                [str(self._exe("scrcpy")), "-s", serial, "--force-adb-forward", "--port", str(port),
                 "--window-title", "%s - PhoneCluster" % name],
                cwd=str(self.tool_dir), env=self.env(), stdin=subprocess.DEVNULL,
                stdout=out, stderr=subprocess.STDOUT, creationflags=NO_WINDOW,
            )
        deadline = time.time() + 2.5
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.1)
        if proc.poll() is not None:
            lines = [l for l in log_path.read_text("utf-8", "replace").splitlines() if l.strip()]
            errors = [l for l in lines if "ERROR" in l] or lines
            raise ScreenError("scrcpy stopped: %s" % (errors[-1].strip() if errors else "no output"))
        self.procs[device["id"]] = proc
        self.ports[device["id"]] = port
        log.info("showing the screen of %s (%s, tunnel port %d)", name, serial, port)
        return {"ok": True, "serial": serial, "via": "wifi" if ":" in serial else "usb"}

    def close(self) -> None:
        """With the coordinator: close the screen windows and our adb server."""
        for proc in self.procs.values():
            if proc.poll() is None:
                proc.terminate()
        self.procs.clear()
        self.ports.clear()
        if self.used_adb and self.available():
            try:
                self.adb("kill-server", timeout=5)
            except Exception:  # pragma: no cover - nothing to stop
                pass
