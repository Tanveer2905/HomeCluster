"""Browser Python runtime (worker/browser/pyrunner.js on Pyodide), driven from Node.

Skipped when ``tools/pyodide`` (scripts/get-pyodide.ps1) or ``node`` is missing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYODIDE = ROOT / "tools" / "pyodide" / "pyodide.asm.wasm"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    not PYODIDE.is_file() or NODE is None,
    reason="needs tools/pyodide (scripts/get-pyodide.ps1) and node",
)


@pytest.fixture(scope="module")
def report() -> dict:
    from coordinator.plugins.python_map import WORKER_PY

    env = dict(os.environ, PC_WORKER_PY=WORKER_PY)
    proc = subprocess.run(
        [NODE, str(ROOT / "tests" / "pyrunner_check.js")],
        capture_output=True, text=True, timeout=300, env=env, cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "fatal" not in data, data.get("fatal")
    return data


def test_pyodide_loads_and_runs_a_function(report: dict) -> None:
    assert report["version"].startswith("0.")
    assert report["checks"]["basic"]["result"] == {"sum": 6, "name": "node", "n_inputs": 0}


def test_inputs_outputs_logs_and_progress(report: dict) -> None:
    files = report["checks"]["files"]
    assert files["result"]["result"] == 5
    assert files["output"] == [{"name": "shout.txt", "text": "HELLO"}]
    assert "printed line" in files["logs"]  # print() is captured as a log line
    assert files["progress"] == [0.5]


def test_errors_carry_a_traceback(report: dict) -> None:
    err = report["checks"]["error"]["payload"]
    assert err["error"] == "nope"
    assert "ValueError: nope" in err["traceback"]


def test_checkpoint_and_resume(report: dict) -> None:
    ck = report["checks"]["checkpoint"]
    assert ck["result"]["result"] == 41           # ctx.resume() saw the saved state
    assert ck["posted"] == [{"i": 42}]            # ctx.checkpoint() reached JS


def test_python_map_runner_resumes_under_pyodide(report: dict) -> None:
    pm = report["checks"]["python_map"]
    assert pm["result"]["result"] == [1, 4, 9, 16]
    assert any("resuming after 2 of 4" in line for line in pm["logs"])
    assert [l for l in pm["logs"] if l.startswith("item ")] == ["item 3", "item 4"]


def test_tasks_do_not_share_globals(report: dict) -> None:
    assert report["checks"]["isolated"]["result"] is False
