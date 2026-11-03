"""``benchmark`` -- score every online device (SPEC §7).

The loop below is the definition from SPEC §7 and must stay byte-for-byte
equivalent to the one in ``worker/worker.py`` and the browser worker so the
scores can be compared.
"""

from __future__ import annotations

from typing import Any, Dict, List

TYPE = "benchmark"
DESCRIPTION = "Measure device speed: integer loop for 2 s, sha256 for 1 s"

PARAMS: List[Dict[str, Any]] = []

DEFAULT_OPTIONS: Dict[str, Any] = {"targets": "all", "timeout": 120, "max_attempts": 1}

WORKER_PY = '''
import hashlib
import time


def run(args, ctx):
    x = 42
    iters = 0
    batch = 20000
    start = time.time()
    end = start + 2.0
    while True:
        for _ in range(batch):
            x = (x * 1103515245 + 12345) & 0x7fffffff
        iters += batch
        left = end - time.time()
        if left <= 0:
            break
        rate = iters / max(1e-6, time.time() - start)
        batch = max(10000, min(2000000, int(rate * min(left, 0.25)) or 10000))
    buf = b"\\xa5" * (1024 * 1024)
    rounds = 0
    hstart = time.time()
    hend = hstart + 1.0
    while time.time() < hend:
        hashlib.sha256(buf).digest()
        rounds += 1
    elapsed = max(1e-6, time.time() - hstart)
    ctx.log("benchmark: %d loop iterations, %.1f MB/s sha256" % (iters, rounds / elapsed))
    return {
        "score": iters / 1e6,
        "loop_iters": iters,
        "hash_mb_s": round(rounds / elapsed, 3),
        "kind": "python",
    }
'''

WORKER_JS = '''
async function run(args, ctx) {
  var x = 42;
  var iters = 0;
  var batch = 200000;
  var start = Date.now();
  var end = start + 2000;
  while (true) {
    for (var i = 0; i < batch; i++) { x = (x * 1103515245 + 12345) & 0x7fffffff; }
    iters += batch;
    var left = end - Date.now();
    if (left <= 0) { break; }
    var rate = iters / Math.max(1, Date.now() - start);
    batch = Math.max(100000, Math.min(20000000, Math.floor(rate * Math.min(left, 250)) || 100000));
  }
  var buf = new Uint8Array(1024 * 1024);
  buf.fill(0xa5);
  var rounds = 0;
  var hstart = Date.now();
  while (Date.now() - hstart < 1000) {
    await crypto.subtle.digest('SHA-256', buf);
    rounds++;
  }
  var elapsed = Math.max(1, Date.now() - hstart) / 1000;
  ctx.log('benchmark: ' + iters + ' loop iterations');
  return {
    score: iters / 25 / 1e6,
    loop_iters: iters,
    hash_mb_s: Math.round((rounds / elapsed) * 1000) / 1000,
    kind: 'js'
  };
}
'''


def split(params: Dict[str, Any], options: Dict[str, Any], ctx: Any) -> List[Dict[str, Any]]:
    devices = ctx.devices()
    targets = options.get("targets") or "all"
    if isinstance(targets, (list, tuple)):
        wanted = {str(t) for t in targets}
        devices = [d for d in devices if d["id"] in wanted]
    if not devices:
        raise ValueError("no online device to benchmark")
    timeout = options.get("timeout") or 120
    payloads: List[Dict[str, Any]] = []
    for device in devices:
        caps = device.get("caps") or []
        impls: Dict[str, Any] = {}
        if "python" in caps:
            impls["python"] = {"code": WORKER_PY, "entry": "run", "args": {}}
        if "js" in caps:
            impls["js"] = {"code": WORKER_JS, "entry": "run", "args": {}}
        if not impls:
            continue
        payloads.append(
            {
                "impls": impls,
                "inputs": [],
                "timeout": timeout,
                "requires": [],
                "device_id": device["id"],
                "meta": {"device": device.get("name")},
            }
        )
    if not payloads:
        raise ValueError("no online device can run the benchmark")
    return payloads


def reduce(params: Dict[str, Any], results: List[Dict[str, Any]], ctx: Any) -> Any:
    """Store ``bench_score`` per device and return the per-device results."""
    out: Dict[str, Any] = {}
    for item in results:
        device_id = item.get("device_id")
        value = item.get("result")
        if not isinstance(value, dict):
            continue
        out[str(device_id)] = value
        if device_id and value.get("score") is not None:
            try:
                ctx.set_bench(str(device_id), float(value["score"]), value)
            except Exception:  # pragma: no cover - defensive
                pass
    return out
