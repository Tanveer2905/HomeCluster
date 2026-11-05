"""WebAssembly versions of the built-in number crunching (SPEC §7, §6.9):
the hand-assembled modules, and how prime_range / monte_carlo_pi offer them."""

from __future__ import annotations

import hashlib
import math
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from conftest import ROOT
from coordinator.plugins import _wasm_kernels as kernels
from coordinator.plugins import monte_carlo_pi, prime_range
from coordinator.plugins.wasm import inspect_module

WASMTIME = ROOT / "tools" / "wasmtime" / ("wasmtime.exe" if sys.platform == "win32" else "wasmtime")
needs_wasmtime = pytest.mark.skipif(not WASMTIME.is_file(), reason="needs tools/wasmtime")


def reference_count(lo: int, hi: int) -> int:
    """Primes in [lo, hi), independently of both workers' code."""
    lo = max(lo, 2)
    if hi <= lo:
        return 0
    root = math.isqrt(hi) + 1
    small = bytearray([1]) * (root + 1)
    small[0:2] = b"\0\0"
    for p in range(2, math.isqrt(root) + 1):
        if small[p]:
            small[p * p::p] = bytearray(len(small[p * p::p]))
    block = bytearray([1]) * (hi - lo)
    for p in (q for q in range(2, root + 1) if small[q]):
        first = max(p * p, (lo + p - 1) // p * p)
        if first < hi:
            block[first - lo::p] = bytearray(len(block[first - lo::p]))
    return sum(block)


def invoke(module: bytes, name: str, args: List[int], tmp_path: Path) -> int:
    path = tmp_path / ("%s.wasm" % name)
    path.write_bytes(module)
    proc = subprocess.run([str(WASMTIME), "run", "--invoke", name, str(path)] + [str(a) for a in args],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return int(proc.stdout.strip())


def test_the_modules_export_one_function_each() -> None:
    for name, module in kernels.MODULES.items():
        info = inspect_module(module)
        assert info["imports"] == [] and not info["wasi"]
        assert info["exports"][name] == {"kind": "func", "params": ["i64", "i64"], "results": ["i64"]}


@needs_wasmtime
@pytest.mark.parametrize("lo,hi", [(2, 3), (2, 1000), (2, 1000000), (5, 5), (10, 11),
                                   (262140, 262160),                # across the 256 KiB blocks
                                   (100000000, 100700000), (999999000000, 1000000000000)])
def test_count_primes_matches_a_reference(lo: int, hi: int, tmp_path: Path) -> None:
    assert invoke(kernels.MODULES["count_primes"], "count_primes", [lo, hi], tmp_path) == reference_count(lo, hi)


@needs_wasmtime
def test_count_inside_estimates_pi_and_repeats_exactly(tmp_path: Path) -> None:
    inside = invoke(kernels.MODULES["count_inside"], "count_inside", [2000000, 1000003], tmp_path)
    assert abs(4.0 * inside / 2000000 - math.pi) < 0.01
    assert invoke(kernels.MODULES["count_inside"], "count_inside", [2000000, 1000003], tmp_path) == inside
    assert invoke(kernels.MODULES["count_inside"], "count_inside", [0, 7], tmp_path) == 0


class Ctx:
    """The bits of PluginContext the kernels use."""

    def __init__(self) -> None:
        self.files: Dict[str, Dict[str, Any]] = {}

    def file_meta(self, file_id: str) -> Optional[Dict[str, Any]]:
        return self.files.get(file_id)

    def add_file(self, data: bytes, name: str = "output.bin") -> Dict[str, Any]:
        file_id = hashlib.sha256(data).hexdigest()
        self.files[file_id] = {"id": file_id, "name": name, "size": len(data)}
        return self.files[file_id]


def test_both_jobs_offer_webassembly_next_to_python_and_javascript() -> None:
    ctx = Ctx()
    payloads = prime_range.split({"start": 2, "end": 250000}, {"chunk_size": 100000}, ctx)
    module_id = hashlib.sha256(kernels.MODULES["count_primes"]).hexdigest()
    assert ctx.files[module_id]["name"] == "builtin-count_primes.wasm"
    assert [sorted(p["impls"]) for p in payloads] == [["js", "python", "wasm"]] * 3
    assert payloads[0]["inputs"] == [module_id]
    assert payloads[2]["impls"]["wasm"]["args"] == [200002, 250000]
    assert payloads[0]["impls"]["wasm"]["params"] == ["i64", "i64"]
    # a list of the primes themselves stays with Python and JavaScript
    listing = prime_range.split({"start": 2, "end": 100, "return_list": True}, {}, ctx)
    assert sorted(listing[0]["impls"]) == ["js", "python"] and listing[0]["inputs"] == []
    # automatic sizing passes the module along in its state
    plan = prime_range.work({"start": 2, "end": 10 ** 7}, {}, ctx)
    piece = prime_range.piece({"start": 2, "end": 10 ** 7}, {}, ctx, plan["state"], 0, 5000)
    assert piece["impls"]["wasm"]["args"] == [2, 5002]
    pi = monte_carlo_pi.piece({"samples": 10 ** 6}, {}, ctx, monte_carlo_pi.work({"samples": 10 ** 6}, {}, ctx)["state"],
                              0, 1000)
    assert pi["impls"]["wasm"]["call"] == "count_inside" and pi["impls"]["wasm"]["args"] == [1000, 1000003]
    # without a coordinator to hold the module (ctx None) there is no WebAssembly version
    assert sorted(prime_range.split({"start": 2, "end": 10}, {}, None)[0]["impls"]) == ["js", "python"]


def test_results_from_every_version_add_up() -> None:
    assert prime_range.reduce({}, [{"result": 25}, {"result": {"count": 21}}, {"result": None}], None) == {"count": 46}
    total = monte_carlo_pi.reduce({}, [{"result": 785, "meta": {"samples": 1000}},
                                       {"result": {"inside": 790, "samples": 1000}}], None)
    assert total == {"pi": 4.0 * 1575 / 2000, "inside": 1575, "samples": 2000}
