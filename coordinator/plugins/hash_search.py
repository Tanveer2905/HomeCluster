"""``hash_search`` -- brute-force a hash prefix over a numeric range (SPEC §7)."""

from __future__ import annotations

from typing import Any, Dict, List

TYPE = "hash_search"
DESCRIPTION = "Find numbers whose sha256/md5 digest starts with a prefix"

PARAMS = [
    {"name": "algo", "type": "select", "default": "sha256", "choices": ["sha256", "md5"]},
    {"name": "prefix", "type": "str", "default": "0000", "help": "Hex prefix to look for."},
    {"name": "start", "type": "int", "default": 0, "help": "First number to try."},
    {"name": "count", "type": "int", "default": 1000000, "help": "How many numbers to try."},
    {"name": "chunk_size", "type": "int", "default": 0,
     "help": "Numbers per task; 0 = automatic (sized to each device's speed)."},
    {"name": "salt", "type": "str", "default": "", "help": "Prepended to every candidate."},
    {"name": "max_results", "type": "int", "default": 10, "help": "Stop reporting after this many matches."},
]

DEFAULT_OPTIONS: Dict[str, Any] = {"requires": []}
CACHE = True
VERIFY = True

WORKER_PY = '''
import hashlib


def run(args, ctx):
    algo = args.get("algo") or "sha256"
    prefix = (args.get("prefix") or "").lower()
    salt = args.get("salt") or ""
    start = int(args.get("start") or 0)
    count = int(args.get("count") or 0)
    limit = int(args.get("max_results") or 10)
    matches = []
    searched = 0
    step = max(1, count // 20)
    for n in range(start, start + count):
        digest = hashlib.new(algo, (salt + str(n)).encode("utf-8")).hexdigest()
        searched += 1
        if not prefix or digest.startswith(prefix):
            matches.append({"n": n, "hash": digest})
            if len(matches) >= limit:
                break
        if step and searched % step == 0:
            ctx.progress(float(searched) / max(1, count))
    return {"matches": matches, "searched": searched}
'''

WORKER_JS = '''
async function sha256Hex(text) {
  var buf = new TextEncoder().encode(text);
  var digest = await crypto.subtle.digest('SHA-256', buf);
  var bytes = new Uint8Array(digest);
  var out = '';
  for (var i = 0; i < bytes.length; i++) { out += bytes[i].toString(16).padStart(2, '0'); }
  return out;
}

async function run(args, ctx) {
  if ((args.algo || 'sha256') !== 'sha256') { throw new Error('browser workers only support sha256'); }
  var prefix = (args.prefix || '').toLowerCase();
  var salt = args.salt || '';
  var start = args.start | 0;
  var count = args.count | 0;
  var limit = args.max_results || 10;
  var matches = [];
  var searched = 0;
  var step = Math.max(1, Math.floor(count / 20));
  for (var n = start; n < start + count; n++) {
    var digest = await sha256Hex(salt + String(n));
    searched++;
    if (!prefix || digest.indexOf(prefix) === 0) {
      matches.push({ n: n, hash: digest });
      if (matches.length >= limit) { break; }
    }
    if (searched % step === 0) { ctx.progress(searched / Math.max(1, count)); }
  }
  return { matches: matches, searched: searched };
}
'''


def _check(params: Dict[str, Any]) -> Any:
    algo = str(params.get("algo") or "sha256").lower()
    if algo not in ("sha256", "md5"):
        raise ValueError("algo must be sha256 or md5")
    count = int(params.get("count") or 0)
    if count <= 0:
        raise ValueError("count must be positive")
    return algo, count


def _payload(params: Dict[str, Any], options: Dict[str, Any], offset: int, size: int) -> Dict[str, Any]:
    algo = _check(params)[0]
    start = int(params.get("start") or 0)
    args = {
        "algo": algo,
        "prefix": str(params.get("prefix") or ""),
        "salt": str(params.get("salt") or ""),
        "start": start + offset,
        "count": size,
        "max_results": int(params.get("max_results") or 10),
    }
    impls: Dict[str, Any] = {
        "python": {"code": WORKER_PY, "entry": "run", "args": args},
    }
    if algo == "sha256":
        impls["js"] = {"code": WORKER_JS, "entry": "run", "args": args}
    return {
        "impls": impls,
        "inputs": [],
        "timeout": options.get("timeout"),
        "requires": list(options.get("requires") or []),
        "meta": {"start": start + offset, "count": size, "units": size},
    }


def _fixed_chunk(params: Dict[str, Any], options: Dict[str, Any]) -> int:
    return int(params.get("chunk_size") or options.get("chunk_size") or 0)


def speed_key(params: Dict[str, Any]) -> str:
    return "hash_search:%s" % str(params.get("algo") or "sha256").lower()


def work(params: Dict[str, Any], options: Dict[str, Any], ctx: Any) -> Any:
    """Automatic sizing unless a chunk size was given (SPEC §9.6)."""
    if _fixed_chunk(params, options) > 0:
        return None
    return {"units": _check(params)[1], "grain": 100}


def piece(params: Dict[str, Any], options: Dict[str, Any], ctx: Any, state: Any,
          lo: int, hi: int) -> Dict[str, Any]:
    return _payload(params, options, lo, hi - lo)


def split(params: Dict[str, Any], options: Dict[str, Any], ctx: Any) -> List[Dict[str, Any]]:
    count = _check(params)[1]
    chunk = max(1, _fixed_chunk(params, options) or 100000)
    payloads: List[Dict[str, Any]] = []
    offset = 0
    while offset < count:
        size = min(chunk, count - offset)
        payloads.append(_payload(params, options, offset, size))
        offset += size
    return payloads


def reduce(params: Dict[str, Any], results: List[Dict[str, Any]], ctx: Any) -> Any:
    matches: List[Dict[str, Any]] = []
    searched = 0
    for item in results:
        value = item.get("result") or {}
        if not isinstance(value, dict):
            continue
        searched += int(value.get("searched") or 0)
        for match in value.get("matches") or []:
            matches.append(match)
    matches.sort(key=lambda m: m.get("n", 0))
    limit = int(params.get("max_results") or 10)
    if limit > 0:
        matches = matches[:limit]
    return {"matches": matches, "searched": searched}
