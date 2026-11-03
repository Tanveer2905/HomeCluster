"""``monte_carlo_pi`` -- estimate pi with random sampling (SPEC §7)."""

from __future__ import annotations

from typing import Any, Dict, List

TYPE = "monte_carlo_pi"
DESCRIPTION = "Estimate pi by throwing random darts at a quarter circle"

PARAMS = [
    {"name": "samples", "type": "int", "default": 1000000, "help": "Total number of samples."},
    {"name": "chunks", "type": "int", "default": 0,
     "help": "How many tasks to split into; 0 = automatic (sized to each device's speed)."},
]

DEFAULT_OPTIONS: Dict[str, Any] = {"requires": []}
#: seeded per piece, so a repeat gives the same numbers (on the same runtime:
#: Python and JS use different generators, checks compare like with like)
CACHE = True
VERIFY = True

WORKER_PY = '''
import random


def run(args, ctx):
    n = int(args.get("samples") or 0)
    rng = random.Random(int(args.get("seed") or 0))
    inside = 0
    step = max(1, n // 20)
    for i in range(n):
        x = rng.random()
        y = rng.random()
        if x * x + y * y <= 1.0:
            inside += 1
        if step and (i + 1) % step == 0:
            ctx.progress(float(i + 1) / max(1, n))
    return {"inside": inside, "samples": n}
'''

WORKER_JS = '''
function __pcRandom(seed) {
  var s = (seed | 0) || 1;
  return function () {
    s = (s * 1103515245 + 12345) & 0x7fffffff;
    return s / 2147483648;
  };
}

async function run(args, ctx) {
  var n = args.samples | 0;
  var rand = __pcRandom(args.seed | 0);
  var inside = 0;
  var step = Math.max(1, Math.floor(n / 20));
  for (var i = 0; i < n; i++) {
    var x = rand();
    var y = rand();
    if (x * x + y * y <= 1.0) { inside++; }
    if ((i + 1) % step === 0) { ctx.progress((i + 1) / Math.max(1, n)); }
  }
  return { inside: inside, samples: n };
}
'''


def _samples(params: Dict[str, Any]) -> int:
    samples = int(params.get("samples") or 0)
    if samples <= 0:
        raise ValueError("samples must be positive")
    return samples


def _payload(options: Dict[str, Any], count: int, seed: int) -> Dict[str, Any]:
    args = {"samples": count, "seed": seed}
    return {
        "impls": {
            "python": {"code": WORKER_PY, "entry": "run", "args": args},
            "js": {"code": WORKER_JS, "entry": "run", "args": args},
        },
        "inputs": [],
        "timeout": options.get("timeout"),
        "requires": list(options.get("requires") or []),
        "meta": {"samples": count, "units": count},
    }


def _fixed_chunks(params: Dict[str, Any], options: Dict[str, Any]) -> int:
    return int(params.get("chunks") or options.get("chunk_size") or 0)


def work(params: Dict[str, Any], options: Dict[str, Any], ctx: Any) -> Any:
    """Automatic sizing unless a number of chunks was given (SPEC §9.6)."""
    if _fixed_chunks(params, options) > 0:
        return None
    return {"units": _samples(params), "grain": 1000}


def piece(params: Dict[str, Any], options: Dict[str, Any], ctx: Any, state: Any,
          lo: int, hi: int) -> Dict[str, Any]:
    # a seed per position keeps pieces independent and the run reproducible
    return _payload(options, hi - lo, 1000003 + lo)


def split(params: Dict[str, Any], options: Dict[str, Any], ctx: Any) -> List[Dict[str, Any]]:
    samples = _samples(params)
    chunks = max(1, min(_fixed_chunks(params, options) or 4, samples))
    base, extra = divmod(samples, chunks)
    payloads: List[Dict[str, Any]] = []
    for i in range(chunks):
        count = base + (1 if i < extra else 0)
        if count <= 0:
            continue
        payloads.append(_payload(options, count, 1000003 + i * 7919))
    return payloads


def reduce(params: Dict[str, Any], results: List[Dict[str, Any]], ctx: Any) -> Any:
    inside = 0
    samples = 0
    for item in results:
        value = item.get("result") or {}
        if not isinstance(value, dict):
            continue
        inside += int(value.get("inside") or 0)
        samples += int(value.get("samples") or 0)
    pi = (4.0 * inside / samples) if samples else 0.0
    return {"pi": pi, "inside": inside, "samples": samples}
