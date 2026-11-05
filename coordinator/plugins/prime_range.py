"""``prime_range`` -- count (and optionally list) primes in a range (SPEC §7)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ._wasm_kernels import kernel, wasm_impl

TYPE = "prime_range"
DESCRIPTION = "Count the primes in [start, end) across the cluster"

PARAMS = [
    {"name": "start", "type": "int", "default": 2, "help": "First number (inclusive)."},
    {"name": "end", "type": "int", "default": 1000000, "help": "Last number (exclusive)."},
    {"name": "chunk_size", "type": "int", "default": 0,
     "help": "Numbers per task; 0 = automatic (sized to each device's speed)."},
    {"name": "return_list", "type": "bool", "default": False, "help": "Also return every prime."},
]

DEFAULT_OPTIONS: Dict[str, Any] = {"requires": []}
CACHE = True
VERIFY = True

WORKER_PY = '''

def run(args, ctx):
    start = int(args.get("start") or 0)
    end = int(args.get("end") or 0)
    want_list = bool(args.get("return_list"))
    if start < 2:
        start = 2
    size = max(0, end - start)
    if size == 0:
        return {"count": 0, "primes": [] if want_list else None}
    sieve = [True] * size
    limit = int(end ** 0.5) + 1
    p = 2
    while p <= limit:
        first = max(p * p, ((start + p - 1) // p) * p)
        for multiple in range(first, end, p):
            sieve[multiple - start] = False
        p += 1
    count = 0
    primes = []
    step = max(1, size // 20)
    for i in range(size):
        if sieve[i]:
            count += 1
            if want_list:
                primes.append(start + i)
        if step and (i + 1) % step == 0:
            ctx.progress(float(i + 1) / size)
    out = {"count": count}
    if want_list:
        out["primes"] = primes
    return out
'''

WORKER_JS = '''
async function run(args, ctx) {
  var start = Math.max(2, args.start | 0);
  var end = args.end | 0;
  var wantList = !!args.return_list;
  var size = Math.max(0, end - start);
  if (size === 0) { return wantList ? { count: 0, primes: [] } : { count: 0 }; }
  var sieve = new Uint8Array(size);
  var limit = Math.floor(Math.sqrt(end)) + 1;
  for (var p = 2; p <= limit; p++) {
    var first = Math.max(p * p, Math.ceil(start / p) * p);
    for (var m = first; m < end; m += p) { sieve[m - start] = 1; }
  }
  var count = 0;
  var primes = [];
  var step = Math.max(1, Math.floor(size / 20));
  for (var i = 0; i < size; i++) {
    if (!sieve[i]) { count++; if (wantList) { primes.push(start + i); } }
    if ((i + 1) % step === 0) { ctx.progress((i + 1) / size); }
  }
  return wantList ? { count: count, primes: primes } : { count: count };
}
'''


def _bounds(params: Dict[str, Any]) -> Any:
    start = int(params.get("start") or 0)
    end = int(params.get("end") or 0)
    if end <= start:
        raise ValueError("end must be greater than start")
    return start, end


def _payload(params: Dict[str, Any], options: Dict[str, Any], lo: int, hi: int,
             module: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    args = {"start": lo, "end": hi, "return_list": bool(params.get("return_list"))}
    impls: Dict[str, Any] = {
        "python": {"code": WORKER_PY, "entry": "run", "args": args},
        "js": {"code": WORKER_JS, "entry": "run", "args": args},
    }
    inputs: List[str] = []
    if module is not None and not args["return_list"]:
        # where wasmtime is ready the count comes from WebAssembly, many times faster
        impls["wasm"] = wasm_impl(module, [max(2, lo), hi])
        inputs = [module["id"]]
    return {
        "impls": impls,
        "inputs": inputs,
        "timeout": options.get("timeout"),
        "requires": list(options.get("requires") or []),
        "meta": {"start": lo, "end": hi, "units": hi - lo},
    }


def _fixed_chunk(params: Dict[str, Any], options: Dict[str, Any]) -> int:
    return int(params.get("chunk_size") or options.get("chunk_size") or 0)


def work(params: Dict[str, Any], options: Dict[str, Any], ctx: Any) -> Any:
    """Automatic sizing unless a chunk size was given (SPEC §9.6)."""
    if _fixed_chunk(params, options) > 0:
        return None
    start, end = _bounds(params)
    return {"units": end - start, "grain": 1000, "state": {"module": kernel("count_primes", ctx)}}


def piece(params: Dict[str, Any], options: Dict[str, Any], ctx: Any, state: Any,
          lo: int, hi: int) -> Dict[str, Any]:
    start = _bounds(params)[0]
    return _payload(params, options, start + lo, start + hi, (state or {}).get("module"))


def split(params: Dict[str, Any], options: Dict[str, Any], ctx: Any) -> List[Dict[str, Any]]:
    start, end = _bounds(params)
    chunk = max(1, _fixed_chunk(params, options) or 100000)
    module = kernel("count_primes", ctx)
    payloads: List[Dict[str, Any]] = []
    lo = start
    while lo < end:
        hi = min(lo + chunk, end)
        payloads.append(_payload(params, options, lo, hi, module))
        lo = hi
    return payloads


def reduce(params: Dict[str, Any], results: List[Dict[str, Any]], ctx: Any) -> Any:
    count = 0
    primes: List[int] = []
    want_list = bool(params.get("return_list"))
    for item in results:
        value = item.get("result")
        if isinstance(value, int) and not isinstance(value, bool):
            count += value  # the WebAssembly version answers with the count alone
            continue
        value = value or {}
        if not isinstance(value, dict):
            continue
        count += int(value.get("count") or 0)
        if want_list:
            primes.extend(value.get("primes") or [])
    out: Dict[str, Any] = {"count": count}
    if want_list:
        out["primes"] = primes
    return out
