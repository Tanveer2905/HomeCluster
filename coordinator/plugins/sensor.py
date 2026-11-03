"""``sensor`` -- read Android sensors through Termux:API (SPEC §7)."""

from __future__ import annotations

from typing import Any, Dict, List

TYPE = "sensor"
DESCRIPTION = "Capture a photo / audio / location / battery / sensor reading on phones"

SENSORS = ["photo", "audio", "location", "battery", "sensors", "wifi"]

PARAMS = [
    {"name": "sensor", "type": "select", "default": "battery", "choices": SENSORS},
    {"name": "duration", "type": "int", "default": 5, "help": "Seconds to record (audio)."},
    {"name": "camera", "type": "int", "default": 0, "help": "Camera id for photo (0 = back)."},
    {"name": "targets", "type": "json", "default": "all", "help": '"all" or a list of device ids.'},
]

DEFAULT_OPTIONS: Dict[str, Any] = {
    "targets": "all",
    "requires": ["termux_api"],
    "timeout": 120,
    "backups": False,
}


def _command(params: Dict[str, Any]) -> str:
    sensor = str(params.get("sensor") or "battery").lower()
    if sensor not in SENSORS:
        raise ValueError("sensor must be one of %s" % ", ".join(SENSORS))
    camera = int(params.get("camera") or 0)
    duration = int(params.get("duration") or 5)
    if sensor == "photo":
        return 'termux-camera-photo -c %d "$PC_OUTPUT_DIR/photo.jpg" && echo photo.jpg' % camera
    if sensor == "audio":
        return (
            'termux-microphone-record -d -l %d -f "$PC_OUTPUT_DIR/audio.m4a" '
            "&& sleep %d && termux-microphone-record -q && echo audio.m4a"
            % (duration, duration + 1)
        )
    if sensor == "location":
        return "termux-location"
    if sensor == "battery":
        return "termux-battery-status"
    if sensor == "sensors":
        return "termux-sensor -a -n 1"
    return "termux-wifi-connectioninfo"


def split(params: Dict[str, Any], options: Dict[str, Any], ctx: Any) -> List[Dict[str, Any]]:
    cmd = _command(params)
    targets = params.get("targets") or options.get("targets") or "all"
    requires = list(options.get("requires") or ["termux_api"])
    if "termux_api" not in requires:
        requires.append("termux_api")
    timeout = options.get("timeout") or 120

    if isinstance(targets, (list, tuple)):
        device_ids = [str(t) for t in targets if t]
    else:
        device_ids = [
            d["id"]
            for d in ctx.devices()
            if "termux_api" in (d.get("caps") or []) and "shell" in (d.get("caps") or [])
        ]
    if not device_ids:
        raise ValueError("no online device has the termux_api capability")

    payloads: List[Dict[str, Any]] = []
    for device_id in device_ids:
        payloads.append(
            {
                "impls": {"shell": {"cmd": cmd, "env": {}, "cwd": None}},
                "inputs": [],
                "timeout": timeout,
                "requires": requires,
                "device_id": device_id,
                "meta": {"sensor": params.get("sensor")},
            }
        )
    return payloads


def reduce(params: Dict[str, Any], results: List[Dict[str, Any]], ctx: Any) -> Any:
    out: Dict[str, Any] = {}
    for item in results:
        key = str(item.get("device_id") or ("task%s" % item.get("idx")))
        value = item.get("result") or {}
        text = value.get("stdout") if isinstance(value, dict) else None
        entry: Dict[str, Any] = {
            "ok": bool(item.get("ok")),
            "outputs": item.get("outputs") or [],
        }
        if item.get("error"):
            entry["error"] = item.get("error")
        if text:
            entry["stdout"] = text
            stripped = str(text).strip()
            if stripped.startswith("{") or stripped.startswith("["):
                import json

                try:
                    entry["data"] = json.loads(stripped)
                except ValueError:
                    pass
        out[key] = entry
    return out
