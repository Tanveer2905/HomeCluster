"""Build the example WebAssembly modules in this folder, with no compiler.

    python examples/wasm/build_examples.py

writes ``primes.wasm``, ``wordcount.wasm`` and ``upper.wasm`` next to this
file.  They are tiny hand-assembled modules (``primes.wat`` is the readable
form of the first); real programs come from a compiler -- see docs/WASM.md:

* ``primes.wasm`` exports ``count_primes(lo, hi) -> i32``: the number of
  primes in ``[lo, hi)``.  Run it as a function call (``call`` in the job).
* ``wordcount.wasm`` is a WASI program: reads stdin, prints
  ``{"bytes":..,"lines":..,"words":..}`` to stdout.
* ``upper.wasm`` is a WASI program: reads stdin, writes it in capitals to
  ``/out/upper.txt`` and prints ``{"bytes":..}``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List, Sequence, Tuple

I32, I64 = 0x7F, 0x7E

OPS = {
    "unreachable": 0x00, "nop": 0x01, "block": 0x02, "loop": 0x03, "if": 0x04, "else": 0x05,
    "end": 0x0B, "br": 0x0C, "br_if": 0x0D, "return": 0x0F, "call": 0x10, "drop": 0x1A,
    "local.get": 0x20, "local.set": 0x21, "local.tee": 0x22, "global.get": 0x23, "global.set": 0x24,
    "i32.load": 0x28, "i32.load8_u": 0x2D, "i32.store": 0x36, "i32.store8": 0x3A,
    "i32.const": 0x41, "i32.eqz": 0x45, "i32.eq": 0x46, "i32.ne": 0x47, "i32.lt_u": 0x49,
    "i32.gt_u": 0x4B, "i32.ge_u": 0x4F, "i32.add": 0x6A, "i32.sub": 0x6B, "i32.mul": 0x6C,
    "i32.div_u": 0x6E, "i32.rem_u": 0x70, "i32.and": 0x71, "i32.or": 0x72,
    "i64.const": 0x42, "i32.le_u": 0x4D,
}


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


def name(text: str) -> bytes:
    raw = text.encode("utf-8")
    return uleb(len(raw)) + raw


def vec(items: Sequence[bytes]) -> bytes:
    return uleb(len(items)) + b"".join(items)


def code(*ops: Any) -> bytes:
    """``("i32.const", 5)``, ``"i32.add"``, ``("block",)`` ... -> bytes."""
    out = bytearray()
    for op in ops:
        if isinstance(op, str):
            op = (op,)
        word, args = op[0], op[1:]
        out.append(OPS[word])
        if word in ("block", "loop", "if"):
            out.append(0x40)  # no result
        elif word in ("i32.const", "i64.const"):
            out += sleb(args[0])
        elif word in ("i32.load", "i32.load8_u", "i32.store", "i32.store8"):
            align = 2 if word in ("i32.load", "i32.store") else 0
            out += uleb(align) + uleb(args[0] if args else 0)
        else:
            for arg in args:
                out += uleb(arg)
    return bytes(out)


def section(sid: int, body: bytes) -> bytes:
    return bytes([sid]) + uleb(len(body)) + body


def functype(params: Sequence[int], results: Sequence[int]) -> bytes:
    return b"\x60" + vec([bytes([p]) for p in params]) + vec([bytes([r]) for r in results])


def body(locals_: List[Tuple[int, int]], instructions: bytes) -> bytes:
    inner = vec([uleb(n) + bytes([t]) for n, t in locals_]) + instructions + b"\x0b"
    return uleb(len(inner)) + inner


def module(types: List[bytes], imports: List[bytes], funcs: List[int], memory: bool,
           globals_: List[bytes], exports: List[bytes], bodies: List[bytes],
           data: List[bytes]) -> bytes:
    out = b"\x00asm\x01\x00\x00\x00"
    out += section(1, vec(types))
    if imports:
        out += section(2, vec(imports))
    out += section(3, vec([uleb(f) for f in funcs]))
    if memory:
        out += section(5, vec([b"\x00" + uleb(1)]))  # one 64 KiB page
    if globals_:
        out += section(6, vec(globals_))
    out += section(7, vec(exports))
    out += section(10, vec(bodies))
    if data:
        out += section(11, vec(data))
    return out


# ---------------------------------------------------------------- primes
def primes() -> bytes:
    # func 0: is_prime(n) -> i32 ; func 1: count_primes(lo, hi) -> i32
    is_prime = code(
        ("local.get", 0), ("i32.const", 2), "i32.lt_u", ("if",), ("i32.const", 0), "return", "end",
        ("local.get", 0), ("i32.const", 4), "i32.lt_u", ("if",), ("i32.const", 1), "return", "end",
        ("local.get", 0), ("i32.const", 2), "i32.rem_u", "i32.eqz",
        ("if",), ("i32.const", 0), "return", "end",
        ("i32.const", 3), ("local.set", 1),
        ("loop",),
        ("local.get", 1), ("local.get", 1), "i32.mul", ("local.get", 0), "i32.gt_u",
        ("if",), ("i32.const", 1), "return", "end",
        ("local.get", 0), ("local.get", 1), "i32.rem_u", "i32.eqz",
        ("if",), ("i32.const", 0), "return", "end",
        ("local.get", 1), ("i32.const", 2), "i32.add", ("local.set", 1),
        ("br", 0),
        "end",
        ("i32.const", 1),
    )
    count = code(
        ("local.get", 0), ("local.set", 2),
        ("block",), ("loop",),
        ("local.get", 2), ("local.get", 1), "i32.ge_u", ("br_if", 1),
        ("local.get", 3), ("local.get", 2), ("call", 0), "i32.add", ("local.set", 3),
        ("local.get", 2), ("i32.const", 1), "i32.add", ("local.set", 2),
        ("br", 0),
        "end", "end",
        ("local.get", 3),
    )
    return module(
        types=[functype([I32], [I32]), functype([I32, I32], [I32])],
        imports=[], funcs=[0, 1], memory=False, globals_=[],
        exports=[name("count_primes") + b"\x00" + uleb(1)],
        bodies=[body([(1, I32)], is_prime), body([(2, I32)], count)],
        data=[],
    )


# ------------------------------------------------------------- wordcount
OUT = 512        # output buffer
TMP_END = 500    # digits are written backwards ending here
BUF = 1024       # read buffer
BUF_LEN = 32768
STRINGS = 256    # constant text


def helpers(first: int) -> Tuple[bytes, bytes]:
    """put_str(src, len) and put_num(n): append text / a decimal to the output
    buffer (global 0).  ``first`` is put_str's function index."""
    put_str = code(  # locals: 0 src, 1 len, 2 i
        ("block",), ("loop",),
        ("local.get", 2), ("local.get", 1), "i32.ge_u", ("br_if", 1),
        ("global.get", 0), ("local.get", 0), ("local.get", 2), "i32.add", ("i32.load8_u",),
        ("i32.store8",),
        ("global.get", 0), ("i32.const", 1), "i32.add", ("global.set", 0),
        ("local.get", 2), ("i32.const", 1), "i32.add", ("local.set", 2),
        ("br", 0),
        "end", "end",
    )
    put_num = code(  # locals: 0 n, 1 t
        ("i32.const", TMP_END), ("local.set", 1),
        ("local.get", 0), "i32.eqz",
        ("if",),
        ("global.get", 0), ("i32.const", 48), ("i32.store8",),
        ("global.get", 0), ("i32.const", 1), "i32.add", ("global.set", 0),
        "return", "end",
        ("block",), ("loop",),
        ("local.get", 0), "i32.eqz", ("br_if", 1),
        ("local.get", 1), ("i32.const", 1), "i32.sub", ("local.set", 1),
        ("local.get", 1), ("local.get", 0), ("i32.const", 10), "i32.rem_u", ("i32.const", 48), "i32.add",
        ("i32.store8",),
        ("local.get", 0), ("i32.const", 10), "i32.div_u", ("local.set", 0),
        ("br", 0),
        "end", "end",
        ("local.get", 1), ("i32.const", TMP_END), ("local.get", 1), "i32.sub", ("call", first),
    )
    return put_str, put_num


def texts_at(texts: Sequence[bytes]) -> Tuple[List[int], int]:
    offsets, pos = [], STRINGS
    for text in texts:
        offsets.append(pos)
        pos += len(text)
    return offsets, pos


def flush_stdout(fd_write: int) -> bytes:
    """Write the output buffer to stdout (iovec at 16, count at 24)."""
    return code(
        ("i32.const", 16), ("i32.const", OUT), ("i32.store",),
        ("i32.const", 20), ("global.get", 0), ("i32.const", OUT), "i32.sub", ("i32.store",),
        ("i32.const", 1), ("i32.const", 16), ("i32.const", 1), ("i32.const", 24), ("call", fd_write),
        "drop",
    )


def wordcount() -> bytes:
    texts = [b'{"bytes":', b',"lines":', b',"words":', b"}\n"]
    offsets, pos = texts_at(texts)
    # imports: 0 fd_read, 1 fd_write ; own funcs: 2 put_str(src,len), 3 put_num(n), 4 _start
    put_str, put_num = helpers(2)
    # _start locals: 0 n, 1 i, 2 bytes, 3 lines, 4 words, 5 inword, 6 c, 7 ws
    start = code(
        ("block",), ("loop",),
        ("i32.const", 0), ("i32.const", BUF), ("i32.store",),
        ("i32.const", 4), ("i32.const", BUF_LEN), ("i32.store",),
        ("i32.const", 0), ("i32.const", 0), ("i32.const", 1), ("i32.const", 8), ("call", 0),
        ("br_if", 1),  # read error: stop
        ("i32.const", 8), ("i32.load",), ("local.tee", 0), "i32.eqz", ("br_if", 1),  # end of input
        ("local.get", 2), ("local.get", 0), "i32.add", ("local.set", 2),
        ("i32.const", 0), ("local.set", 1),
        ("block",), ("loop",),
        ("local.get", 1), ("local.get", 0), "i32.ge_u", ("br_if", 1),
        ("local.get", 1), ("i32.load8_u", BUF), ("local.set", 6),
        ("local.get", 6), ("i32.const", 10), "i32.eq",
        ("if",), ("local.get", 3), ("i32.const", 1), "i32.add", ("local.set", 3), "end",
        ("local.get", 6), ("i32.const", 32), "i32.eq",
        ("local.get", 6), ("i32.const", 10), "i32.eq", "i32.or",
        ("local.get", 6), ("i32.const", 9), "i32.eq", "i32.or",
        ("local.get", 6), ("i32.const", 13), "i32.eq", "i32.or",
        ("local.set", 7),
        ("local.get", 7),
        ("if",), ("i32.const", 0), ("local.set", 5),
        "else",
        ("local.get", 5), "i32.eqz",
        ("if",), ("local.get", 4), ("i32.const", 1), "i32.add", ("local.set", 4), "end",
        ("i32.const", 1), ("local.set", 5),
        "end",
        ("local.get", 1), ("i32.const", 1), "i32.add", ("local.set", 1),
        ("br", 0),
        "end", "end",
        ("br", 0),
        "end", "end",
        ("i32.const", offsets[0]), ("i32.const", len(texts[0])), ("call", 2),
        ("local.get", 2), ("call", 3),
        ("i32.const", offsets[1]), ("i32.const", len(texts[1])), ("call", 2),
        ("local.get", 3), ("call", 3),
        ("i32.const", offsets[2]), ("i32.const", len(texts[2])), ("call", 2),
        ("local.get", 4), ("call", 3),
        ("i32.const", offsets[3]), ("i32.const", len(texts[3])), ("call", 2),
    ) + flush_stdout(1)
    wasi = "wasi_snapshot_preview1"
    rw = 0  # type index of (i32 i32 i32 i32) -> i32
    return module(
        types=[functype([I32] * 4, [I32]), functype([I32, I32], []), functype([I32], []),
               functype([], [])],
        imports=[name(wasi) + name("fd_read") + b"\x00" + uleb(rw),
                 name(wasi) + name("fd_write") + b"\x00" + uleb(rw)],
        funcs=[1, 2, 3], memory=True,
        globals_=[bytes([I32, 1]) + code(("i32.const", OUT)) + b"\x0b"],
        exports=[name("memory") + b"\x02" + uleb(0), name("_start") + b"\x00" + uleb(4)],
        bodies=[body([(1, I32)], put_str), body([(1, I32)], put_num), body([(8, I32)], start)],
        data=[b"\x00" + code(("i32.const", STRINGS)) + b"\x0b" + uleb(pos - STRINGS) + b"".join(texts)],
    )


# ----------------------------------------------------------------- upper
FILE_NAME = 32   # "upper.txt" in memory


def upper() -> bytes:
    """Reads stdin, writes it in capitals to /out/upper.txt, prints
    {"bytes":N}.  Uses the second preopened folder (fd 4 = /out)."""
    texts = [b'{"bytes":', b"}\n"]
    offsets, pos = texts_at(texts)
    # imports: 0 fd_read, 1 fd_write, 2 path_open ; own: 3 put_str, 4 put_num, 5 _start
    put_str, put_num = helpers(3)
    # _start locals: 0 n, 1 i, 2 total, 3 fd, 4 c
    start = code(
        ("i32.const", 4), ("i32.const", 0), ("i32.const", FILE_NAME), ("i32.const", 9),
        ("i32.const", 9),  # O_CREAT | O_TRUNC
        ("i64.const", 0x66), ("i64.const", 0),  # rights: read, seek, tell, write
        ("i32.const", 0), ("i32.const", 28), ("call", 2),
        ("if",), "unreachable", "end",
        ("i32.const", 28), ("i32.load",), ("local.set", 3),
        ("block",), ("loop",),
        ("i32.const", 0), ("i32.const", BUF), ("i32.store",),
        ("i32.const", 4), ("i32.const", BUF_LEN), ("i32.store",),
        ("i32.const", 0), ("i32.const", 0), ("i32.const", 1), ("i32.const", 8), ("call", 0),
        ("br_if", 1),
        ("i32.const", 8), ("i32.load",), ("local.tee", 0), "i32.eqz", ("br_if", 1),
        ("local.get", 2), ("local.get", 0), "i32.add", ("local.set", 2),
        ("i32.const", 0), ("local.set", 1),
        ("block",), ("loop",),
        ("local.get", 1), ("local.get", 0), "i32.ge_u", ("br_if", 1),
        ("local.get", 1), ("i32.load8_u", BUF), ("local.set", 4),
        ("local.get", 4), ("i32.const", 97), "i32.sub", ("i32.const", 25), "i32.le_u",
        ("if",),
        ("local.get", 1), ("local.get", 4), ("i32.const", 32), "i32.sub", ("i32.store8", BUF),
        "end",
        ("local.get", 1), ("i32.const", 1), "i32.add", ("local.set", 1),
        ("br", 0),
        "end", "end",
        ("i32.const", 16), ("i32.const", BUF), ("i32.store",),
        ("i32.const", 20), ("local.get", 0), ("i32.store",),
        ("local.get", 3), ("i32.const", 16), ("i32.const", 1), ("i32.const", 24), ("call", 1),
        "drop",
        ("br", 0),
        "end", "end",
        ("i32.const", offsets[0]), ("i32.const", len(texts[0])), ("call", 3),
        ("local.get", 2), ("call", 4),
        ("i32.const", offsets[1]), ("i32.const", len(texts[1])), ("call", 3),
    ) + flush_stdout(1)
    wasi = "wasi_snapshot_preview1"
    return module(
        types=[functype([I32] * 4, [I32]), functype([I32, I32], []), functype([I32], []),
               functype([], []), functype([I32] * 5 + [I64, I64, I32, I32], [I32])],
        imports=[name(wasi) + name("fd_read") + b"\x00" + uleb(0),
                 name(wasi) + name("fd_write") + b"\x00" + uleb(0),
                 name(wasi) + name("path_open") + b"\x00" + uleb(4)],
        funcs=[1, 2, 3], memory=True,
        globals_=[bytes([I32, 1]) + code(("i32.const", OUT)) + b"\x0b"],
        exports=[name("memory") + b"\x02" + uleb(0), name("_start") + b"\x00" + uleb(5)],
        bodies=[body([(1, I32)], put_str), body([(1, I32)], put_num), body([(5, I32)], start)],
        data=[b"\x00" + code(("i32.const", FILE_NAME)) + b"\x0b" + name("upper.txt"),
              b"\x00" + code(("i32.const", STRINGS)) + b"\x0b" + uleb(pos - STRINGS) + b"".join(texts)],
    )


def main(argv: Sequence[str]) -> int:
    here = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent
    for filename, build in (("primes.wasm", primes), ("wordcount.wasm", wordcount),
                            ("upper.wasm", upper)):
        data = build()
        (here / filename).write_bytes(data)
        print("wrote %s (%d bytes)" % (here / filename, len(data)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
