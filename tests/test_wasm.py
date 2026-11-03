"""WebAssembly tasks (SPEC 6.9): the plugin, the worker's output parsing, and
the browser runner (Node) giving the same answers as wasmtime."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

from conftest import ROOT
from coordinator.plugins import wasm as plugin

EXAMPLES = ROOT / "examples" / "wasm"
WASMTIME = ROOT / "tools" / "wasmtime" / ("wasmtime.exe" if sys.platform == "win32" else "wasmtime")
NODE = shutil.which("node")


class Ctx:
    """The bits of PluginContext the wasm plugin uses."""

    def __init__(self, files: Dict[str, bytes]) -> None:
        self.files = files

    def file_meta(self, file_id: str) -> Any:
        return {"name": file_id + ".bin", "size": len(self.files[file_id])} if file_id in self.files else None

    def read_file(self, file_id: str) -> bytes:
        return self.files[file_id]


def ctx() -> Ctx:
    return Ctx({name: (EXAMPLES / ("%s.wasm" % name)).read_bytes() for name in ("primes", "wordcount", "upper")}
               | {"text1": b"a b c\n", "text2": b"hello\n"})


# --------------------------------------------------------------------------
# reading modules
# --------------------------------------------------------------------------

def test_the_example_modules_are_what_the_builder_makes(tmp_path: Path) -> None:
    proc = subprocess.run([sys.executable, str(EXAMPLES / "build_examples.py"), str(tmp_path)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    for name in ("primes.wasm", "wordcount.wasm", "upper.wasm"):
        assert (tmp_path / name).read_bytes() == (EXAMPLES / name).read_bytes(), name


def test_exports_and_imports_are_read() -> None:
    primes = plugin.inspect_module((EXAMPLES / "primes.wasm").read_bytes())
    assert primes["exports"]["count_primes"] == {"kind": "func", "params": ["i32", "i32"], "results": ["i32"]}
    assert primes["program"] is False and primes["wasi"] is False
    words = plugin.inspect_module((EXAMPLES / "wordcount.wasm").read_bytes())
    assert words["program"] is True and words["wasi"] is True
    assert "wasi_snapshot_preview1.fd_read" in words["imports"]
    with pytest.raises(ValueError, match="WebAssembly text"):
        plugin.inspect_module(b"(module)")
    with pytest.raises(ValueError, match="not a WebAssembly module"):
        plugin.inspect_module(b"hello")


# --------------------------------------------------------------------------
# the plugin
# --------------------------------------------------------------------------

def test_a_range_is_cut_into_calls() -> None:
    params = {"module": "primes", "call": "count_primes", "range": [2, 1000], "grain": 10}
    info = plugin.work(params, {}, ctx())
    assert info["units"] == 998 and info["grain"] == 10
    payload = plugin.piece(params, {}, ctx(), info["state"], 100, 200)
    impl = payload["impls"]["wasm"]
    assert payload["inputs"] == ["primes"] and impl["args"] == [102, 202]
    assert impl["params"] == ["i32", "i32"] and impl["results"] == ["i32"]
    assert payload["meta"]["units"] == 100
    assert plugin.speed_key(params).startswith("wasm:")


def test_calls_and_programs_are_checked_before_anything_runs() -> None:
    with pytest.raises(ValueError, match="exports no function 'nope'.*count_primes"):
        plugin.split({"module": "primes", "call": "nope", "args_list": [[1, 2]]}, {}, ctx())
    with pytest.raises(ValueError, match="takes 2 number"):
        plugin.split({"module": "primes", "call": "count_primes", "args_list": [[1]]}, {}, ctx())
    with pytest.raises(ValueError, match="no _start"):
        plugin.split({"module": "primes"}, {}, ctx())
    with pytest.raises(ValueError, match="needs 'module'"):
        plugin.split({}, {}, ctx())
    with pytest.raises(ValueError, match="takes numbers"):
        plugin.split({"module": "primes", "call": "count_primes", "args_list": [["a", "b"]]}, {}, ctx())


def test_program_mode_makes_one_task_per_input_or_argument_list() -> None:
    per_file = plugin.split({"module": "wordcount", "inputs": ["text1", "text2"], "env": {"A": 1}}, {}, ctx())
    assert len(per_file) == 2
    impl = per_file[1]["impls"]["wasm"]
    assert per_file[1]["inputs"] == ["wordcount", "text2"]
    assert impl["stdin_input"] == 1 and impl["files"] == [1] and impl["names"] == {"1": "text2.bin"}
    assert impl["env"] == {"A": "1"} and impl["stdin"] is None
    runs = plugin.split({"module": "wordcount", "args_list": [["-v"], ["x", 2]], "stdin": "hi"}, {}, ctx())
    assert [r["impls"]["wasm"]["args"] for r in runs] == [["-v"], ["x", "2"]]
    assert runs[0]["impls"]["wasm"]["stdin"] == "hi"


def test_results_combine() -> None:
    results = [{"result": 3, "meta": {}}, {"result": 4, "meta": {}}]
    assert plugin.reduce({"range": [0, 10], "call": "f"}, results, None) == {"sum": 7, "tasks": 2}
    assert plugin.reduce({"call": "f"}, results, None) == [3, 4]
    assert plugin.reduce({"combine": "text"}, [{"result": "a"}, {"result": "b\n"}], None) == "ab\n"
    per_input = plugin.reduce({"inputs": ["x"]}, [{"result": {"n": 1}, "meta": {"name": "x.txt"},
                                                   "outputs": [{"file_id": "f"}]}], None)
    assert per_input == [{"input": "x.txt", "result": {"n": 1}, "outputs": [{"file_id": "f"}]}]


# --------------------------------------------------------------------------
# what devices print
# --------------------------------------------------------------------------

def load_worker() -> Any:
    name = "pc_worker_wasm"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "worker" / "worker.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    return sys.modules[name]


def test_worker_parses_wasmtime_output_like_the_browser() -> None:
    worker = load_worker()
    assert worker.parse_invoke_output("9592\n") == 9592
    assert worker.parse_invoke_output("1\n2.5\n") == [1, 2.5]
    assert worker.parse_invoke_output("progress...\n42\n") == 42
    assert worker.parse_invoke_output("") is None
    assert worker.parse_program_output('{"a": 1}\n') == {"a": 1}
    assert worker.parse_program_output("42\n") == 42
    assert worker.parse_program_output("plain text\n") == "plain text\n"
    assert worker.parse_program_output("Infinity") == "Infinity"


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_the_browser_runner_gives_the_same_answers() -> None:
    proc = subprocess.run([NODE, str(ROOT / "tests" / "wasm_check.js")], capture_output=True, text=True,
                          timeout=120, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report["primes"]["result"] == 9592
    assert report["wordcount"]["result"] == {"bytes": 19, "lines": 2, "words": 4}
    assert report["upper"]["result"] == {"bytes": 29}
    assert report["upper"]["outputs"] == [{"name": "upper.txt", "text": "HELLO WASM WORLD\nSECOND LINE\n"}]
    assert "exports no function nope" in report["missing"]["error"]["error"]
    assert report["f32"] == [3.14, 0.1, 1]
    worker = load_worker()
    texts = ['{"a": 1}\n', "42\n", "plain text\n", "Infinity"]
    assert report["parse"] == [worker.parse_program_output(t) for t in texts]


@pytest.mark.skipif(not WASMTIME.is_file(), reason="needs tools/wasmtime (scripts/get-wasmtime.ps1)")
def test_wasmtime_agrees_with_the_browser_runner() -> None:
    worker = load_worker()
    run = subprocess.run([str(WASMTIME), "run", "--invoke", "count_primes", str(EXAMPLES / "primes.wasm"),
                          "2", "100000"], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    assert worker.parse_invoke_output(run.stdout) == 9592
    words = subprocess.run([str(WASMTIME), "run", str(EXAMPLES / "wordcount.wasm")],
                           input=b"one two three\nfour\n", capture_output=True, timeout=60)
    assert worker.parse_program_output(words.stdout.decode()) == {"bytes": 19, "lines": 2, "words": 4}
