"""``shell`` -- run a shell command on one device or on all of them (SPEC §7)."""

from __future__ import annotations

from typing import Any, Dict, List

TYPE = "shell"
DESCRIPTION = "Run a shell command on one device (any) or fan it out to all"

PARAMS = [
    {"name": "cmd", "type": "str", "default": "echo hello", "help": "Command line (POSIX / Termux)."},
    {"name": "cmd_windows", "type": "str", "default": None, "help": "Overrides cmd on Windows workers."},
    {"name": "timeout", "type": "int", "default": 600, "help": "Seconds before the command is killed."},
    {"name": "env", "type": "json", "default": {}, "help": "Extra environment variables."},
    {"name": "cwd", "type": "str", "default": None, "help": "Working directory (default: the task workdir)."},
    {"name": "inputs", "type": "files", "default": [], "help": "file_ids exposed as PC_INPUT_<n>."},
]

#: commands may have side effects, so they never run twice as a backup copy
DEFAULT_OPTIONS: Dict[str, Any] = {"targets": "any", "requires": [], "backups": False}


def _payload(params: Dict[str, Any], options: Dict[str, Any], device_id: Any = None) -> Dict[str, Any]:
    impl: Dict[str, Any] = {
        "cmd": str(params.get("cmd") or ""),
        "env": dict(params.get("env") or {}),
        "cwd": params.get("cwd"),
    }
    if params.get("cmd_windows"):
        impl["cmd_windows"] = str(params["cmd_windows"])
    timeout = params.get("timeout") or options.get("timeout")
    payload: Dict[str, Any] = {
        "impls": {"shell": impl},
        "inputs": list(params.get("inputs") or []),
        "timeout": timeout,
        "requires": list(options.get("requires") or []),
    }
    if device_id:
        payload["device_id"] = str(device_id)
    return payload


def split(params: Dict[str, Any], options: Dict[str, Any], ctx: Any) -> List[Dict[str, Any]]:
    if not str(params.get("cmd") or "").strip() and not str(params.get("cmd_windows") or "").strip():
        raise ValueError("shell needs a 'cmd'")
    targets = options.get("targets")
    if targets is None:
        targets = "any"
    if targets == "all":
        devices = [d for d in ctx.devices() if "shell" in (d.get("caps") or [])]
        if not devices:
            raise ValueError("no online device can run shell commands")
        return [_payload(params, options, d["id"]) for d in devices]
    if isinstance(targets, (list, tuple)):
        ids = [str(t) for t in targets if t]
        if not ids:
            raise ValueError("targets list is empty")
        return [_payload(params, options, device_id) for device_id in ids]
    return [_payload(params, options)]


def reduce(params: Dict[str, Any], results: List[Dict[str, Any]], ctx: Any) -> Any:
    """``{device_id: {exit_code, stdout, stderr}}`` (SPEC §7)."""
    out: Dict[str, Any] = {}
    for item in results:
        key = str(item.get("device_id") or ("task%s" % item.get("idx")))
        value = item.get("result")
        if not isinstance(value, dict):
            value = {
                "exit_code": None,
                "stdout": "",
                "stderr": str(item.get("error") or ""),
            }
        out[key] = value
    return out
