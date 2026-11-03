"""``custom`` -- raw task payload passthrough for power users (SPEC §7)."""

from __future__ import annotations

import copy
from typing import Any, Dict, List

TYPE = "custom"
DESCRIPTION = "Ship raw impls (python / js / shell) with one task per args_list entry"

PARAMS = [
    {
        "name": "impls",
        "type": "json",
        "default": {"shell": {"cmd": "echo hi"}},
        "help": "Task impls exactly as in SPEC §6.",
    },
    {
        "name": "args_list",
        "type": "json",
        "default": [],
        "help": "One task per element; each element is merged into every impl's args.",
    },
    {"name": "inputs", "type": "files", "default": [], "help": "file_ids to download."},
    {"name": "requires", "type": "json", "default": [], "help": "Extra capabilities."},
]

DEFAULT_OPTIONS: Dict[str, Any] = {"requires": []}


def _merge_args(impls: Dict[str, Any], extra: Any) -> Dict[str, Any]:
    out = copy.deepcopy(impls)
    if not isinstance(extra, dict):
        extra = {"item": extra}
    for kind, impl in out.items():
        if not isinstance(impl, dict):
            continue
        if kind == "shell":
            env = dict(impl.get("env") or {})
            for key, value in extra.items():
                env["PC_ARG_%s" % str(key).upper()] = str(value)
            impl["env"] = env
        else:
            args = dict(impl.get("args") or {})
            args.update(extra)
            impl["args"] = args
    return out


def split(params: Dict[str, Any], options: Dict[str, Any], ctx: Any) -> List[Dict[str, Any]]:
    impls = params.get("impls")
    if not isinstance(impls, dict) or not impls:
        raise ValueError("custom needs a non-empty 'impls' object")
    for kind in impls:
        if kind not in ("python", "js", "shell", "wasm"):
            raise ValueError("unknown impl kind %r" % (kind,))
    inputs = list(params.get("inputs") or [])
    requires = list(options.get("requires") or []) + list(params.get("requires") or [])
    requires = list(dict.fromkeys(requires))
    timeout = options.get("timeout")
    args_list = params.get("args_list")
    targets = options.get("targets")

    if not args_list:
        base = [{"impls": copy.deepcopy(impls), "inputs": inputs, "timeout": timeout,
                 "requires": requires}]
    else:
        if not isinstance(args_list, list):
            raise ValueError("args_list must be a list")
        base = [
            {
                "impls": _merge_args(impls, extra),
                "inputs": inputs,
                "timeout": timeout,
                "requires": requires,
                "meta": {"args": extra},
            }
            for extra in args_list
        ]

    if targets == "all":
        devices = ctx.devices()
        if not devices:
            raise ValueError("no online device")
        out: List[Dict[str, Any]] = []
        for device in devices:
            for payload in base:
                pinned = copy.deepcopy(payload)
                pinned["device_id"] = device["id"]
                out.append(pinned)
        return out
    if isinstance(targets, (list, tuple)) and targets:
        out = []
        for device_id in targets:
            for payload in base:
                pinned = copy.deepcopy(payload)
                pinned["device_id"] = str(device_id)
                out.append(pinned)
        return out
    return base


def reduce(params: Dict[str, Any], results: List[Dict[str, Any]], ctx: Any) -> Any:
    return [
        {
            "idx": item.get("idx"),
            "device_id": item.get("device_id"),
            "ok": item.get("ok"),
            "result": item.get("result"),
            "outputs": item.get("outputs") or [],
            "error": item.get("error"),
        }
        for item in results
    ]
