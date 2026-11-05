"""WebAssembly versions of the built-in number crunching (SPEC §7, §6.9).

Python is slow on phones.  Where wasmtime is ready or can be installed (the PC,
Termux phones) ``prime_range`` and ``monte_carlo_pi`` run these two tiny modules
instead: the same answers, many times faster.  Browsers keep their JavaScript
versions, which their engines already compile to native code.

No compiler is needed: the bytes are assembled right here.  The readable form
of each function is in its docstring.
"""

from __future__ import annotations

import hashlib
import struct
from typing import Any, Dict, Optional, Sequence

I32, I64, F64 = 0x7F, 0x7E, 0x7C

OPS = {
    "block": 0x02, "loop": 0x03, "if": 0x04, "else": 0x05, "end": 0x0B, "br": 0x0C, "br_if": 0x0D,
    "return": 0x0F, "select": 0x1B, "local.get": 0x20, "local.set": 0x21, "local.tee": 0x22,
    "i32.load8_u": 0x2D, "i64.store": 0x37, "i32.store8": 0x3A,
    "i32.const": 0x41, "i64.const": 0x42, "f64.const": 0x44,
    "i32.eqz": 0x45, "i32.ge_u": 0x4F, "i32.add": 0x6A,
    "i64.eqz": 0x50, "i64.eq": 0x51, "i64.lt_u": 0x54, "i64.gt_u": 0x56, "i64.ge_u": 0x5A,
    "f64.le": 0x65,
    "i64.add": 0x7C, "i64.sub": 0x7D, "i64.mul": 0x7E, "i64.div_u": 0x80, "i64.xor": 0x85,
    "i64.shl": 0x86, "i64.shr_u": 0x88, "f64.add": 0xA0, "f64.mul": 0xA2,
    "i32.wrap_i64": 0xA7, "f64.convert_i64_u": 0xBA,
}
MEMORY_OPS = {"i32.load8_u": 0, "i32.store8": 0, "i64.store": 3}   # natural alignment (log2)


def uleb(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def sleb(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        done = (n == 0 and not byte & 0x40) or (n == -1 and byte & 0x40)
        out.append(byte | (0 if done else 0x80))
        if done:
            return bytes(out)


def signed64(n: int) -> int:
    return n - (1 << 64) if n >= 1 << 63 else n


def code(*ops: Any) -> bytes:
    out = bytearray()
    for op in ops:
        if isinstance(op, str):
            op = (op,)
        word, args = op[0], op[1:]
        out.append(OPS[word])
        if word in ("block", "loop", "if"):
            out.append(0x40)  # no result
        elif word in ("i32.const", "i64.const"):
            out += sleb(signed64(args[0]) if word == "i64.const" else args[0])
        elif word == "f64.const":
            out += struct.pack("<d", float(args[0]))
        elif word in MEMORY_OPS:
            out += uleb(MEMORY_OPS[word]) + uleb(args[0] if args else 0)
        else:
            for arg in args:
                out += uleb(arg)
    return bytes(out)


def vec(items: Sequence[bytes]) -> bytes:
    return uleb(len(items)) + b"".join(items)


def section(sid: int, body: bytes) -> bytes:
    return bytes([sid]) + uleb(len(body)) + body


def single_function(name: str, params: Sequence[int], result: int, locals_: Sequence[int],
                    instructions: bytes, pages: int = 0) -> bytes:
    """A module exporting one function (and, with ``pages``, a private memory)."""
    functype = b"\x60" + vec([bytes([p]) for p in params]) + vec([bytes([result])])
    groups = [uleb(1) + bytes([t]) for t in locals_]
    inner = vec(groups) + instructions + b"\x0b"
    out = b"\x00asm\x01\x00\x00\x00"
    out += section(1, vec([functype]))
    out += section(3, vec([uleb(0)]))
    if pages:
        out += section(5, vec([b"\x00" + uleb(pages)]))
    raw = name.encode("utf-8")
    out += section(7, vec([uleb(len(raw)) + raw + b"\x00" + uleb(0)]))
    out += section(10, vec([uleb(len(inner)) + inner]))
    return out


# ----------------------------------------------------------------- primes
BLOCK = 1 << 18   # bytes sieved at a time: small enough for a phone's cache


def count_primes() -> bytes:
    """count_primes(lo: i64, hi: i64) -> i64: the primes in [lo, hi), lo >= 2.

    A segmented sieve: for each block of 256 KiB numbers, every p with
    p*p < block end (2, then odd p) crosses out its multiples from
    max(p*p, the first multiple in the block); the zeros left are primes.
    """
    lo, hi, seg, end, length, p, m, count, i, pp = range(10)
    body = code(
        ("local.get", lo), ("local.set", seg),
        ("block",), ("loop",),                                       # each block
        ("local.get", seg), ("local.get", hi), "i64.ge_u", ("br_if", 1),
        # end = min(seg + BLOCK, hi); length = end - seg
        ("local.get", hi), ("local.get", seg), ("i64.const", BLOCK), "i64.add", ("local.tee", end),
        ("local.get", end), ("local.get", hi), "i64.gt_u", "select", ("local.set", end),
        ("local.get", end), ("local.get", seg), "i64.sub", "i32.wrap_i64", ("local.set", length),
        # clear the block, 8 bytes at a time
        ("i32.const", 0), ("local.set", i),
        ("block",), ("loop",),
        ("local.get", i), ("local.get", length), "i32.ge_u", ("br_if", 1),
        ("local.get", i), ("i64.const", 0), ("i64.store",),
        ("local.get", i), ("i32.const", 8), "i32.add", ("local.set", i),
        ("br", 0), "end", "end",
        # cross out multiples
        ("i64.const", 2), ("local.set", p),
        ("block",), ("loop",),
        ("local.get", p), ("local.get", p), "i64.mul", ("local.tee", pp),
        ("local.get", end), "i64.ge_u", ("br_if", 1),
        ("local.get", seg), ("local.get", p), "i64.add", ("i64.const", 1), "i64.sub",
        ("local.get", p), "i64.div_u", ("local.get", p), "i64.mul", ("local.set", m),
        ("local.get", pp), ("local.get", m), ("local.get", m), ("local.get", pp), "i64.lt_u", "select",
        ("local.set", m),
        ("block",), ("loop",),
        ("local.get", m), ("local.get", end), "i64.ge_u", ("br_if", 1),
        ("local.get", m), ("local.get", seg), "i64.sub", "i32.wrap_i64", ("i32.const", 1), ("i32.store8",),
        ("local.get", m), ("local.get", p), "i64.add", ("local.set", m),
        ("br", 0), "end", "end",
        # next p: 2 -> 3, then odd numbers
        ("i64.const", 3), ("local.get", p), ("i64.const", 2), "i64.add",
        ("local.get", p), ("i64.const", 2), "i64.eq", "select", ("local.set", p),
        ("br", 0), "end", "end",
        # count what is left
        ("i32.const", 0), ("local.set", i),
        ("block",), ("loop",),
        ("local.get", i), ("local.get", length), "i32.ge_u", ("br_if", 1),
        ("local.get", i), ("i32.load8_u",), "i32.eqz",
        ("if",), ("local.get", count), ("i64.const", 1), "i64.add", ("local.set", count), "end",
        ("local.get", i), ("i32.const", 1), "i32.add", ("local.set", i),
        ("br", 0), "end", "end",
        ("local.get", end), ("local.set", seg),
        ("br", 0), "end", "end",
        ("local.get", count),
    )
    locals_ = [I64, I64, I32, I64, I64, I64, I32, I64]   # seg end length p m count i pp
    pages = (BLOCK + 8 + 65535) // 65536 + 1
    return single_function("count_primes", [I64, I64], I64, locals_, body, pages)


# ------------------------------------------------------------ monte carlo
GOLDEN = 0x9E3779B97F4A7C15


def count_inside() -> bytes:
    """count_inside(n: i64, seed: i64) -> i64: of n random points in the unit
    square, how many fall inside the quarter circle.

    Random numbers: xorshift64 (s ^= s<<13; s ^= s>>7; s ^= s<<17) from
    seed ^ 0x9E3779B97F4A7C15; a coordinate is the top 53 bits / 2**53.
    """
    n, seed, s, i, inside, x, y = range(7)

    def draw(target: int) -> tuple:
        return (
            ("local.get", s), ("local.get", s), ("i64.const", 13), "i64.shl", "i64.xor", ("local.set", s),
            ("local.get", s), ("local.get", s), ("i64.const", 7), "i64.shr_u", "i64.xor", ("local.set", s),
            ("local.get", s), ("local.get", s), ("i64.const", 17), "i64.shl", "i64.xor", ("local.set", s),
            ("local.get", s), ("i64.const", 11), "i64.shr_u", "f64.convert_i64_u",
            ("f64.const", 2.0 ** -53), "f64.mul", ("local.set", target),
        )

    body = code(
        ("local.get", seed), ("i64.const", GOLDEN), "i64.xor", ("local.tee", s), "i64.eqz",
        ("if",), ("i64.const", GOLDEN), ("local.set", s), "end",
        ("block",), ("loop",),
        ("local.get", i), ("local.get", n), "i64.ge_u", ("br_if", 1),
        *draw(x), *draw(y),
        ("local.get", x), ("local.get", x), "f64.mul", ("local.get", y), ("local.get", y), "f64.mul",
        "f64.add", ("f64.const", 1.0), "f64.le",
        ("if",), ("local.get", inside), ("i64.const", 1), "i64.add", ("local.set", inside), "end",
        ("local.get", i), ("i64.const", 1), "i64.add", ("local.set", i),
        ("br", 0), "end", "end",
        ("local.get", inside),
    )
    return single_function("count_inside", [I64, I64], I64, [I64, I64, I64, F64, F64], body)


# ------------------------------------------------------------- for plugins
MODULES = {"count_primes": count_primes(), "count_inside": count_inside()}


def kernel(name: str, ctx: Any) -> Optional[Dict[str, Any]]:
    """The module for ``name`` in the file store (put there on first use), or
    None without a coordinator to hold it."""
    if ctx is None or not hasattr(ctx, "add_file"):
        return None
    data = MODULES[name]
    file_id = hashlib.sha256(data).hexdigest()
    if ctx.file_meta(file_id) is None:
        ctx.add_file(data, "builtin-%s.wasm" % name)
    return {"id": file_id, "call": name}


def wasm_impl(module: Dict[str, Any], args: Sequence[int]) -> Dict[str, Any]:
    """The ``wasm`` impl of a task: call the module's function with two i64s."""
    return {"module": 0, "call": module["call"], "args": [int(a) for a in args], "env": {},
            "memory_mb": 16, "params": ["i64", "i64"], "results": ["i64"]}
