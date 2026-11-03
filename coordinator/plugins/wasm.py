"""``wasm`` -- one WebAssembly program runs on every device (SPEC §7, §6.9).

Compile once (C, C++, Rust, Go, Zig, AssemblyScript ... to ``wasm32-wasi``),
upload the ``.wasm`` file, and the same bytes run on the PC (wasmtime from
``tools/wasmtime``), on Termux phones (``pkg install wasmtime``, done by the
worker on its first WebAssembly task) and in a browser tab such as the
Samsung J2's (the browser's own WebAssembly engine).  No per-device code.

Two ways to run a module:

* **call** an exported function with numbers: ``call: count_primes`` and
  ``args_list: [[2, 1000], [1000, 2000]]`` -- or ``range: [2, 10000000]`` to let
  the cluster cut the range into pieces sized to each device's speed; the
  function is then called as ``f(lo, hi)`` per piece and the results are
  added up (``combine``);
* **run** it as a WASI program (``_start``): ``args_list`` gives each task its
  command line, ``stdin`` a text, and with ``inputs`` every file becomes one
  task whose stdin is that file (also readable at ``/in/<name>``).  Whatever
  the program prints is the result (parsed as JSON when it is JSON); files it
  writes to ``/out`` come back as outputs.

WebAssembly is sandboxed: a task sees only its own ``/in`` and ``/out``
folders, no network, and at most ``memory_mb`` of memory.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

TYPE = "wasm"
DESCRIPTION = "Run one WebAssembly program on every device (PC, phones, browsers)"

PARAMS = [
    {"name": "module", "type": "file", "default": None,
     "help": "file_id of the .wasm module (compiled for wasm32-wasi, or plain functions)."},
    {"name": "call", "type": "str", "default": "",
     "help": "Exported function to call with numbers; empty = run the module as a WASI program."},
    {"name": "range", "type": "json", "default": None,
     "help": "[start, end]: call f(lo, hi) on pieces of this range, sized automatically."},
    {"name": "grain", "type": "int", "default": 1,
     "help": "Pieces of a range are a multiple of this many numbers."},
    {"name": "args_list", "type": "json", "default": [],
     "help": "One task per entry: numbers for a call, command-line strings for a program."},
    {"name": "inputs", "type": "files", "default": [],
     "help": "Program mode: one task per file, the file as stdin and at /in/<name>."},
    {"name": "stdin", "type": "str", "default": "", "help": "Text given to the program on stdin."},
    {"name": "env", "type": "json", "default": {}, "help": "Environment variables for the program."},
    {"name": "combine", "type": "select", "default": "auto", "choices": ["auto", "list", "sum", "text"],
     "help": "How task results join: auto = sum for a range, else a list."},
    {"name": "memory_mb", "type": "int", "default": 256, "help": "Memory limit per task."},
]

DEFAULT_OPTIONS: Dict[str, Any] = {"requires": []}

#: deterministic unless the program reads the clock or random numbers -- the
#: checker notices that and stops checking (SPEC 9.9)
CACHE = True
VERIFY = True


# ------------------------------------------------------------ module reading
VALTYPES = {0x7F: "i32", 0x7E: "i64", 0x7D: "f32", 0x7C: "f64", 0x7B: "v128",
            0x70: "funcref", 0x6F: "externref"}


class _Reader:
    def __init__(self, data: bytes, pos: int = 0, end: Optional[int] = None) -> None:
        self.data, self.pos, self.end = data, pos, len(data) if end is None else end

    def byte(self) -> int:
        if self.pos >= self.end:
            raise ValueError("truncated module")
        value = self.data[self.pos]
        self.pos += 1
        return value

    def uleb(self) -> int:
        result = shift = 0
        while True:
            byte = self.byte()
            result |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return result

    def name(self) -> str:
        size = self.uleb()
        text = self.data[self.pos:self.pos + size].decode("utf-8", "replace")
        self.pos += size
        return text

    def limits(self) -> None:
        flags = self.byte()
        self.uleb()
        if flags & 1:
            self.uleb()


def inspect_module(data: bytes) -> Dict[str, Any]:
    """Exports (with parameter and result types) and imports of a module."""
    if data[:4] != b"\x00asm":
        if data.lstrip()[:1] == b"(":
            raise ValueError("this is WebAssembly text (.wat); browsers need the binary .wasm")
        raise ValueError("not a WebAssembly module")
    types: List[Dict[str, List[str]]] = []
    funcs: List[int] = []
    imports: List[str] = []
    exports: Dict[str, Dict[str, Any]] = {}
    reader = _Reader(data, 8)
    while reader.pos < reader.end:
        sid = reader.byte()
        size = reader.uleb()
        body = _Reader(data, reader.pos, reader.pos + size)
        reader.pos += size
        if sid == 1:
            for _ in range(body.uleb()):
                if body.byte() != 0x60:
                    raise ValueError("unsupported type entry (GC types)")
                params = [VALTYPES.get(body.byte(), "?") for _ in range(body.uleb())]
                results = [VALTYPES.get(body.byte(), "?") for _ in range(body.uleb())]
                types.append({"params": params, "results": results})
        elif sid == 2:
            for _ in range(body.uleb()):
                module, field, kind = body.name(), body.name(), body.byte()
                imports.append("%s.%s" % (module, field))
                if kind == 0:
                    funcs.append(body.uleb())
                elif kind == 1:
                    body.byte()
                    body.limits()
                elif kind == 2:
                    body.limits()
                elif kind == 3:
                    body.byte()
                    body.byte()
                else:
                    body.byte()
                    body.uleb()
        elif sid == 3:
            funcs.extend(body.uleb() for _ in range(body.uleb()))
        elif sid == 7:
            for _ in range(body.uleb()):
                name, kind, index = body.name(), body.byte(), body.uleb()
                entry: Dict[str, Any] = {"kind": ("func", "table", "memory", "global", "tag")[kind]
                                         if kind < 5 else "?"}
                if kind == 0 and index < len(funcs) and funcs[index] < len(types):
                    entry.update(types[funcs[index]])
                exports[name] = entry
    return {"exports": exports, "imports": imports,
            "wasi": any(i.startswith("wasi_snapshot_preview1.") for i in imports),
            "program": "_start" in exports}


def _module(params: Dict[str, Any], ctx: Any) -> Dict[str, Any]:
    file_id = params.get("module")
    if isinstance(file_id, list):
        file_id = file_id[0] if file_id else None
    if not file_id:
        raise ValueError("wasm needs 'module': the file id of a .wasm file")
    meta = ctx.file_meta(str(file_id)) if ctx is not None else None
    if ctx is not None and meta is None:
        raise ValueError("module %r is not in the file store" % file_id)
    info: Dict[str, Any] = {"exports": {}, "imports": []}
    if ctx is not None:
        info = inspect_module(ctx.read_file(str(file_id)))
    call = str(params.get("call") or "").strip()
    if ctx is not None and call:
        export = info["exports"].get(call)
        if export is None or export.get("kind") != "func":
            names = sorted(n for n, e in info["exports"].items() if e.get("kind") == "func")
            raise ValueError("the module exports no function %r (it has: %s)"
                             % (call, ", ".join(names) or "none"))
        if any(t not in ("i32", "i64", "f32", "f64") for t in export.get("params", [])):
            raise ValueError("%s takes non-numeric parameters; call it from a WASI program" % call)
    elif ctx is not None and not info.get("program"):
        raise ValueError("the module has no _start: give 'call' (one of its functions)")
    return {"id": str(file_id), "name": str((meta or {}).get("name") or "module.wasm"),
            "info": info, "export": info["exports"].get(call) if call else None}


def _range(params: Dict[str, Any]) -> Optional[List[int]]:
    rng = params.get("range")
    if rng in (None, "", []):
        return None
    if not isinstance(rng, (list, tuple)) or len(rng) != 2:
        raise ValueError("range must be [start, end]")
    start, end = int(rng[0]), int(rng[1])
    if end <= start:
        raise ValueError("range end must be greater than start")
    return [start, end]


def _impl(params: Dict[str, Any], module: Dict[str, Any], args: List[Any], files: List[int],
          names: Dict[str, str], stdin_input: Optional[int]) -> Dict[str, Any]:
    call = str(params.get("call") or "").strip() or None
    impl: Dict[str, Any] = {
        "module": 0,
        "call": call,
        "args": list(args),
        "env": {str(k): str(v) for k, v in dict(params.get("env") or {}).items()},
        "memory_mb": max(16, int(params.get("memory_mb") or 256)),
    }
    export = module.get("export") or {}
    if call and export.get("params") is not None:
        if len(export["params"]) != len(args):
            raise ValueError("%s takes %d number(s), got %d" % (call, len(export["params"]), len(args)))
        # browsers print f32 like wasmtime does, and pass i64 right, only when they know the types
        impl["params"] = list(export.get("params") or [])
        impl["results"] = list(export.get("results") or [])
    if not call:
        impl.update({"files": files, "names": names, "stdin_input": stdin_input,
                     "stdin": str(params.get("stdin") or "") if stdin_input is None else None})
    return impl


def _payload(params: Dict[str, Any], options: Dict[str, Any], module: Dict[str, Any],
             args: List[Any], data: List[Dict[str, Any]], meta: Dict[str, Any]) -> Dict[str, Any]:
    inputs: List[Any] = [module["id"]] + [d["id"] for d in data]
    files = list(range(1, len(inputs)))
    names = {str(i + 1): d["name"] for i, d in enumerate(data)}
    stdin_input = 1 if data else None
    return {
        "impls": {"wasm": _impl(params, module, args, files, names, stdin_input)},
        "inputs": inputs,
        "timeout": options.get("timeout"),
        "requires": list(options.get("requires") or []),
        "meta": meta,
    }


def speed_key(params: Dict[str, Any]) -> str:
    text = "%s:%s" % (params.get("module"), params.get("call") or "_start")
    return "wasm:" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def work(params: Dict[str, Any], options: Dict[str, Any], ctx: Any) -> Any:
    """A range is cut into pieces as devices free up (SPEC 9.6)."""
    rng = _range(params)
    if rng is None:
        return None
    if not str(params.get("call") or "").strip():
        raise ValueError("range needs 'call': the function to call as f(lo, hi)")
    module = _module(params, ctx)
    return {"units": rng[1] - rng[0], "grain": max(1, int(params.get("grain") or 1)),
            "state": {"module": module}}


def piece(params: Dict[str, Any], options: Dict[str, Any], ctx: Any, state: Any,
          lo: int, hi: int) -> Dict[str, Any]:
    start = _range(params)[0]  # type: ignore[index]
    module = (state or {}).get("module") or _module(params, ctx)
    return _payload(params, options, module, [start + lo, start + hi], [],
                    {"lo": start + lo, "hi": start + hi, "units": hi - lo})


def split(params: Dict[str, Any], options: Dict[str, Any], ctx: Any) -> List[Dict[str, Any]]:
    module = _module(params, ctx)
    call = str(params.get("call") or "").strip()
    rng = _range(params)
    if rng is not None:  # a fixed chunk_size asked for fixed pieces
        if not call:
            raise ValueError("range needs 'call': the function to call as f(lo, hi)")
        chunk = max(1, int(options.get("chunk_size") or 0) or (rng[1] - rng[0]))
        out = []
        for lo in range(rng[0], rng[1], chunk):
            hi = min(lo + chunk, rng[1])
            out.append(_payload(params, options, module, [lo, hi], [], {"lo": lo, "hi": hi, "units": hi - lo}))
        return out
    args_list = params.get("args_list") or []
    if not isinstance(args_list, list):
        raise ValueError("args_list must be a list")
    inputs = [str(f) for f in (params.get("inputs") or []) if f]
    if call and inputs:
        raise ValueError("inputs are for programs; a call only takes numbers")
    if inputs:
        out = []
        base = args_list[0] if args_list else []
        for idx, file_id in enumerate(inputs):
            meta = ctx.file_meta(file_id) or {}
            name = str(meta.get("name") or "input-%d" % idx).replace("/", "_").replace("\\", "_")
            out.append(_payload(params, options, module, _args(base, call), [{"id": file_id, "name": name}],
                                {"input": file_id, "name": name}))
        return out
    entries = args_list or [[]]
    return [_payload(params, options, module, _args(entry, call), [], {"args": entry, "i": i})
            for i, entry in enumerate(entries)]


def _args(entry: Any, call: str) -> List[Any]:
    values = entry if isinstance(entry, list) else [entry]
    if call:
        out = []
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                try:
                    value = float(value) if "." in str(value) else int(str(value), 0)
                except ValueError:
                    raise ValueError("a call takes numbers, got %r" % (value,))
            out.append(value)
        return out
    return [str(v) for v in values]


def reduce(params: Dict[str, Any], results: List[Dict[str, Any]], ctx: Any) -> Any:
    combine = str(params.get("combine") or "auto")
    if combine == "auto":
        combine = "sum" if _range(params) is not None else "list"
    values = [r.get("result") for r in results]
    if combine == "sum":
        total: Any = 0
        for value in values:
            if isinstance(value, list):
                value = sum(v for v in value if isinstance(v, (int, float)))
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total += value
        return {"sum": total, "tasks": len(values)}
    if combine == "text":
        return "".join(v if isinstance(v, str) else ("" if v is None else str(v)) for v in values)
    if params.get("inputs") and not str(params.get("call") or "").strip():
        return [{"input": (r.get("meta") or {}).get("name"), "result": r.get("result"),
                 "outputs": r.get("outputs") or []} for r in results]
    return values
