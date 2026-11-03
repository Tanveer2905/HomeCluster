"""``service`` -- host a model on a device and answer requests (SPEC §6.7).

The job never finishes on its own: each task keeps a model loaded and serves
requests relayed by the coordinator until the job is cancelled.  Service
code defines::

    def load(ctx):                 # once per (re)start; return the model
        ...
    def serve(request, ctx):       # or serve(request, ctx, model)
        for chunk in request.stream():   # bytes as they arrive, no disk
            ...
        ctx.emit({"partial": ...})       # optional live results
        return {"text": ...}             # JSON-serialisable final result

``ctx`` also offers ``log``, ``inputs`` (model files from ``inputs``),
``service_dir`` (persistent per-service folder), ``output_bytes(data, name)``
for binary results, ``params`` (static params from the job), ``request``.
"""

from __future__ import annotations

from typing import Any, Dict, List

TYPE = "service"
DESCRIPTION = "Host a model (speech, vision, translation, TTS, ...) that other devices call"

PARAMS = [
    {"name": "name", "type": "str", "default": "", "help": "Service name callers use, e.g. vosk-en."},
    {"name": "description", "type": "str", "default": "", "help": "Shown in the service list."},
    {"name": "code", "type": "code",
     "default": "def load(ctx):\n    return None\n\ndef serve(request, ctx):\n    data = request.read()\n    return {\"bytes\": len(data)}\n",
     "help": "Python defining load(ctx) and serve(request, ctx)."},
    {"name": "inputs", "type": "files", "default": [], "help": "Model files, downloaded once (ctx.inputs)."},
    {"name": "setup_cmd", "type": "str", "default": "",
     "help": "Optional shell command run once before load(), e.g. pip install vosk."},
    {"name": "accepts", "type": "str", "default": "", "help": "What callers send, e.g. audio/wav, image/*, text."},
    {"name": "produces", "type": "str", "default": "", "help": "What the service returns, e.g. text, json, audio/wav."},
    {"name": "streaming_output", "type": "bool", "default": False,
     "help": "The service emits partial results while working."},
    {"name": "request_timeout", "type": "int", "default": 300, "help": "Seconds per request before the model is restarted."},
    {"name": "params", "type": "json", "default": {}, "help": "Static settings passed to load()/serve() as ctx.params."},
]

#: services run on devices with the "AI models & services" role, outside the
#: normal job slots; no deadline, retried on crash
DEFAULT_OPTIONS: Dict[str, Any] = {"requires": ["service"], "timeout": 0, "max_attempts": 5}


def split(params: Dict[str, Any], options: Dict[str, Any], ctx: Any) -> List[Dict[str, Any]]:
    name = str(params.get("name") or "").strip()
    if not name:
        raise ValueError("service needs a 'name'")
    code = str(params.get("code") or "")
    if "def serve" not in code:
        raise ValueError("service code must define serve(request, ctx)")
    requires = list(dict.fromkeys(list(options.get("requires") or []) + ["service"]))
    service = {
        "name": name,
        "request_timeout": int(params.get("request_timeout") or 300),
        "setup_cmd": str(params.get("setup_cmd") or "").strip() or None,
        "params": params.get("params") if isinstance(params.get("params"), dict) else {},
        "meta": {"accepts": params.get("accepts"), "produces": params.get("produces"),
                 "streaming_output": bool(params.get("streaming_output"))},
    }

    def payload(device_id: Any = None) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "impls": {"python": {"code": code, "entry": "serve", "args": {}}},
            "inputs": list(params.get("inputs") or []),
            "timeout": 0,
            "requires": requires,
            "service": service,
            "meta": {"service": name},
        }
        if device_id:
            out["device_id"] = str(device_id)
        return out

    targets = options.get("targets")
    if targets == "all":
        devices = [d for d in ctx.devices(role="ai") if "service" in (d.get("caps") or [])]
        if not devices:
            raise ValueError("no online device can host services")
        return [payload(d["id"]) for d in devices]
    if isinstance(targets, (list, tuple)) and targets:
        return [payload(t) for t in targets]
    return [payload()]


def reduce(params: Dict[str, Any], results: List[Dict[str, Any]], ctx: Any) -> Any:
    return {"service": params.get("name"), "instances": len(results), "stopped": True}
