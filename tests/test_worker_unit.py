"""Tests for worker/worker.py against tests/fake_coordinator.py.

Self-contained: no conftest.py, no coordinator, no third-party deps beyond
pytest.  Run in the project folder with:

    & .\\tools\\python\\python.exe -m pytest tests\\test_worker_unit.py -q
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WORKER_PY = os.path.join(ROOT, "worker", "worker.py")
BROWSER_DIR = os.path.join(ROOT, "worker", "browser")

if HERE not in sys.path:
    sys.path.insert(0, HERE)

from fake_coordinator import FakeCoordinator  # noqa: E402

IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def wait_for(predicate, timeout=20.0, interval=0.05, what="condition"):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError("timed out waiting for %s" % what)


class WorkerProc(object):
    """A worker.py subprocess with its output captured for diagnostics."""

    def __init__(self, fake, state_dir, pin=None, name="testdev", slots=2,
                 extra=None):
        self.fake = fake
        self.state_dir = str(state_dir)
        self.lines = []
        args = [sys.executable, WORKER_PY, "--url", fake.url,
                "--state-dir", self.state_dir, "--name", name,
                "--slots", str(slots), "--verbose"]
        if pin:
            args += ["--pin", pin]
        if extra:
            args += list(extra)
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        self.proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, env=env, cwd=ROOT)
        self._reader = threading.Thread(target=self._pump)
        self._reader.daemon = True
        self._reader.start()

    def _pump(self):
        try:
            for raw in iter(self.proc.stdout.readline, b""):
                self.lines.append(raw.decode("utf-8", "replace").rstrip())
        except Exception:
            pass

    @property
    def output(self):
        return "\n".join(self.lines)

    def wait_registered(self, timeout=20.0):
        wait_for(lambda: self.fake.device_ids(), timeout=timeout,
                 what="worker registration\n" + self.output)
        # the worker has also stored its identity: a test may stop it now
        wait_for(lambda: os.path.isfile(os.path.join(self.state_dir, "device.json")),
                 timeout=timeout, what="device.json")
        return self.fake.device_ids()[-1]

    def stop(self, timeout=10.0):
        if self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=timeout)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self._reader.join(timeout=2)

    def device_json(self):
        path = os.path.join(self.state_dir, "device.json")
        with open(path, "r") as fh:
            return json.load(fh)


def start_cluster(tmp_dir, **kwargs):
    fake = FakeCoordinator(pin="123456", poll_timeout=0.4, log_batch_ms=100).start()
    worker = WorkerProc(fake, tmp_dir, pin="123456", **kwargs)
    return fake, worker


def push_task(fake, device_id, task_id, payload, job_id="job1", idx=0, attempt=1):
    fake.push({"cmd": "task", "task": {"id": task_id, "job_id": job_id, "idx": idx,
                                       "attempt": attempt, "payload": payload}},
              device_id)
    return task_id


def wait_result(fake, task_id, timeout=60.0):
    return wait_for(lambda: fake.result(task_id), timeout=timeout,
                    what="result of task " + task_id)


# ---------------------------------------------------------------------------
# module-scoped worker used by most of the protocol tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    state = tmp_path_factory.mktemp("state")
    fake, worker = start_cluster(state)
    try:
        device_id = worker.wait_registered()
        yield fake, worker, device_id
    finally:
        worker.stop()
        fake.stop()


# ---------------------------------------------------------------------------
# registration / protocol basics
# ---------------------------------------------------------------------------

def test_registration_payload_and_device_json(cluster):
    fake, worker, device_id = cluster
    reg = fake.registrations[0]
    assert reg["pin"] == "123456"
    assert reg["name"] == "testdev"
    assert reg["slots"] == 2
    assert reg["platform"] in ("windows", "linux", "termux")
    assert "python" in reg["caps"] and "shell" in reg["caps"]
    assert reg["platform"] in reg["caps"]
    info = reg["info"]
    for key in ("cpu_count", "mem_mb", "os", "python", "model", "shell"):
        assert key in info
    assert info["cpu_count"] >= 1
    assert info["mem_mb"] is None or info["mem_mb"] > 0

    saved = worker.device_json()
    assert saved["device_id"] == device_id
    assert saved["token"]
    assert saved["name"] == "testdev"
    assert saved["url"].startswith("http://127.0.0.1:")


def test_poll_status_block(cluster):
    fake, _worker, device_id = cluster
    status = wait_for(lambda: fake.last_status.get(device_id), what="a poll status")
    for key in ("battery", "charging", "temp", "free_slots", "running",
                "storage_free_mb"):
        assert key in status
    assert status["free_slots"] == 2
    assert status["running"] == []
    assert status["storage_free_mb"] is None or status["storage_free_mb"] > 0


def test_python_task_with_inputs_logs_and_output(cluster):
    fake, _worker, device_id = cluster
    file_id = fake.add_file("data.txt", b"hello world")
    code = (
        "import os\n"
        "def run(args, ctx):\n"
        "    ctx.log('starting ' + ctx.task_id)\n"
        "    ctx.progress(0.5)\n"
        "    body = open(ctx.input(args['fid']), 'rb').read().decode()\n"
        "    assert ctx.inputs[0] == ctx.input(args['fid'])\n"
        "    p = os.path.join(ctx.workdir, 'result.txt')\n"
        "    fh = open(p, 'w')\n"
        "    fh.write(body.upper())\n"
        "    fh.close()\n"
        "    ctx.output(p, 'result.txt')\n"
        "    ctx.log('device=' + str(ctx.device_name))\n"
        "    return {'n': args['n'] * 2, 'body': body}\n"
    )
    payload = {"impls": {"python": {"code": code, "entry": "run",
                                    "args": {"n": 21, "fid": file_id}}},
               "inputs": [file_id], "timeout": 60}
    push_task(fake, device_id, "t-python-ok", payload)
    result = wait_result(fake, "t-python-ok")

    assert result["ok"] is True, result
    assert result["result"] == {"n": 42, "body": "hello world"}
    assert result["duration"] >= 0
    assert result["log_lines"] >= 2
    assert "t-python-ok" in fake.started

    text = fake.log_text("t-python-ok")
    assert "starting t-python-ok" in text
    assert "__PC_PROGRESS__ 0.5" in text
    assert "device=testdev" in text

    outputs = result["outputs"]
    assert len(outputs) == 1
    assert outputs[0]["name"] == "result.txt"
    assert fake.file_bytes(outputs[0]["file_id"]) == b"HELLO WORLD"

    # the input was cached under <state>/cache/<file_id>
    cached = os.path.join(_worker.state_dir, "cache", file_id)
    assert os.path.isfile(cached)


def test_python_task_failure_reports_traceback(cluster):
    fake, _worker, device_id = cluster
    code = ("def run(args, ctx):\n"
            "    ctx.log('about to explode')\n"
            "    raise ValueError('boom ' + args['tag'])\n")
    payload = {"impls": {"python": {"code": code, "entry": "run",
                                    "args": {"tag": "xyz"}}},
               "timeout": 60}
    push_task(fake, device_id, "t-python-err", payload)
    result = wait_result(fake, "t-python-err")

    assert result["ok"] is False
    assert "boom xyz" in result["error"]
    assert "ValueError" in result["traceback"]
    assert "about to explode" in fake.log_text("t-python-err")


def test_python_missing_entry_point(cluster):
    fake, _worker, device_id = cluster
    payload = {"impls": {"python": {"code": "x = 1\n", "entry": "nope"}},
               "timeout": 30}
    push_task(fake, device_id, "t-python-entry", payload)
    result = wait_result(fake, "t-python-entry")
    assert result["ok"] is False
    assert "nope" in result["error"]


def test_shell_task_uploads_output_dir(cluster):
    fake, _worker, device_id = cluster
    shell = {
        "cmd": 'echo shell-ran; printf hi > "$PC_OUTPUT_DIR/o.txt"',
        "cmd_windows": 'echo shell-ran& echo hi>"%PC_OUTPUT_DIR%\\o.txt"',
        "env": {"PC_TEST_VAR": "abc"},
    }
    payload = {"impls": {"shell": shell}, "timeout": 60}
    push_task(fake, device_id, "t-shell", payload)
    result = wait_result(fake, "t-shell")

    assert result["ok"] is True, result
    assert result["result"]["exit_code"] == 0
    assert "shell-ran" in result["result"]["stdout"]
    assert "stderr" in result["result"]
    outputs = result["outputs"]
    assert len(outputs) == 1 and outputs[0]["name"] == "o.txt"
    assert fake.file_bytes(outputs[0]["file_id"]).strip() == b"hi"


def test_shell_task_nonzero_exit(cluster):
    fake, _worker, device_id = cluster
    shell = {"cmd": "exit 3", "cmd_windows": "exit /b 3"}
    push_task(fake, device_id, "t-shell-fail", {"impls": {"shell": shell},
                                                "timeout": 30})
    result = wait_result(fake, "t-shell-fail")
    assert result["ok"] is False
    assert result["result"]["exit_code"] == 3


def test_shell_env_and_inputs(cluster):
    fake, _worker, device_id = cluster
    file_id = fake.add_file("in.txt", b"payload-bytes")
    shell = {
        "cmd": 'echo "$PC_TASK_ID"; cat "$PC_INPUT_0"',
        "cmd_windows": 'echo %PC_TASK_ID%& type "%PC_INPUT_0%"',
    }
    push_task(fake, device_id, "t-shell-env",
              {"impls": {"shell": shell}, "inputs": [file_id], "timeout": 30})
    result = wait_result(fake, "t-shell-env")
    assert result["ok"] is True, result
    assert "t-shell-env" in result["result"]["stdout"]
    assert "payload-bytes" in result["result"]["stdout"]


def test_impl_preference_python_over_shell(cluster):
    fake, _worker, device_id = cluster
    payload = {"impls": {
        "shell": {"cmd": "echo should-not-run", "cmd_windows": "echo should-not-run"},
        "python": {"code": "def run(a, c):\n    return 'python-won'\n"},
        "js": {"code": "function run(){return 1}"},
    }, "timeout": 30}
    push_task(fake, device_id, "t-order", payload)
    result = wait_result(fake, "t-order")
    assert result["ok"] is True
    assert result["result"] == "python-won"


def test_task_timeout_kills_process_tree(cluster):
    fake, _worker, device_id = cluster
    code = "import time\ndef run(args, ctx):\n    time.sleep(60)\n    return 1\n"
    push_task(fake, device_id, "t-timeout",
              {"impls": {"python": {"code": code}}, "timeout": 2})
    started = time.time()
    result = wait_result(fake, "t-timeout", timeout=45)
    assert result["ok"] is False
    assert "timeout" in result["error"].lower()
    assert time.time() - started < 30


def test_cancel_kills_running_task(cluster):
    fake, _worker, device_id = cluster
    code = "import time\ndef run(args, ctx):\n    ctx.log('running')\n    time.sleep(60)\n"
    push_task(fake, device_id, "t-cancel",
              {"impls": {"python": {"code": code}}, "timeout": 300})
    wait_for(lambda: "t-cancel" in fake.started, what="task start")
    wait_for(lambda: "running" in fake.log_text("t-cancel"), what="task log")
    fake.push({"cmd": "cancel", "task_id": "t-cancel"}, device_id)
    result = wait_result(fake, "t-cancel", timeout=45)
    assert result["ok"] is False
    assert "cancel" in result["error"].lower()


def test_store_send_and_delete_chunk(cluster):
    fake, worker, device_id = cluster
    data = os.urandom(4096)
    chunk_id = fake.add_chunk(data)
    fake.push({"cmd": "store_chunk", "chunk_id": chunk_id, "size": len(data)},
              device_id)
    ack = wait_for(lambda: (fake.chunk_acks or [None])[-1], what="chunk_ack")
    assert ack["chunk_id"] == chunk_id
    assert ack["ok"] is True, ack
    stored = os.path.join(worker.state_dir, "store", chunk_id)
    assert os.path.isfile(stored)
    with open(stored, "rb") as fh:
        assert fh.read() == data

    fake.push({"cmd": "send_chunk", "chunk_id": chunk_id, "req_id": "req-1"},
              device_id)
    put = wait_for(lambda: (fake.chunk_puts or [None])[-1], what="chunk PUT")
    assert put["chunk_id"] == chunk_id
    assert put["req_id"] == "req-1"
    assert put["data"] == data

    fake.push({"cmd": "del_chunk", "chunk_id": chunk_id}, device_id)
    wait_for(lambda: not os.path.exists(stored), what="chunk deletion")


def test_store_chunk_rejects_bad_hash(cluster):
    fake, _worker, device_id = cluster
    bogus = "f" * 64
    with fake.lock:
        fake.chunks[bogus] = b"not the right bytes"
    before = len(fake.chunk_acks)
    fake.push({"cmd": "store_chunk", "chunk_id": bogus, "size": 19}, device_id)
    wait_for(lambda: len(fake.chunk_acks) > before, what="chunk_ack")
    ack = fake.chunk_acks[-1]
    assert ack["ok"] is False
    assert "mismatch" in ack["error"]


def test_terminal_echo(cluster):
    fake, _worker, device_id = cluster
    sid = "term-1"
    fake.push({"cmd": "term_open", "session_id": sid, "shell": None}, device_id)
    time.sleep(1.0)
    fake.push({"cmd": "term_input", "session_id": sid,
               "data": "echo hi\r\n" if IS_WINDOWS else "echo hi\n"}, device_id)
    wait_for(lambda: "hi" in fake.term_output.get(sid, ""), timeout=30,
             what="terminal output")
    fake.push({"cmd": "term_close", "session_id": sid}, device_id)
    wait_for(lambda: sid in fake.term_closed, timeout=30, what="terminal close")


def test_full_terminal(cluster):
    """A shell on a pseudo-terminal (ConPTY on Windows): keystrokes in, the
    terminal's echo and escape codes out, a size the programs see (SPEC 10)."""
    fake, _worker, device_id = cluster
    assert "pty" in fake.registrations[0]["caps"]
    sid = "term-full"
    fake.push({"cmd": "term_open", "session_id": sid, "shell": None,
               "pty": True, "cols": 100, "rows": 30}, device_id)
    time.sleep(1.0)
    # Enter is a carriage return in a real terminal
    fake.push({"cmd": "term_input", "session_id": sid, "data": "echo full-$((6*7))\r"
               if not IS_WINDOWS else "echo full-%COMPUTERNAME:~0,0%42\r"}, device_id)
    wait_for(lambda: "full-42" in fake.term_output.get(sid, ""), timeout=30,
             what="terminal output")
    fake.push({"cmd": "term_resize", "session_id": sid, "cols": 120, "rows": 40}, device_id)
    time.sleep(0.5)
    ask = "mode con\r" if IS_WINDOWS else "stty size\r"
    fake.push({"cmd": "term_input", "session_id": sid, "data": ask}, device_id)
    if IS_WINDOWS:
        wait_for(lambda: re.search(r"Columns:\s+120", fake.term_output.get(sid, "")),
                 timeout=30, what="new width")
    else:
        wait_for(lambda: "40 120" in fake.term_output.get(sid, ""), timeout=30, what="new size")
    fake.push({"cmd": "term_input", "session_id": sid, "data": "exit 3\r"}, device_id)
    wait_for(lambda: sid in fake.term_closed, timeout=30, what="terminal exit")
    assert fake.term_closed[sid] == 3


def test_full_terminal_close_ends_a_running_program(cluster):
    fake, _worker, device_id = cluster
    sid = "term-busy"
    fake.push({"cmd": "term_open", "session_id": sid, "pty": True, "cols": 80, "rows": 24}, device_id)
    time.sleep(1.0)
    busy = "ping -n 60 127.0.0.1\r" if IS_WINDOWS else "sleep 60\r"
    fake.push({"cmd": "term_input", "session_id": sid, "data": busy}, device_id)
    time.sleep(1.0)
    fake.push({"cmd": "term_close", "session_id": sid}, device_id)
    wait_for(lambda: sid in fake.term_closed, timeout=20, what="terminal close")


def test_terminal_size_is_clamped(w):
    assert w.clamp_size(None, None) == (80, 24)
    assert w.clamp_size("132", 50) == (132, 50)
    assert w.clamp_size(1, 1) == (w.TERM_COLS[0], w.TERM_ROWS[0])
    assert w.clamp_size(10 ** 6, 10 ** 6) == (w.TERM_COLS[1], w.TERM_ROWS[1])
    assert w.clamp_size("wide", []) == (80, 24)


def test_terminal_output_keeps_split_characters_whole(w):
    """A character cut in two by a read reaches the page whole, not as two '?'."""
    posts = []

    class Http(object):
        def call(self, method, path, body, timeout=None):
            posts.append(body["data"])

    class Worker(object):
        http = Http()

    session = w.TerminalSession(Worker(), "t1")
    text = "héllo ── \U0001F600"
    raw = text.encode("utf-8")
    for i in range(len(raw)):
        session.buf.extend(raw[i:i + 1])
        session._flush()
    session._flush(final=True)
    assert "".join(posts) == text
    assert all("�" not in p for p in posts)


def test_benchmark_command(cluster):
    fake, _worker, device_id = cluster
    before = len(fake.benches)
    fake.push({"cmd": "benchmark"}, device_id)
    wait_for(lambda: len(fake.benches) > before, timeout=45, what="bench post")
    bench = fake.benches[-1]
    assert bench["kind"] == "python"
    assert bench["score"] > 0
    assert bench["loop_iters"] > 0
    assert bench["hash_mb_s"] > 0


def test_config_pause_zeroes_free_slots(cluster):
    fake, _worker, device_id = cluster
    fake.push({"cmd": "config", "paused": True, "slots": 3}, device_id)
    wait_for(lambda: fake.last_status.get(device_id, {}).get("free_slots") == 0,
             what="paused status")
    fake.push({"cmd": "config", "paused": False}, device_id)
    wait_for(lambda: fake.last_status.get(device_id, {}).get("free_slots") == 3,
             what="resumed status with new slot count")


def test_unknown_command_is_ignored(cluster):
    fake, _worker, device_id = cluster
    polls_before = len(fake.polls)
    fake.push({"cmd": "wat", "x": 1}, device_id)
    wait_for(lambda: len(fake.polls) > polls_before + 1, what="worker still polling")


# ---------------------------------------------------------------------------
# isolated workers (own coordinator + state dir)
# ---------------------------------------------------------------------------

def test_reattach_uses_saved_credentials(tmp_path):
    fake = FakeCoordinator(pin="123456", poll_timeout=0.3).start()
    try:
        first = WorkerProc(fake, tmp_path / "state", pin="123456", name="reattach")
        device_id = first.wait_registered()
        token = first.device_json()["token"]
        first.stop()

        # no --url and no --pin: everything comes from device.json
        args = [sys.executable, WORKER_PY, "--state-dir", str(tmp_path / "state"),
                "--verbose"]
        proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                cwd=ROOT)
        try:
            wait_for(lambda: len(fake.registrations) >= 2, what="re-registration")
            reg = fake.registrations[-1]
            assert reg["device_id"] == device_id
            assert reg["token"] == token
            assert reg["name"] == "reattach"
            wait_for(lambda: len(fake.polls) >= 2, what="poll after re-attach")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
    finally:
        fake.stop()


def test_stale_token_falls_back_to_pin(tmp_path):
    state = tmp_path / "state"
    os.makedirs(str(state))
    with open(os.path.join(str(state), "device.json"), "w") as fh:
        json.dump({"device_id": "ghost", "token": "deadbeef", "name": "ghost"}, fh)
    fake = FakeCoordinator(pin="123456", poll_timeout=0.3).start()
    worker = WorkerProc(fake, state, pin="123456", name="revived")
    try:
        device_id = worker.wait_registered(timeout=30)
        assert device_id != "ghost"
        assert worker.device_json()["device_id"] == device_id
        kinds = [(r.get("device_id") or "-") for r in fake.registrations]
        assert "ghost" in kinds
    finally:
        worker.stop()
        fake.stop()


def test_removed_command_forgets_token_and_exits(tmp_path):
    fake = FakeCoordinator(pin="123456", poll_timeout=0.3).start()
    worker = WorkerProc(fake, tmp_path / "state", pin="123456", name="doomed")
    try:
        device_id = worker.wait_registered()
        assert os.path.isfile(os.path.join(worker.state_dir, "device.json"))
        fake.push({"cmd": "removed"}, device_id)
        wait_for(lambda: worker.proc.poll() is not None, timeout=30,
                 what="worker exit\n" + worker.output)
        assert worker.proc.returncode == 0
        assert not os.path.exists(os.path.join(worker.state_dir, "device.json"))
    finally:
        worker.stop()
        fake.stop()


def test_410_during_poll_exits(tmp_path):
    fake = FakeCoordinator(pin="123456", poll_timeout=0.3).start()
    worker = WorkerProc(fake, tmp_path / "state", pin="123456", name="gone")
    try:
        device_id = worker.wait_registered()
        fake.remove_device(device_id)
        wait_for(lambda: worker.proc.poll() is not None, timeout=30,
                 what="worker exit after 410\n" + worker.output)
        assert not os.path.exists(os.path.join(worker.state_dir, "device.json"))
    finally:
        worker.stop()
        fake.stop()


def test_auto_benchmark_when_not_done(tmp_path):
    fake = FakeCoordinator(pin="123456", poll_timeout=0.3, bench_done=False).start()
    worker = WorkerProc(fake, tmp_path / "state", pin="123456", name="fresh")
    try:
        worker.wait_registered()
        wait_for(lambda: fake.benches, timeout=60, what="automatic benchmark")
        assert fake.benches[0]["kind"] == "python"
    finally:
        worker.stop()
        fake.stop()


def test_no_auto_benchmark_when_bench_done(tmp_path):
    fake = FakeCoordinator(pin="123456", poll_timeout=0.3, bench_done=True).start()
    worker = WorkerProc(fake, tmp_path / "state", pin="123456", name="known")
    try:
        worker.wait_registered()
        time.sleep(4.0)
        assert fake.benches == []
    finally:
        worker.stop()
        fake.stop()


def test_shutdown_command_stops_worker(tmp_path):
    fake = FakeCoordinator(pin="123456", poll_timeout=0.3).start()
    worker = WorkerProc(fake, tmp_path / "state", pin="123456", name="bye")
    try:
        device_id = worker.wait_registered()
        fake.push({"cmd": "shutdown"}, device_id)
        wait_for(lambda: worker.proc.poll() is not None, timeout=30,
                 what="worker exit after shutdown\n" + worker.output)
        assert fake.leaves == [device_id]
    finally:
        worker.stop()
        fake.stop()


def test_survives_coordinator_restart(tmp_path):
    fake = FakeCoordinator(pin="123456", poll_timeout=0.3).start()
    port = fake.port
    worker = WorkerProc(fake, tmp_path / "state", pin="123456", name="sticky")
    try:
        worker.wait_registered()
        wait_for(lambda: len(fake.polls) > 0, what="first poll")
        fake.stop()
        time.sleep(1.5)
        again = FakeCoordinator(pin="123456", poll_timeout=0.3)
        again.start(port)
        try:
            wait_for(lambda: len(again.registrations) >= 1, timeout=60,
                     what="reconnect after restart\n" + worker.output)
            # the saved token is unknown to the new coordinator -> 401 -> PIN
            wait_for(lambda: len(again.polls) >= 1, timeout=30,
                     what="poll after reconnect\n" + worker.output)
            assert worker.proc.poll() is None
        finally:
            again.stop()
    finally:
        worker.stop()
        try:
            fake.stop()
        except Exception:
            pass


OUTBOX_TASK = '''
import os, time
def run(args, ctx):
    path = os.path.join(ctx.workdir, "out.txt")
    with open(path, "w") as fh:
        fh.write("hello outbox")
    ctx.output(path, "out.txt")
    ctx.log("sleeping")
    time.sleep(float(args.get("sleep", 3)))
    return {"n": 7}
'''


def test_result_survives_a_coordinator_outage(tmp_path):
    """Coordinator dies mid-task -> result + output parked in the outbox ->
    delivered (upload first, then done) once the coordinator is back."""
    fake = FakeCoordinator(pin="123456", poll_timeout=0.3).start()
    port = fake.port
    worker = WorkerProc(fake, tmp_path / "state", pin="123456", name="outboxer")
    again = None
    try:
        device_id = worker.wait_registered()
        push_task(fake, device_id, "tout1", {
            "impls": {"python": {"code": OUTBOX_TASK, "entry": "run", "args": {"sleep": 3}}},
            "inputs": [], "timeout": 60})
        wait_for(lambda: "tout1" in fake.started, timeout=30,
                 what="task start\n" + worker.output)
        fake.stop()  # coordinator vanishes while the task is running

        entry = os.path.join(str(tmp_path / "state"), "outbox", "tout1", "entry.json")
        wait_for(lambda: os.path.isfile(entry), timeout=60,
                 what="outbox entry\n" + worker.output)
        with open(entry) as fh:
            saved = json.load(fh)
        assert saved["result"]["ok"] is True
        assert saved["result"]["result"] == {"n": 7}
        assert len(saved["pending"]) == 1 and saved["pending"][0]["name"] == "out.txt"
        assert "outbox: kept result of task tout1" in worker.output

        # coordinator returns, knowing the same device token
        again = FakeCoordinator(pin="123456", poll_timeout=0.3)
        again.tokens = dict(fake.tokens)
        again.devices = dict(fake.devices)
        again.start(port)

        result = wait_result(again, "tout1", timeout=60)
        assert result["ok"] is True and result["result"] == {"n": 7}
        assert [o["name"] for o in result["outputs"]] == ["out.txt"]
        assert again.file_bytes(result["outputs"][0]["file_id"]) == b"hello outbox"
        wait_for(lambda: not os.path.exists(os.path.dirname(entry)), timeout=10,
                 what="outbox entry removal\n" + worker.output)
        assert again.registrations == []  # token still valid: no re-registration
    finally:
        worker.stop()
        for server in (fake, again):
            if server is not None:
                try:
                    server.stop()
                except Exception:
                    pass


def test_outbox_replays_after_a_worker_restart(tmp_path):
    """Entries written by a previous run are delivered by the next one."""
    state_dir = tmp_path / "state"
    fake = FakeCoordinator(pin="123456", poll_timeout=0.3).start()
    worker = WorkerProc(fake, state_dir, pin="123456", name="restarter")
    try:
        device_id = worker.wait_registered()
    finally:
        worker.stop()
    # hand-craft an entry as the previous run would have left it
    entry_dir = os.path.join(str(state_dir), "outbox", "told")
    os.makedirs(entry_dir)
    with open(os.path.join(entry_dir, "out000"), "wb") as fh:
        fh.write(b"leftover bytes")
    with open(os.path.join(entry_dir, "entry.json"), "w") as fh:
        json.dump({"task_id": "told", "created": time.time(),
                   "result": {"ok": True, "result": "late", "outputs": [], "duration": 1.0},
                   "pending": [{"file": "out000", "name": "left.bin"}]}, fh)
    worker = WorkerProc(fake, state_dir, name="restarter")  # no PIN: uses device.json
    try:
        result = wait_result(fake, "told", timeout=60)
        assert result["result"] == "late"
        assert fake.file_bytes(result["outputs"][0]["file_id"]) == b"leftover bytes"
        assert result["outputs"][0]["name"] == "left.bin"
        wait_for(lambda: not os.path.exists(entry_dir), timeout=10, what="entry cleanup")
        assert "outbox : 1 undelivered result" in worker.output
        assert fake.device_ids() == [device_id]
    finally:
        worker.stop()
        fake.stop()


def test_outbox_drops_entries_the_coordinator_rejects(tmp_path):
    """A 409 (task already finished elsewhere) removes the entry for good."""
    state_dir = tmp_path / "state"
    fake = FakeCoordinator(pin="123456", poll_timeout=0.3).start()
    worker = WorkerProc(fake, state_dir, pin="123456", name="rejected")
    try:
        worker.wait_registered()
    finally:
        worker.stop()
    entry_dir = os.path.join(str(state_dir), "outbox", "gone")
    os.makedirs(entry_dir)
    with open(os.path.join(entry_dir, "entry.json"), "w") as fh:
        json.dump({"task_id": "gone", "created": time.time(),
                   "result": {"ok": True, "result": 1, "outputs": []}, "pending": []}, fh)
    fake.reject_done.add("gone")
    worker = WorkerProc(fake, state_dir, name="rejected")
    try:
        try:
            wait_for(lambda: "no longer wanted" in worker.output, timeout=60, what="rejection notice")
        except AssertionError:
            raise AssertionError("no rejection notice; worker said:\n" + worker.output)
        wait_for(lambda: not os.path.exists(entry_dir), timeout=10, what="entry cleanup")
        assert fake.result("gone") is None
    finally:
        worker.stop()
        fake.stop()


CHECKPOINT_TASK = '''
import time
def run(args, ctx):
    done = ctx.resume({"i": 0, "acc": []})
    i = done["i"]
    acc = list(done["acc"])
    if i:
        ctx.log("resumed at %d" % i)
    while i < int(args.get("n", 5)):
        acc.append(i * i)
        i += 1
        ctx.checkpoint({"i": i, "acc": acc})
        time.sleep(float(args.get("sleep", 0)))
    return {"acc": acc, "resumed_from": done["i"]}
'''


def test_python_task_checkpoints_and_resumes(cluster):
    """ctx.checkpoint() reaches the coordinator; ctx.resume() sees it next time."""
    fake, worker, device_id = cluster
    push_task(fake, device_id, "tck1", {
        "impls": {"python": {"code": CHECKPOINT_TASK, "entry": "run",
                             "args": {"n": 4, "sleep": 0.8}}},
        "inputs": [], "timeout": 60})
    result = wait_result(fake, "tck1", timeout=60)
    assert result["ok"] is True
    assert result["result"] == {"acc": [0, 1, 4, 9], "resumed_from": 0}
    posted = fake.checkpoints.get("tck1") or []
    assert posted, "no checkpoint reached the coordinator"
    assert posted[-1]["state"]["i"] >= 2  # throttled to ~1 per 2 s, so not all 4
    assert posted[-1]["attempt"] == 1

    # attempt 2 gets the saved state back and continues from there
    fake.push({"cmd": "task", "task": {"id": "tck2", "job_id": "j", "idx": 0, "attempt": 2,
                                       "checkpoint": {"i": 2, "acc": [0, 1]},
                                       "payload": {"impls": {"python": {
                                           "code": CHECKPOINT_TASK, "entry": "run",
                                           "args": {"n": 4}}}, "inputs": [], "timeout": 60}}},
              device_id)
    result = wait_result(fake, "tck2", timeout=60)
    assert result["result"] == {"acc": [0, 1, 4, 9], "resumed_from": 2}
    assert "resumed at 2" in fake.log_text("tck2")


def test_checkpoint_is_flushed_before_a_failure_is_reported(cluster):
    """A task that dies after checkpointing must not lose the last state."""
    fake, worker, device_id = cluster
    code = '''
def run(args, ctx):
    ctx.checkpoint({"progress": 7})
    raise RuntimeError("boom after checkpoint")
'''
    push_task(fake, device_id, "tck3", {
        "impls": {"python": {"code": code, "entry": "run", "args": {}}},
        "inputs": [], "timeout": 60})
    result = wait_result(fake, "tck3", timeout=60)
    assert result["ok"] is False
    assert fake.done_order.index("tck3") >= 0
    posted = fake.checkpoints.get("tck3") or []
    assert posted and posted[-1]["state"] == {"progress": 7}
    # checkpoint arrived before done
    assert fake.checkpoint_times["tck3"] <= fake.done_times["tck3"]


def test_shell_task_checkpoints_via_file(cluster):
    fake, worker, device_id = cluster
    if IS_WINDOWS:
        cmd = 'echo {"step": 3} > "%PC_CHECKPOINT_FILE%" && ping -n 3 127.0.0.1 > nul && echo prev=%PC_CHECKPOINT%'
    else:
        cmd = 'echo \'{"step": 3}\' > "$PC_CHECKPOINT_FILE" && sleep 2 && echo prev=$PC_CHECKPOINT'
    fake.push({"cmd": "task", "task": {"id": "tsh1", "job_id": "j", "idx": 0, "attempt": 2,
                                       "checkpoint": {"step": 1},
                                       "payload": {"impls": {"shell": {"cmd": cmd}},
                                                   "inputs": [], "timeout": 60}}},
              device_id)
    result = wait_result(fake, "tsh1", timeout=60)
    assert result["ok"] is True, result
    assert 'prev={"step": 1}' in result["result"]["stdout"]
    posted = fake.checkpoints.get("tsh1") or []
    assert posted and posted[-1]["state"] == {"step": 3}


def test_range_inputs_are_fetched_partially(cluster):
    """A dict input {file_id, offset, length} arrives as a byte slice."""
    fake, worker, device_id = cluster
    data = b"".join(b"row %02d\n" % i for i in range(20))
    fid = fake.add_file("rows.txt", data)
    code = '''
def run(args, ctx):
    return open(ctx.inputs[0], "rb").read().decode()
'''
    push_task(fake, device_id, "trng", {
        "impls": {"python": {"code": code, "entry": "run", "args": {}}},
        "inputs": [{"file_id": fid, "offset": 14, "length": 21}], "timeout": 60})
    result = wait_result(fake, "trng", timeout=60)
    assert result["ok"] is True
    assert result["result"] == data[14:35].decode()
    assert fake.range_requests and fake.range_requests[-1] == (fid, 14, 34)


class UdpResponder(object):
    """Answers discovery probes with whatever URL the test points it at."""

    def __init__(self):
        import socket

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.url = None
        self.probes = 0
        self._stop = False
        self._thread = threading.Thread(target=self._serve)
        self._thread.daemon = True
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                data, addr = self.sock.recvfrom(1024)
            except Exception:
                continue
            if data.startswith(b"PHONECLUSTER_DISCOVER") and self.url:
                self.probes += 1
                self.sock.sendto(json.dumps({"phonecluster": 1, "url": self.url,
                                             "urls": [self.url], "port": 0,
                                             "name": "test"}).encode(), addr)

    def stop(self):
        self._stop = True
        self.sock.close()
        self._thread.join(timeout=2)


def test_worker_finds_the_coordinator_by_discovery(tmp_path):
    fake = FakeCoordinator(pin="123456", poll_timeout=0.3).start()
    responder = UdpResponder()
    responder.url = fake.url
    worker = WorkerProc(fake, tmp_path / "state", pin="123456", name="finder",
                        extra=["--url", "auto", "--discover-host", "127.0.0.1",
                               "--discovery-port", str(responder.port)])
    try:
        worker.wait_registered(timeout=30)
        assert responder.probes >= 1
        assert "found coordinator at " + fake.url in worker.output
        assert worker.device_json()["url"] == fake.url
    finally:
        worker.stop()
        responder.stop()
        fake.stop()


def test_worker_rediscovers_when_the_coordinator_moves(tmp_path):
    """Saved URL dies; the LAN says the coordinator now lives elsewhere."""
    first = FakeCoordinator(pin="123456", poll_timeout=0.3).start()
    responder = UdpResponder()
    responder.url = first.url
    worker = WorkerProc(first, tmp_path / "state", pin="123456", name="mover",
                        extra=["--discover-host", "127.0.0.1",
                               "--discovery-port", str(responder.port)])
    second = None
    try:
        worker.wait_registered(timeout=30)
        first.stop()
        second = FakeCoordinator(pin="123456", poll_timeout=0.3).start()
        responder.url = second.url
        wait_for(lambda: second.device_ids(), timeout=90,
                 what="registration with the moved coordinator\n" + worker.output)
        assert "coordinator moved" in worker.output
        assert worker.device_json()["url"] == second.url
        assert worker.proc.poll() is None
    finally:
        worker.stop()
        responder.stop()
        for server in (first, second):
            if server is not None:
                try:
                    server.stop()
                except Exception:
                    pass


def test_auto_url_without_pin_or_saved_device_exits(tmp_path):
    proc = subprocess.run(
        [sys.executable, WORKER_PY, "--url", "auto", "--state-dir", str(tmp_path / "empty")],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    assert proc.returncode == 2
    assert b"has not joined" in proc.stderr


# ---------------------------------------------------------------------------
# services (SPEC 6.7)
# ---------------------------------------------------------------------------

SERVICE_CODE = '''
import json, time, wave, io

def load(ctx):
    ctx.log("loading model for %s" % ctx.service_name)
    ctx.log("service_dir=%s inputs=%d params=%s" % (bool(ctx.service_dir), len(ctx.inputs), json.dumps(ctx.params)))
    if ctx.params.get("fail_load"):
        raise RuntimeError("model missing")
    return {"loaded_at": time.time()}

def serve(request, ctx, model):
    if request.params.get("boom"):
        raise ValueError("bad audio")
    if request.params.get("hang"):
        time.sleep(30)
    total = 0
    chunks = 0
    for chunk in request.stream():
        total += len(chunk)
        chunks += 1
        ctx.emit({"so_far": total})
    if request.params.get("wav"):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
            w.writeframes(b"\\x00\\x00" * 800)
        ctx.output_bytes(buf.getvalue(), "speech.wav")
    return {"bytes": total, "chunks": chunks, "content_type": request.content_type,
            "name": request.name, "params": request.params, "model": bool(model)}
'''


def service_task(task_id, name="echo", params=None, setup_cmd=None, request_timeout=30):
    return {"cmd": "task", "task": {"id": task_id, "job_id": "sj", "idx": 0, "attempt": 1, "payload": {
        "impls": {"python": {"code": SERVICE_CODE, "entry": "serve", "args": {}}},
        "inputs": [], "timeout": 0, "requires": ["service"],
        "service": {"name": name, "request_timeout": request_timeout, "setup_cmd": setup_cmd,
                    "params": params or {}, "meta": {"accepts": "audio/wav"}}}}}


def test_service_loads_announces_ready_and_streams_requests(tmp_path):
    fake = FakeCoordinator(pin="123456", poll_timeout=0.3).start()
    worker = WorkerProc(fake, tmp_path / "state", pin="123456", name="host", slots=2)
    try:
        device_id = worker.wait_registered()
        assert "service" in fake.registrations[0]["caps"]
        fake.push(service_task("svc1", setup_cmd="echo setup-ran"), device_id)
        wait_for(lambda: fake.service_ready, timeout=60, what="service ready\n" + worker.output)
        assert fake.service_ready[0][:2] == ("svc1", "echo")
        wait_for(lambda: "loading model for echo" in fake.log_text("svc1"), timeout=10,
                 what="service log lines")
        assert "[setup] setup-ran" in fake.log_text("svc1")

        # a streamed request: 4 network chunks -> partials as they arrive
        data = b"x" * 40000
        fake.send_request(device_id, "svc1", "r1", data, "audio/wav", {"lang": "en"}, name="a.wav")
        result = wait_for(lambda: fake.request_result("r1"), timeout=60,
                          what="request result\n" + worker.output)
        assert result["ok"] is True, result
        assert result["result"]["bytes"] == 40000 and result["result"]["chunks"] >= 2
        assert result["result"]["content_type"] == "audio/wav" and result["result"]["name"] == "a.wav"
        assert result["result"]["params"] == {"lang": "en"} and result["result"]["model"] is True
        partials = fake.request_partials["r1"]
        assert partials[-1] == {"so_far": 40000} and len(partials) >= 2
        assert result["ms"] >= 0

        # a second request on the same loaded model; binary output uploaded
        fake.send_request(device_id, "svc1", "r2", b"hi", "text/plain", {"wav": True})
        result = wait_for(lambda: fake.request_result("r2"), timeout=60, what="second result")
        assert result["ok"] and result["result"]["bytes"] == 2
        assert [o["name"] for o in result["outputs"]] == ["speech.wav"]
        assert fake.file_bytes(result["outputs"][0]["file_id"])[:4] == b"RIFF"
        assert fake.log_text("svc1").count("loading model") == 1  # loaded once

        # a request whose handler raises: error reported, service keeps running
        fake.send_request(device_id, "svc1", "r3", b"", "text/plain", {"boom": True})
        result = wait_for(lambda: fake.request_result("r3"), timeout=60, what="failed result")
        assert result["ok"] is False and "bad audio" in result["error"]
        assert "ValueError" in (result.get("traceback") or "")
        fake.send_request(device_id, "svc1", "r4", b"still alive", "text/plain")
        result = wait_for(lambda: fake.request_result("r4"), timeout=60, what="r4")
        assert result["ok"] and result["result"]["bytes"] == 11
        assert fake.log_text("svc1").count("loading model") == 1

        # the task never finishes on its own
        assert fake.result("svc1") is None
        # cancel -> child stopped, task reports cancelled
        fake.push({"cmd": "cancel", "task_id": "svc1"}, device_id)
        result = wait_for(lambda: fake.result("svc1"), timeout=60, what="service stop")
        assert result["ok"] is False and result["error"] == "cancelled"
    finally:
        worker.stop()
        fake.stop()


def test_service_request_timeout_restarts_the_model(tmp_path):
    fake = FakeCoordinator(pin="123456", poll_timeout=0.3).start()
    worker = WorkerProc(fake, tmp_path / "state", pin="123456", name="host2")
    try:
        device_id = worker.wait_registered()
        fake.push(service_task("svc2", request_timeout=3), device_id)
        wait_for(lambda: fake.service_ready, timeout=60, what="ready")
        fake.send_request(device_id, "svc2", "slow", b"zz", "text/plain", {"hang": True})
        result = wait_for(lambda: fake.request_result("slow"), timeout=60, what="timeout result")
        assert result["ok"] is False and "timed out" in result["error"]
        # model comes back and answers again
        wait_for(lambda: len(fake.service_ready) >= 2, timeout=60, what="ready again\n" + worker.output)
        fake.send_request(device_id, "svc2", "after", b"ok", "text/plain")
        result = wait_for(lambda: fake.request_result("after"), timeout=60, what="after")
        assert result["ok"] and result["result"]["bytes"] == 2
    finally:
        worker.stop()
        fake.stop()


def test_service_that_cannot_load_fails_the_task(tmp_path):
    fake = FakeCoordinator(pin="123456", poll_timeout=0.3).start()
    worker = WorkerProc(fake, tmp_path / "state", pin="123456", name="host3")
    try:
        device_id = worker.wait_registered()
        fake.push(service_task("svc3", params={"fail_load": True}), device_id)
        result = wait_for(lambda: fake.result("svc3"), timeout=120, what="load failure\n" + worker.output)
        assert result["ok"] is False and "crash" in result["error"]
        assert "model missing" in fake.log_text("svc3")
        assert fake.service_ready == []
    finally:
        worker.stop()
        fake.stop()


# ---------------------------------------------------------------------------
# provisioning: llama.cpp runtime and model files (SPEC 6.8)
# ---------------------------------------------------------------------------

class _WorkerStub(object):
    """Just what Provisioner needs: an authenticated HttpClient and a state dir."""

    def __init__(self, w, fake, state_dir, info=None):
        self.http = w.HttpClient(fake.url)
        reg = self.http.call("POST", "/api/w/register",
                             {"pin": "123456", "name": "prov", "platform": "windows",
                              "caps": ["python"], "info": {}, "slots": 1})
        self.http.token = reg["token"]
        self.state_dir = str(state_dir)
        self.info = info or {}
        self.name = "prov"


def _fake_runtime(fake, tag="b1", platform_key="win-x64"):
    import io as _io
    import tarfile as _tarfile
    import zipfile as _zipfile

    if platform_key == "win-x64":
        buf = _io.BytesIO()
        with _zipfile.ZipFile(buf, "w") as zf:
            for name in ("llama-server.exe", "ggml-rpc-server.exe", "ggml.dll"):
                zf.writestr(name, b"MZ fake " + name.encode())
        name = "llama-%s-bin-win-cpu-x64.zip" % tag
    else:
        buf = _io.BytesIO()
        with _tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for fname in ("llama-server", "ggml-rpc-server", "libllama.so"):
                data = b"\x7fELF fake " + fname.encode()
                info = _tarfile.TarInfo("llama-%s/%s" % (tag, fname))
                info.size = len(data)
                info.mode = 0o755
                tar.addfile(info, _io.BytesIO(data))
        name = "llama-%s-bin-android-arm64.tar.gz" % tag
    data = buf.getvalue()
    fake.runtime = {"tag": tag, "assets": {platform_key: {"file": name, "size": len(data)}}}
    fake.runtime_files[name] = data
    return name


def test_llama_platform_choices(w):
    win = {"abi": "x86_64", "android_api": None}
    if w.IS_WINDOWS:
        assert w.llama_platform(win) == "win-x64"
    else:
        assert w.llama_platform({"abi": "arm64-v8a", "android_api": 30}) == "android-arm64"
        # Redmi 5A: Android 7 -> the prebuilt (API 28+) does not run, compile instead
        assert w.llama_platform({"abi": "arm64-v8a", "android_api": 25}) == "source"
        assert w.llama_platform({"abi": "armeabi-v7a", "android_api": 30}) == "source"


def test_provisioner_fetches_and_extracts_the_runtime_once(w, tmp_path, monkeypatch):
    if not w.IS_WINDOWS:
        pytest.skip("this runtime test uses the Windows archive layout")
    monkeypatch.delenv("PC_ROOT", raising=False)
    fake = FakeCoordinator(pin="123456").start()
    try:
        archive = _fake_runtime(fake, "b1")
        lines = []
        prov = w.Provisioner(_WorkerStub(w, fake, tmp_path / "state", {"abi": "x86_64"}), lines.append)
        path = prov.llama("b1")
        assert os.path.isfile(os.path.join(path, "llama-server.exe"))
        assert w.find_binary(path, ("ggml-rpc-server", "rpc-server")).endswith("ggml-rpc-server.exe")
        assert not os.path.exists(os.path.join(str(tmp_path / "state"), "runtime", archive))  # cleaned up
        assert fake.runtime_downloads == [archive]
        # second call: already installed, no download
        assert prov.llama("b1") == path
        assert fake.runtime_downloads == [archive]
        # a coordinator on another version is refused (RPC needs identical builds)
        with pytest.raises(RuntimeError) as info:
            prov.llama("b2")
        assert "b1" in str(info.value) and "b2" in str(info.value)
    finally:
        fake.stop()


def test_provisioner_uses_the_pc_copy_when_local(w, tmp_path, monkeypatch):
    if not w.IS_WINDOWS:
        pytest.skip("the PC shortcut is for the Windows worker")
    root = tmp_path / "root"
    win = root / "tools" / "llama.cpp" / "win-x64"
    win.mkdir(parents=True)
    (win / "llama-server.exe").write_bytes(b"MZ")
    (root / "tools" / "llama.cpp" / "VERSION").write_text("b7\n")
    (root / "data" / "models").mkdir(parents=True)
    (root / "data" / "models" / "m.gguf").write_bytes(b"GGUF" + b"x" * 10)
    monkeypatch.setenv("PC_ROOT", str(root))

    class Bare(object):
        state_dir = str(tmp_path / "state")
        info = {"abi": "x86_64"}
        name = "pc"
        http = None  # must not be used

    prov = w.Provisioner(Bare(), lambda line: None)
    assert prov.llama("b7") == str(win)
    assert prov.model("m.gguf", 14) == str(root / "data" / "models" / "m.gguf")
    # the GPU build exists only on the PC, and only when it was downloaded
    with pytest.raises(RuntimeError) as info:
        prov.llama("b7", "gpu")
    assert "-Vulkan" in str(info.value)
    vk = root / "tools" / "llama.cpp" / "win-vulkan-x64"
    vk.mkdir()
    (vk / "llama-server.exe").write_bytes(b"MZ")
    assert prov.llama("b7", "gpu") == str(vk)
    with pytest.raises(RuntimeError):
        prov.llama("b8", "gpu")                     # same version rule as everywhere


SPAWNING_SERVICE = (
    "import sys\n"
    "def load(ctx):\n"
    "    child = ctx.spawn([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "    ctx.announce({'child': child.pid})\n"
    "    return child\n"
    "def serve(request, ctx, model):\n"
    "    return 1\n"
)


def test_a_service_helper_process_dies_with_the_service(w, tmp_path):
    """llama-server must not outlive its service, even when the service
    process is killed outright (no chance to unload, no tree kill)."""
    import psutil

    spec = {"code": SPAWNING_SERVICE, "workdir": str(tmp_path), "service_name": "t", "task_id": "t1"}
    proc = subprocess.Popen([sys.executable, "-c", w.SERVICE_HARNESS], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    proc.stdin.write((json.dumps(spec) + "\n").encode("utf-8"))
    proc.stdin.flush()
    child = None
    deadline = time.time() + 30
    while time.time() < deadline and child is None:
        line = proc.stdout.readline().decode("utf-8", "replace").strip()
        if line.startswith(w.SENT_READY):
            child = json.loads(line[len(w.SENT_READY):])["announce"]["child"]
    assert child and psutil.pid_exists(child)
    proc.kill()                                     # the service process alone
    proc.wait(timeout=10)

    def gone():
        try:
            return psutil.Process(child).status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return True

    wait_for(gone, timeout=10, what="the helper process to follow its service")


NVIDIA_SMI = "0, NVIDIA GeForce RTX 3050 Laptop GPU, 4096, 3487, 47, 3\n"
LIST_DEVICES = """Available devices:
  Vulkan0: NVIDIA GeForce RTX 3050 Laptop GPU (3964 MiB, 3369 MiB free)
  Vulkan1: AMD Radeon(TM) Graphics (8113 MiB, 7707 MiB free)
"""


def test_gpu_readings_are_parsed_and_matched(w):
    cards = w.parse_nvidia_smi(NVIDIA_SMI + "garbage line\n")
    assert cards == [{"index": 0, "name": "NVIDIA GeForce RTX 3050 Laptop GPU", "vram_total_mb": 4096,
                      "vram_free_mb": 3487, "temp": 47, "util": 3}]
    vulkan = w.parse_vulkan_devices(LIST_DEVICES)
    assert [(v["vk_index"], v["total_mb"]) for v in vulkan] == [(0, 3964), (1, 8113)]
    matched = w.match_gpus(cards, vulkan)
    # only the card whose temperature we can read; the integrated GPU shares system RAM
    assert matched == [{"name": "NVIDIA GeForce RTX 3050 Laptop GPU", "vram_mb": 4096, "vk_index": 0,
                        "nvidia_index": 0}]
    assert w.match_gpus([], vulkan) == []
    assert w.parse_nvidia_smi("") == [] and w.parse_vulkan_devices(None) == []


def test_gpu_monitor_refreshes_in_the_background(w, monkeypatch):
    calls = []
    release = threading.Event()

    def fake_capture(args, timeout=10):
        calls.append(args)
        release.wait(5)                             # a slow nvidia-smi
        return NVIDIA_SMI.replace(" 47,", " 63,")

    monkeypatch.setattr(w, "run_capture", fake_capture)
    monitor = w.GpuMonitor([{"name": "x", "vk_index": 0, "nvidia_index": 0}], "nvidia-smi")
    assert monitor.read() is None                   # the poll never waits for nvidia-smi
    release.set()
    deadline = time.time() + 5
    while monitor.latest is None and time.time() < deadline:
        time.sleep(0.02)
    assert monitor.read()["temp"] == 63 and monitor.read()["vram_free_mb"] == 3487
    assert len(calls) == 1                          # at most every 10 s
    assert w.GpuMonitor([], None).read() is None


def test_default_slots_use_every_core_but_one(w, tmp_path):
    worker = w.Worker("http://127.0.0.1:9", pin="1", name="t", state_dir=str(tmp_path / "s"))
    assert worker.slots == max(1, (os.cpu_count() or 1) - 1) and not worker.slots_set
    worker._save_device()
    again = w.Worker("http://127.0.0.1:9", pin="1", name="t", state_dir=str(tmp_path / "s"))
    assert not again.slots_set                     # a default is not remembered as a choice
    fixed = w.Worker("http://127.0.0.1:9", pin="1", name="t", slots=3, state_dir=str(tmp_path / "s"))
    fixed._save_device()
    later = w.Worker("http://127.0.0.1:9", pin="1", name="t", state_dir=str(tmp_path / "s"))
    assert later.slots == 3 and later.slots_set


def test_provisioner_downloads_a_model_and_resumes(w, tmp_path, monkeypatch):
    monkeypatch.delenv("PC_ROOT", raising=False)
    fake = FakeCoordinator(pin="123456").start()
    try:
        data = b"GGUF" + os.urandom(300000)
        fake.models["tiny.gguf"] = data
        fake.model_cut["tiny.gguf"] = 100000   # first attempt drops after 100 kB
        lines = []
        prov = w.Provisioner(_WorkerStub(w, fake, tmp_path / "state"), lines.append)
        with pytest.raises(Exception):
            prov.model("tiny.gguf", len(data))
        part = os.path.join(str(tmp_path / "state"), "models", "tiny.gguf.part")
        assert os.path.getsize(part) == 100000
        path = prov.model("tiny.gguf", len(data))   # resumes with a Range request
        with open(path, "rb") as fh:
            assert fh.read() == data
        assert fake.range_requests[-1] == ("tiny.gguf", 100000, len(data) - 1)
        assert not os.path.exists(part)
        # cached: no new request
        count = len(fake.range_requests)
        assert prov.model("tiny.gguf", len(data)) == path
        assert len(fake.range_requests) == count
    finally:
        fake.stop()


ANNOUNCE_CODE = '''
def load(ctx):
    ctx.announce({"endpoint": "%s:1234" % ctx.lan_ip(), "runtime": ctx.runtime_dir,
                  "model": ctx.model_path})
    return None

def serve(request, ctx, model):
    return {"ok": True}
'''


def test_service_announces_facts_and_gets_its_runtime(tmp_path, monkeypatch):
    """needs.llama + needs.model are provisioned before load(); ctx.announce()
    facts arrive with the ready signal; the service re-announces after the
    worker re-registers (coordinator restart)."""
    fake = FakeCoordinator(pin="123456", poll_timeout=0.3).start()
    archive = _fake_runtime(fake, "b3") if os.name == "nt" else _fake_runtime(fake, "b3", "android-arm64")
    fake.models["m.gguf"] = b"GGUF" + b"z" * 5000
    env_root = os.environ.pop("PC_ROOT", None)
    worker = WorkerProc(fake, tmp_path / "state", pin="123456", name="announcer")
    try:
        device_id = worker.wait_registered()
        if os.name != "nt":
            pytest.skip("runtime layout test is Windows-only here")
        fake.push({"cmd": "task", "task": {"id": "ann1", "job_id": "j", "idx": 0, "attempt": 1, "payload": {
            "impls": {"python": {"code": ANNOUNCE_CODE, "entry": "serve", "args": {}}},
            "inputs": [], "timeout": 0, "requires": ["service"],
            "service": {"name": "_llm.x.y", "request_timeout": 30, "params": {},
                        "needs": {"llama": "b3", "model": {"name": "m.gguf", "size": 5004}},
                        "meta": {"role": "shard"}}}}}, device_id)
        wait_for(lambda: fake.service_ready, timeout=60, what="ready\n" + worker.output)
        task_id, name, meta = fake.service_ready[0]
        assert name == "_llm.x.y" and meta["role"] == "shard"
        host, port = meta["endpoint"].rsplit(":", 1)
        assert port == "1234" and host.count(".") == 3
        assert meta["runtime"].endswith("llama-b3") and meta["model"].endswith("m.gguf")
        assert archive in fake.runtime_downloads
        # the worker re-registers (e.g. its token was forgotten by a restarted
        # coordinator) -> the running service is announced again
        with fake.lock:
            fake.tokens.clear()
        wait_for(lambda: len(fake.service_ready) >= 2, timeout=60,
                 what="re-announce after re-registration\n" + worker.output)
        assert fake.service_ready[-1][0] == "ann1"
    finally:
        worker.stop()
        fake.stop()
        if env_root is not None:
            os.environ["PC_ROOT"] = env_root


def test_outbox_class_add_entries_remove(w, tmp_path):
    class Dummy(object):
        pass

    box = w.Outbox(Dummy(), str(tmp_path / "box"))
    assert box.count() == 0
    src = tmp_path / "file.bin"
    src.write_bytes(b"abc")
    box.add("t1", {"ok": True, "result": 5}, [{"path": str(src), "name": "file.bin"},
                                             {"path": str(tmp_path / "missing"), "name": "x"}])
    assert box.count() == 1
    assert not src.exists()  # moved, not copied
    entries = box.entries()
    assert entries[0]["task_id"] == "t1"
    assert entries[0]["pending"] == [{"file": "out000", "name": "file.bin"}]
    assert (tmp_path / "box" / "t1" / "out000").read_bytes() == b"abc"
    box.remove("t1")
    assert box.count() == 0 and box.entries() == []


def test_missing_url_exits_with_message(tmp_path):
    proc = subprocess.run(
        [sys.executable, WORKER_PY, "--state-dir", str(tmp_path / "empty")],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    assert proc.returncode == 2
    assert b"has not joined" in proc.stderr and b"Join" in proc.stderr


# ---------------------------------------------------------------------------
# pure-function unit tests (worker.py imported directly)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def w():
    spec = importlib.util.spec_from_file_location("pc_worker_mod", WORKER_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("raw,expected", [
    ("350", 35.0),        # tenths of a degree (sysfs battery/temp)
    ("35000", 35.0),      # millidegrees (thermal_zone/temp)
    ("42", 42.0),
    ("  268 \n", 26.8),
    ("0", 0.0),
    ("", None),
    ("n/a", None),
    (None, None),
    ("99999999", None),
])
def test_parse_temp_value(w, raw, expected):
    assert w.parse_temp_value(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Charging", True),
    ("charging\n", True),
    ("Full", True),
    ("Discharging", False),
    ("Not charging", False),
    ("Not_charging", False),
    ("Unknown", None),
    ("", None),
    (None, None),
])
def test_parse_charging(w, raw, expected):
    assert w.parse_charging(raw) is expected


@pytest.mark.parametrize("raw,expected", [
    ("87", 87), ("0", 0), ("100", 100), ("101", None), ("-1", None),
    ("", None), (None, None), ("55.0", 55),
])
def test_parse_capacity(w, raw, expected):
    assert w.parse_capacity(raw) == expected


def test_parse_termux_battery(w):
    payload = json.dumps({"health": "GOOD", "percentage": 64,
                          "plugged": "UNPLUGGED", "status": "DISCHARGING",
                          "temperature": 30.2})
    assert w.parse_termux_battery(payload) == (64, False, 30.2)
    charging = json.dumps({"percentage": 100, "plugged": "PLUGGED_AC",
                           "status": "CHARGING", "temperature": 41.0})
    assert w.parse_termux_battery(charging) == (100, True, 41.0)


def _sysfs(root, files):
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def test_linux_phone_battery_and_temperature(w, tmp_path):
    """A phone running Linux (the J2): the Android-style "battery" supply, its own
    temperature file first, else the chip's thermal zone whatever it is called."""
    supply = _sysfs(tmp_path / "ps", {
        "battery/type": "Battery\n", "battery/capacity": "100\n", "battery/status": "Full\n",
        "max77843-fuelgauge/capacity": "100\n",
    })
    thermal = _sysfs(tmp_path / "th", {"thermal_zone0/type": "ap_therm\n", "thermal_zone0/temp": "35000\n"})
    monitor = w.BatteryMonitor()
    assert monitor._read_linux(str(supply), str(thermal)) == (100, True, 35.0)
    (supply / "battery" / "temp").write_text("312\n")
    (supply / "battery" / "status").write_text("Discharging\n")
    assert monitor._read_linux(str(supply), str(thermal)) == (100, False, 31.2)


def test_linux_laptop_and_desktop_battery(w, tmp_path):
    supply = _sysfs(tmp_path / "ps", {
        "AC/type": "Mains\n", "AC/online": "1\n",
        # a wireless mouse's battery (sorts first here) is not the device's own
        "AAA-mouse/type": "Battery\n", "AAA-mouse/scope": "Device\n", "AAA-mouse/capacity": "20\n",
        "BAT0/type": "Battery\n", "BAT0/capacity": "76\n", "BAT0/status": "Charging\n",
    })
    thermal = _sysfs(tmp_path / "th", {"thermal_zone0/type": "x86_pkg_temp\n", "thermal_zone0/temp": "61000\n"})
    monitor = w.BatteryMonitor()
    # the laptop's battery; no CPU temperature (it runs far above the phone limits)
    assert monitor._read_linux(str(supply), str(thermal)) == (76, True, None)
    desktop = _sysfs(tmp_path / "desk", {"AC/type": "Mains\n"})
    assert monitor._read_linux(str(desktop), str(thermal)) == (None, None, None)
    assert monitor._read_linux(str(tmp_path / "missing"), str(thermal)) == (None, None, None)


def test_thermal_zone_preference(w, tmp_path):
    thermal = _sysfs(tmp_path / "th", {
        "thermal_zone0/type": "gpu\n", "thermal_zone0/temp": "48000\n",
        "thermal_zone1/type": "cpu-0\n", "thermal_zone1/temp": "41000\n",
        "thermal_zone2/type": "battery\n", "thermal_zone2/temp": "33000\n",
    })
    monitor = w.BatteryMonitor()
    assert monitor._read_thermal_zone(str(thermal)) == 33.0
    (thermal / "thermal_zone2" / "temp").write_text("")
    assert monitor._read_thermal_zone(str(thermal)) == 41.0
    only_gpu = _sysfs(tmp_path / "th2", {"thermal_zone0/type": "gpu\n", "thermal_zone0/temp": "48000\n"})
    assert monitor._read_thermal_zone(str(only_gpu)) is None
    assert monitor._read_thermal_zone(str(only_gpu), any_zone=True) == 48.0
    plugged_only = json.dumps({"percentage": 50, "plugged": "PLUGGED_USB",
                               "status": "UNKNOWN"})
    assert w.parse_termux_battery(plugged_only) == (50, True, None)
    assert w.parse_termux_battery("not json") == (None, None, None)
    assert w.parse_termux_battery("") == (None, None, None)


def test_parse_win_battery(w):
    assert w.parse_win_battery("85,2") == (85, True)
    assert w.parse_win_battery("  71,1  \n") == (71, False)
    assert w.parse_win_battery("100,3") == (100, True)
    assert w.parse_win_battery("") == (None, None)
    assert w.parse_win_battery("\n") == (None, None)
    assert w.parse_win_battery("nonsense") == (None, None)


def test_parse_meminfo(w):
    text = ("MemTotal:        2027960 kB\n"
            "MemFree:          123456 kB\n")
    assert w.parse_meminfo(text) == 1980
    assert w.parse_meminfo("") is None
    assert w.parse_meminfo(None) is None
    assert w.parse_meminfo("MemTotal: junk kB\n") is None


def test_detect_caps(w, tmp_path):
    caps = w.detect_caps(str(tmp_path))
    assert "python" in caps and "shell" in caps
    assert w.platform_tag() in caps
    assert len(caps) == len(set(caps))
    assert ("pty" in caps) == w.pty_supported()
    if w.IS_WINDOWS:
        assert "windows" in caps
    # tmp_path lives on a real disk, so >= 200 MB free is expected here
    free = w.free_mb(str(tmp_path))
    assert ("storage" in caps) == (free is not None and free >= 200)


def test_find_ffmpeg_uses_pc_root(w, tmp_path, monkeypatch):
    fake_root = tmp_path / "root"
    tools = fake_root / "tools" / "ffmpeg"
    os.makedirs(str(tools))
    name = "ffmpeg.exe" if w.IS_WINDOWS else "ffmpeg"
    target = tools / name
    target.write_text("#!/bin/sh\n")
    monkeypatch.setenv("PC_ROOT", str(fake_root))
    monkeypatch.setattr(w.shutil, "which", lambda _cmd: None)
    assert w.find_ffmpeg() == str(target)
    monkeypatch.setattr(w.shutil, "which", lambda _cmd: "/usr/bin/ffmpeg")
    assert w.find_ffmpeg() == "/usr/bin/ffmpeg"


def test_find_ffmpeg_missing(w, tmp_path, monkeypatch):
    monkeypatch.setenv("PC_ROOT", str(tmp_path))
    monkeypatch.setattr(w.shutil, "which", lambda _cmd: None)
    assert w.find_ffmpeg() is None


def test_file_cache_lru_eviction(w, tmp_path):
    cache = w.FileCache(str(tmp_path / "cache"), cap_mb=10)
    base = time.time() - 1000
    ids = []
    for i in range(5):
        file_id = "file%02d" % i
        cache.put_bytes(file_id, b"x" * 4000)
        ids.append(file_id)
        # deterministic ages regardless of filesystem timestamp resolution
        os.utime(cache.path(file_id), (base + i, base + i))
    cache.cap = 10240
    removed = cache.enforce()
    assert len(removed) == 3
    remaining = sorted(os.listdir(cache.dir))
    assert remaining == ids[-2:]
    total = sum(os.path.getsize(os.path.join(cache.dir, n)) for n in remaining)
    assert total <= cache.cap
    assert cache.enforce() == []


def test_file_cache_get_and_reuse(w, tmp_path):
    cache = w.FileCache(str(tmp_path / "cache"), cap_mb=10)
    assert cache.get("nope") is None
    path = cache.put_bytes("abc", b"hello")
    assert cache.get("abc") == path
    with open(path, "rb") as fh:
        assert fh.read() == b"hello"
    assert cache.enforce() == []


def test_file_cache_keeps_newest(w, tmp_path):
    cache = w.FileCache(str(tmp_path / "cache"), cap_mb=0.005)  # 5 120 bytes
    cache.put_bytes("old", b"o" * 3000)
    os.utime(cache.path("old"), (1000, 1000))
    cache.put_bytes("new", b"n" * 3000)
    os.utime(cache.path("new"), (time.time(), time.time()))
    cache.enforce()
    assert not os.path.exists(cache.path("old"))
    assert os.path.exists(cache.path("new"))


def test_tail_keeps_last_bytes(w):
    tail = w.Tail(10)
    tail.add(b"abcdefghij")
    tail.add(b"KLMNO")
    assert tail.text() == "fghijKLMNO"


def test_benchmark_shape(w):
    result = w.run_benchmark()
    assert set(result) == {"score", "loop_iters", "hash_mb_s", "kind"}
    assert result["kind"] == "python"
    assert result["loop_iters"] > 0
    assert result["score"] == result["loop_iters"] / 1e6
    assert result["hash_mb_s"] > 0


def test_http_client_base_url(w):
    client = w.HttpClient("https://192.168.1.5:8765")
    assert client.host == "192.168.1.5"
    assert client.port == 8765
    assert client.base_url == "https://192.168.1.5:8765"
    bare = w.HttpClient("192.168.1.5:8765")
    assert bare.scheme == "https" and bare.port == 8765
    local = w.HttpClient("http://127.0.0.1:8764/")
    assert local.port == 8764 and local.base_url == "http://127.0.0.1:8764"
    # plain http only on the PC itself (SPEC 3)
    with pytest.raises(ValueError):
        w.HttpClient("http://192.168.1.5:8765")
    # https without the cluster CA refuses to connect at all
    with pytest.raises(w.ApiError):
        client._new_conn(5)


def test_collect_info(w):
    info = w.collect_info()
    assert info["cpu_count"] >= 1
    assert info["python"].count(".") >= 1
    assert info["shell"]
    assert info["model"]


def test_harness_is_py38_safe(w):
    compile(w.HARNESS, "<harness>", "exec")
    assert "__PC_RESULT__" in w.HARNESS
    assert "__PC_ERROR__" in w.HARNESS
    assert "__PC_PROGRESS__" in w.HARNESS


# ---------------------------------------------------------------------------
# syntax gates for the other supported runtimes
# ---------------------------------------------------------------------------

OLDER_PYTHONS = ("3.8", "3.9", "3.10")


def older_python():
    """A Python older than the cluster's own, as a 3.8 syntax proxy: ``PC_OLD_PYTHON``,
    else python3.8 to 3.10 on PATH, else one the Windows ``py`` launcher knows."""
    named = os.environ.get("PC_OLD_PYTHON")
    if named and os.path.isfile(named):
        return [named]
    for version in OLDER_PYTHONS:
        found = shutil.which("python" + version)
        if found:
            return [found]
    launcher = shutil.which("py")
    for version in OLDER_PYTHONS if launcher else ():
        probe = subprocess.run([launcher, "-" + version, "-c", "pass"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=60)
        if probe.returncode == 0:
            return [launcher, "-" + version]
    return None


def test_worker_compiles_on_older_python(tmp_path):
    exe = older_python()
    if exe is None:
        pytest.skip("no older Python interpreter available as a 3.8 syntax proxy")
    proc = subprocess.run(exe + ["-m", "py_compile", WORKER_PY],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          cwd=str(tmp_path), timeout=120)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")


@pytest.mark.parametrize("name", ["worker.js", "runner.js"])
def test_browser_js_syntax(name):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    path = os.path.join(BROWSER_DIR, name)
    if not os.path.isfile(path):
        pytest.skip("%s not present" % name)
    proc = subprocess.run([node, "--check", path], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")


@pytest.mark.parametrize("name", ["worker.js", "runner.js", "index.html", "style.css"])
def test_browser_assets_are_es2017_and_offline(name):
    path = os.path.join(BROWSER_DIR, name)
    if not os.path.isfile(path):
        pytest.skip("%s not present" % name)
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    assert "??" not in text, "no nullish coalescing (Chrome 80)"
    assert "?." not in text, "no optional chaining (Chrome 80)"
    assert "http://" not in text.replace("http://www.w3.org", "")
    assert "https://" not in text.replace("https://www.w3.org", "")


def test_browser_page_wires_the_assets():
    path = os.path.join(BROWSER_DIR, "index.html")
    if not os.path.isfile(path):
        pytest.skip("index.html not present")
    with open(path, "r", encoding="utf-8") as fh:
        html = fh.read()
    assert "/w/style.css" in html
    assert "/w/worker.js" in html
    for element_id in ("codeInput", "joinBtn", "pauseBtn", "renameBtn", "leaveBtn",
                       "logView", "batteryValue", "taskValue", "wakeVideo"):
        assert 'id="%s"' % element_id in html, element_id
    worker_js = open(os.path.join(BROWSER_DIR, "worker.js"), encoding="utf-8").read()
    assert "'/w/runner.js'" in worker_js
    for endpoint in ("/api/w/register", "/api/w/poll", "/api/w/leave", "/api/w/bench",
                     "/api/w/files", "/done", "/log", "/start"):
        assert endpoint in worker_js, endpoint
    assert "['js', 'browser']" in worker_js
    assert "platform: 'browser'" in worker_js


def test_runner_sha256_matches_hashlib():
    """The benchmark hashes with a hand-rolled SHA-256 (crypto.subtle is not
    available over plain http), so verify it against hashlib via node."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    script = (
        "const r = require(%s);"
        "const vals = ['', 'abc', 'x'.repeat(55), 'y'.repeat(56), 'z'.repeat(1000)];"
        "const out = vals.map(function (s) {"
        "  return r.sha256Hex(new TextEncoder().encode(s));"
        "});"
        "console.log(JSON.stringify(out));"
    ) % json.dumps(os.path.join(BROWSER_DIR, "runner.js").replace("\\", "/"))
    proc = subprocess.run([node, "-e", script], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    got = json.loads(proc.stdout.decode("utf-8"))
    import hashlib
    expected = [hashlib.sha256(s.encode()).hexdigest()
                for s in ["", "abc", "x" * 55, "y" * 56, "z" * 1000]]
    assert got == expected
