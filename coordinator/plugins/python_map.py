"""``python_map`` -- run ``fn(item, ctx)`` over a list of items (SPEC §7)."""

from __future__ import annotations

from typing import Any, Dict, List

TYPE = "python_map"
DESCRIPTION = "Run a Python function fn(item, ctx) over a list of items"

PARAMS = [
    {
        "name": "code",
        "type": "code",
        "default": "def fn(item, ctx):\n    return item * item\n",
        "help": "Python source that defines the mapper function.",
    },
    {"name": "fn_name", "type": "str", "default": "fn", "help": "Function to call."},
    {"name": "items", "type": "json", "default": [], "help": "List of items (JSON)."},
    {"name": "items_file", "type": "file", "default": None, "help": "file_id of a JSON list (instead of items)."},
    {"name": "lines_file", "type": "file", "default": None,
     "help": "file_id of a big text/JSONL file: each line is an item, streamed by byte range (never loaded whole)."},
    {"name": "lines_per_task", "type": "int", "default": 0,
     "help": "Lines per task when using lines_file; 0 = automatic."},
    {"name": "line_format", "type": "select", "default": "auto", "choices": ["auto", "text", "json"],
     "help": "How to parse lines_file lines: text, json (one object per line), auto by extension (.jsonl/.ndjson)."},
    {"name": "chunk_size", "type": "int", "default": 0,
     "help": "Items per task; 0 = automatic (sized to each device's speed)."},
    {"name": "js_code", "type": "code", "default": None, "help": "Optional JS mapper for browser workers."},
    {"name": "inputs", "type": "files", "default": [], "help": "file_ids downloaded before the task runs."},
]

DEFAULT_OPTIONS: Dict[str, Any] = {"requires": []}

#: same code + items -> same result: reuse it, and let a second device check it
#: (a function that reads the clock or the network can say ``cache: false``)
CACHE = True
VERIFY = True

#: Runner appended *after* the user's code so ``run`` is always ours.
#: Every finished item is checkpointed (SPEC §6.5), so a chunk that gets
#: requeued mid-way resumes after the last completed item instead of
#: restarting.  ``ctx.checkpoint`` is throttled by the worker, so calling it
#: per item is cheap.  Items come inline (``items``) or from a byte range of
#: a file (``range`` + ``line_format``, see ``lines_file``).
WORKER_PY = '''

def __pc_items(args, ctx):
    rng = args.get("range")
    if not rng:
        return list(args.get("items") or [])
    path = ctx.input(rng.get("input", 0))
    fmt = args.get("line_format") or "text"
    out = []
    fh = open(path, "rb")
    try:
        for raw in fh:
            line = raw.decode("utf-8", "replace").rstrip("\\r\\n")
            if fmt == "json":
                if not line.strip():
                    continue
                import json as _json
                out.append(_json.loads(line))
            else:
                out.append(line)
    finally:
        fh.close()
    return out


def __pc_run(args, ctx):
    name = args.get("fn_name") or "fn"
    fn = globals().get(name)
    if not callable(fn):
        raise NameError("mapper function %r is not defined" % (name,))
    items = __pc_items(args, ctx)
    total = len(items)
    saved = ctx.resume() if hasattr(ctx, "resume") else None
    out = []
    if isinstance(saved, dict) and isinstance(saved.get("out"), list) \
            and saved.get("n") == total:
        out = list(saved["out"])
        if out:
            ctx.log("[map] resuming after %d of %d items" % (len(out), total))
    for i in range(len(out), total):
        out.append(fn(items[i], ctx))
        if total:
            ctx.progress(float(i + 1) / total)
        if hasattr(ctx, "checkpoint") and i + 1 < total:
            try:
                ctx.checkpoint({"n": total, "out": out})
            except Exception:
                pass
    return out


run = __pc_run
'''

WORKER_JS = '''

async function __pcRun(args, ctx) {
  var name = args.fn_name || 'fn';
  var fn = (typeof self !== 'undefined' && self[name]) || (typeof globalThis !== 'undefined' && globalThis[name]);
  if (typeof fn !== 'function') { throw new Error('mapper function ' + name + ' is not defined'); }
  var items = args.items || [];
  if (args.range) {
    var buf = ctx.inputs[args.range.input || 0];
    var text = new TextDecoder('utf-8').decode(buf);
    var lines = text.split(/\\r?\\n/);
    items = [];
    for (var k = 0; k < lines.length; k++) {
      if (args.line_format === 'json') {
        if (lines[k].trim()) { items.push(JSON.parse(lines[k])); }
      } else if (k < lines.length - 1 || lines[k] !== '') {
        items.push(lines[k]);
      }
    }
  }
  var out = [];
  var saved = ctx.resume ? ctx.resume() : null;
  if (saved && Array.isArray(saved.out) && saved.n === items.length) {
    out = saved.out.slice();
    if (out.length) { ctx.log('[map] resuming after ' + out.length + ' of ' + items.length + ' items'); }
  }
  for (var i = out.length; i < items.length; i++) {
    out.push(await fn(items[i], ctx));
    if (items.length) { ctx.progress((i + 1) / items.length); }
    if (ctx.checkpoint && i + 1 < items.length) { ctx.checkpoint({ n: items.length, out: out }); }
  }
  return out;
}

var run = __pcRun;
'''


def _items(params: Dict[str, Any], ctx: Any) -> List[Any]:
    file_id = params.get("items_file")
    if file_id:
        data = ctx.read_json(str(file_id))
        if not isinstance(data, list):
            raise ValueError("items_file must contain a JSON list")
        return list(data)
    items = params.get("items")
    if items is None:
        return []
    if not isinstance(items, list):
        raise ValueError("items must be a list")
    return list(items)


def _payload(code: Any, js_code: Any, args: Dict[str, Any], inputs: List[Any],
             options: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
    impls: Dict[str, Any] = {"python": {"code": code + WORKER_PY, "entry": "run", "args": args}}
    if js_code:
        impls["js"] = {"code": str(js_code) + WORKER_JS, "entry": "run", "args": args}
    return {
        "impls": impls,
        "inputs": inputs,
        "timeout": options.get("timeout"),
        "requires": list(options.get("requires") or []),
        "meta": meta,
    }


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _code(params: Dict[str, Any]) -> str:
    code = str(params.get("code") or "")
    if not code.strip():
        raise ValueError("python_map needs 'code' defining the mapper function")
    return code


def _line_format(params: Dict[str, Any], meta: Dict[str, Any]) -> str:
    fmt = str(params.get("line_format") or "auto").lower()
    if fmt == "auto":
        name = str(meta.get("name") or "").lower()
        fmt = "json" if name.endswith((".jsonl", ".ndjson")) else "text"
    return fmt


def _lines_meta(params: Dict[str, Any], ctx: Any) -> Dict[str, Any]:
    file_id = str(params.get("lines_file"))
    meta = ctx.file_meta(file_id)
    if meta is None:
        raise ValueError("lines_file %r is not in the file store" % file_id)
    return meta


def _lines_payload(params: Dict[str, Any], options: Dict[str, Any], fmt: str,
                   offset: int, length: int, first_line: int, lines: int, part: int) -> Dict[str, Any]:
    inputs = list(params.get("inputs") or [])
    file_id = str(params.get("lines_file"))
    slice_inputs = inputs + [{"file_id": file_id, "offset": offset, "length": length}]
    args = {
        "fn_name": str(params.get("fn_name") or "fn"),
        "start": first_line,
        "range": {"input": len(inputs)},
        "line_format": fmt,
    }
    return _payload(_code(params), params.get("js_code"), args, slice_inputs, options,
                    {"start": first_line, "offset": offset, "length": length, "part": part,
                     "units": lines})


def _split_lines(params: Dict[str, Any], options: Dict[str, Any], ctx: Any) -> List[Dict[str, Any]]:
    """Streaming input (SPEC §6.6): one task per ``lines_per_task`` lines of a
    big file.  The coordinator scans the file once for byte offsets; each
    worker downloads only its slice with an HTTP Range request."""
    file_id = str(params.get("lines_file"))
    fmt = _line_format(params, _lines_meta(params, ctx))
    per = _int(params.get("lines_per_task")) or 1000
    ranges = ctx.line_ranges(file_id, per)
    payloads: List[Dict[str, Any]] = []
    for idx, (offset, length, first_line) in enumerate(ranges):
        following = ranges[idx + 1][2] if idx + 1 < len(ranges) else None
        lines = (following - first_line) if following is not None else per
        payloads.append(_lines_payload(params, options, fmt, offset, length, first_line, lines, idx))
    return payloads


def _items_payload(params: Dict[str, Any], options: Dict[str, Any], items: List[Any], start: int) -> Dict[str, Any]:
    args = {"items": items, "fn_name": str(params.get("fn_name") or "fn"), "start": start}
    return _payload(_code(params), params.get("js_code"), args, list(params.get("inputs") or []),
                    options, {"start": start, "count": len(items), "units": len(items)})


def speed_key(params: Dict[str, Any]) -> str:
    """Different functions run at different speeds: remember them apart."""
    import hashlib

    text = "\0".join([str(params.get("code") or ""), str(params.get("fn_name") or "fn"),
                      str(params.get("js_code") or "")])
    return "python_map:" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def work(params: Dict[str, Any], options: Dict[str, Any], ctx: Any) -> Any:
    """Automatic sizing (SPEC §9.6) unless a chunk size / lines per task was given.

    Items: one unit per item.  A lines file: units are blocks of lines whose
    byte offsets are found by one scan (the worker still downloads only its
    slice); speeds are learned per line either way.
    """
    _code(params)
    if options.get("targets") == "all":
        return None
    if params.get("lines_file"):
        if _int(params.get("lines_per_task")) > 0:
            return None
        file_id = str(params.get("lines_file"))
        fmt = _line_format(params, _lines_meta(params, ctx))
        per = 100
        blocks = ctx.line_ranges(file_id, per)
        if len(blocks) < 200:  # a small file: finer blocks
            per = 5
            blocks = ctx.line_ranges(file_id, per)
        return {"units": len(blocks), "per_unit": per, "state": {"blocks": blocks, "fmt": fmt, "per": per}}
    if _int(params.get("chunk_size")) > 0 or _int(options.get("chunk_size")) > 0:
        return None
    items = _items(params, ctx)
    return {"units": len(items), "state": {"items": items}}


def piece(params: Dict[str, Any], options: Dict[str, Any], ctx: Any, state: Any,
          lo: int, hi: int) -> Dict[str, Any]:
    if "blocks" in state:
        blocks = state["blocks"][lo:hi]
        offset = blocks[0][0]
        length = blocks[-1][0] + blocks[-1][1] - offset
        first = blocks[0][2]
        following = state["blocks"][hi][2] if hi < len(state["blocks"]) else first + len(blocks) * state["per"]
        return _lines_payload(params, options, state["fmt"], offset, length, first, following - first, lo)
    return _items_payload(params, options, state["items"][lo:hi], lo)


def split(params: Dict[str, Any], options: Dict[str, Any], ctx: Any) -> List[Dict[str, Any]]:
    _code(params)
    if params.get("lines_file"):
        return _split_lines(params, options, ctx)
    items = _items(params, ctx)
    chunk_size = max(1, _int(params.get("chunk_size")) or _int(options.get("chunk_size")) or 10)
    payloads: List[Dict[str, Any]] = []
    for start in range(0, len(items), chunk_size):
        payloads.append(_items_payload(params, options, items[start: start + chunk_size], start))
    return payloads


def reduce(params: Dict[str, Any], results: List[Dict[str, Any]], ctx: Any) -> Any:
    """Concatenate every chunk's output list, in item order."""
    out: List[Any] = []
    for item in results:
        value = item.get("result")
        if isinstance(value, list):
            out.extend(value)
        elif value is not None:
            out.append(value)
    return out
