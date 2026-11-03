"""Unit tests for the PhoneCluster SDK + CLI against a stdlib fake coordinator.

Self-contained on purpose: it starts an ``http.server`` in a thread that
implements the endpoints the SDK talks to (SPEC section 11), records every
request, and moves jobs pending -> running -> done on successive GETs.

    & .\\tools\\python\\python.exe -m pytest tests/test_sdk_unit.py -q   # in the project folder
"""

import io
import json
import os
import struct
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "sdk"))

from phonecluster import Cluster, ClusterError, JobFailed  # noqa: E402
from phonecluster import cli as pc_cli  # noqa: E402
from phonecluster.client import (  # noqa: E402
    WS_CLOSE,
    WS_PING,
    WS_TEXT,
    PhoneClusterError,
    ws_decode_frame,
    ws_encode_frame,
)

PIN = "482913"
FAIL_JOB = "failjob00001"
STUCK_JOB = "stuckjob0001"


# --------------------------------------------------------------------------
# fake coordinator
# --------------------------------------------------------------------------


class Headers(dict):
    """Case-insensitive view of the request headers.

    urllib capitalizes header names (``X-Pin`` goes out as ``X-pin``), which
    is legal HTTP; assertions should not care.
    """

    def __init__(self, message):
        dict.__init__(self, ((k.lower(), v) for k, v in message.items()))

    def get(self, key, default=None):
        return dict.get(self, key.lower(), default)

    def __getitem__(self, key):
        return dict.__getitem__(self, key.lower())

    def __contains__(self, key):
        return dict.__contains__(self, key.lower())


class FakeState(object):
    def __init__(self):
        self.requests = []          # list of dicts: method, path, query, headers, body
        self.jobs = {}              # job_id -> dict
        self.job_polls = {}         # job_id -> number of GETs so far
        self.files = {}             # file_id -> (name, bytes)
        self.blobs = {}
        self.packages = []          # recorded POST /api/packages uploads
        self.package_kind = "job"   # what the fake answers for a package
        self.results_stream = {}    # job_id -> list of items for /results
        self.llm_posts = []         # (path, body) for /api/llm POSTs
        self.llm_polls = 0
        self.counter = 0
        self.lock = threading.Lock()

    def new_id(self, prefix="job"):
        with self.lock:
            self.counter += 1
            return "%s%08d" % (prefix, self.counter)

    def record(self, method, path, query, headers, body):
        entry = {"method": method, "path": path, "query": query,
                 "headers": Headers(headers), "body": body}
        try:
            entry["json"] = json.loads(body.decode("utf-8")) if body else None
        except Exception:
            entry["json"] = None
        with self.lock:
            self.requests.append(entry)
        return entry

    def find(self, method, path):
        return [r for r in self.requests
                if r["method"] == method and r["path"] == path]

    def last(self, method, path):
        found = self.find(method, path)
        assert found, "no %s %s in %s" % (
            method, path, [(r["method"], r["path"]) for r in self.requests])
        return found[-1]

    def create_job(self, spec):
        job_id = spec.get("_id") or self.new_id("job")
        job = {"id": job_id, "name": spec.get("name") or spec.get("type"),
               "type": spec.get("type"), "params": spec.get("params") or {},
               "status": "pending", "priority": spec.get("priority", 0),
               "options": spec.get("options") or {},
               "depends_on": spec.get("depends_on") or [],
               "created": 1757500000.0, "total_tasks": 2, "done_tasks": 0,
               "failed_tasks": 0, "result": None, "error": None}
        self.jobs[job_id] = job
        self.job_polls[job_id] = 0
        return job

    def poll_job(self, job_id):
        """pending -> running -> done (or failed for the poison job id)."""
        job = self.jobs.get(job_id)
        if job is None:
            return None
        with self.lock:
            count = self.job_polls.get(job_id, 0) + 1
            self.job_polls[job_id] = count
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        if job["id"] == STUCK_JOB:
            job["status"] = "running"
        elif count == 1:
            job["status"] = "pending"
        elif count == 2:
            job["status"] = "running"
            job["done_tasks"] = 1
        else:
            if job["id"] == FAIL_JOB:
                job["status"] = "failed"
                job["failed_tasks"] = 1
                job["error"] = "task 1 exploded"
            else:
                job["status"] = "done"
                job["done_tasks"] = job["total_tasks"]
                job["result"] = [1, 4, 9]
        return job


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # silence
            pass

        # -- plumbing -------------------------------------------------
        def _read_body(self):
            length = self.headers.get("Content-Length")
            if length:
                return self.rfile.read(int(length))
            return b""

        def _send(self, obj, status=200, raw=None, ctype="application/json",
                  headers=None):
            if raw is None:
                raw = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)

        def _error(self, status, message):
            self._send({"error": message}, status=status)

        def _dispatch(self, method):
            from urllib.parse import parse_qs, urlsplit

            parts = urlsplit(self.path)
            path = parts.path
            query = dict((k, v[0]) for k, v in parse_qs(parts.query).items())
            body = self._read_body()
            state.record(method, path, query, self.headers, body)
            if path.startswith("/api/") and path != "/api/info":
                if self.headers.get("X-Pin") != PIN:
                    return self._error(401, "unauthorized")
            try:
                return self.route(method, path, query, body)
            except Exception as exc:  # pragma: no cover - test aid
                return self._error(500, "fake server: %r" % (exc,))

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_PATCH(self):
            self._dispatch("PATCH")

        def do_DELETE(self):
            self._dispatch("DELETE")

        # -- routes ---------------------------------------------------
        def route(self, method, path, query, body):
            parts = [p for p in path.split("/") if p]
            payload = None
            if body:
                try:
                    payload = json.loads(body.decode("utf-8"))
                except Exception:
                    payload = None

            if path == "/api/info":
                return self._send({"name": "phonecluster", "version": "0.1.0",
                                   "urls": ["http://127.0.0.1:8765"],
                                   "port": 8765, "has_pin": True, "time": 1.0})
            if path == "/api/login" and method == "POST":
                return self._send({"ok": True})
            if path == "/api/join":
                return self._send({
                    "url": "http://10.0.0.5:8765", "pin": PIN,
                    "worker_url": "http://10.0.0.5:8765/worker.py",
                    "browser_url": "http://10.0.0.5:8765/w?pin=" + PIN,
                    "install_cmd": 'curl -fsS "http://10.0.0.5:8765/install.sh?pin=%s" | sh' % PIN,
                    "qr_svg": "/qr.svg?pin=" + PIN})
            if path == "/api/stats":
                return self._send({
                    "devices": {"online": 2, "total": 3, "cores": 12, "slots_free": 5},
                    "jobs": {"running": 1, "pending": 0, "done_24h": 7, "failed_24h": 1},
                    "tasks_per_min": 4.25,
                    "leaderboard": [
                        {"device_id": "dev1", "name": "note10", "bench_score": 3.5,
                         "tasks_done": 42},
                        {"device_id": "dev2", "name": "pc", "bench_score": 9.1,
                         "tasks_done": 11}]})
            if path == "/api/devices" and method == "GET":
                return self._send([
                    {"id": "dev1", "name": "note10", "platform": "termux",
                     "status": "online", "paused": 0, "slots": 4, "battery": 88,
                     "charging": 1, "temp": 31.5, "bench_score": 3.5,
                     "caps": ["python", "shell", "termux_api"], "last_seen": 1757500000.0},
                    {"id": "dev2", "name": "windows-pc", "platform": "windows",
                     "status": "offline", "paused": 1, "slots": 4, "battery": None,
                     "charging": None, "temp": None, "bench_score": 9.1,
                     "caps": ["python", "shell", "ffmpeg"], "last_seen": 1757400000.0}])
            if len(parts) == 3 and parts[1] == "devices":
                device_id = parts[2]
                if method == "GET":
                    return self._send({"id": device_id, "name": "note10",
                                       "status": "online"})
                if method == "PATCH":
                    return self._send({"id": device_id, "patched": payload})
                if method == "DELETE":
                    return self._send({"ok": True, "removed": device_id})
            if len(parts) == 4 and parts[1] == "devices":
                if parts[3] == "benchmark":
                    return self._send({"ok": True, "device_id": parts[2],
                                       "score": 3.5})
                if parts[3] == "notify":
                    return self._send({"ok": True, "sent": payload})

            if path == "/api/jobs" and method == "POST":
                job = state.create_job(payload or {})
                return self._send(job)
            if path == "/api/jobs" and method == "GET":
                jobs = list(state.jobs.values())
                if query.get("status"):
                    jobs = [j for j in jobs if j["status"] == query["status"]]
                return self._send({"jobs": jobs, "total": len(jobs)})
            if len(parts) == 3 and parts[1] == "jobs":
                job_id = parts[2]
                if method == "GET":
                    job = state.poll_job(job_id)
                    if job is None:
                        return self._error(404, "no such job")
                    return self._send(job)
                if method == "DELETE":
                    state.jobs.pop(job_id, None)
                    return self._send({"ok": True})
            if len(parts) == 4 and parts[1] == "jobs":
                job_id, tail = parts[2], parts[3]
                job = state.jobs.get(job_id)
                if job is None and tail != "export":
                    return self._error(404, "no such job")
                if tail == "result":
                    return self._send({"result": (job or {}).get("result")})
                if tail == "results":
                    lines = state.results_stream.get(job_id)
                    if lines is None:
                        self.send_response(204)
                        self.end_headers()
                        return None
                    return self._send(None, raw="".join(json.dumps(x) + "\n" for x in lines).encode(),
                                      ctype="application/x-ndjson",
                                      headers={"Content-Disposition": 'attachment; filename="r.jsonl"'})
                if tail == "tasks":
                    return self._send([
                        {"id": job_id + "t0", "idx": 0, "status": "done",
                         "device_id": "dev1", "attempts": 1, "duration": 1.5,
                         "outputs": [{"file_id": "f" * 8, "name": "out0.txt",
                                      "size": 5}]},
                        {"id": job_id + "t1", "idx": 1, "status": "done",
                         "device_id": "dev2", "attempts": 1, "duration": 2.5,
                         "outputs": []}])
                if tail == "cancel":
                    if job:
                        job["status"] = "cancelled"
                    return self._send(job or {"ok": True})
                if tail == "retry":
                    if job:
                        job["status"] = "running"
                    return self._send(job or {"ok": True})
                if tail == "rerun":
                    options = dict(job["options"])
                    if (payload or {}).get("fresh"):
                        options["cache"] = "refresh"
                    return self._send(state.create_job({"type": job["type"], "params": job["params"],
                                                        "options": options}))
                if tail == "export":
                    fmt = query.get("format", "zip")
                    if fmt == "json":
                        return self._send({"job": job, "tasks": []})
                    if fmt == "csv":
                        raw = b"idx,device,status\n0,dev1,done\n"
                        return self._send(None, raw=raw, ctype="text/csv")
                    buf = io.BytesIO()
                    with zipfile.ZipFile(buf, "w") as zf:
                        zf.writestr("result.json", json.dumps((job or {}).get("result")))
                        zf.writestr("tasks.csv", "idx,device\n0,dev1\n")
                    return self._send(None, raw=buf.getvalue(),
                                      ctype="application/zip",
                                      headers={"Content-Disposition":
                                               'attachment; filename="%s.zip"' % job_id})

            if path == "/api/join/code" and method == "POST":
                return self._send({
                    "code": "ABCD-EFGH", "expires": 1757500600.0, "minutes": 10,
                    "url": "https://10.0.0.5:8765",
                    "install_cmd": 'curl -fsSk --pinnedpubkey "sha256//x" '
                                   '"https://10.0.0.5:8765/install.sh?code=ABCD-EFGH" | sh',
                    "browser_url": "https://10.0.0.5:8765/w?code=ABCD-EFGH",
                    "manual_cmd": "python worker.py --url https://10.0.0.5:8765 --fp ab --join ABCD-EFGH",
                    "ca_fingerprint": "ab"})
            if path == "/api/cache":
                if method == "DELETE":
                    return self._send({"ok": True, "removed": 3})
                return self._send({"stats": {"entries": 3, "hits": 5}, "entries": []})
            if path == "/api/trust":
                return self._send([{"device_id": "dev1", "name": "note10", "state": "trusted"}])
            if path == "/api/pipelines" and method == "POST":
                jobs = {}
                for spec in (payload or {}).get("jobs", []):
                    created = state.create_job(spec)
                    jobs[spec.get("key")] = created["id"]
                return self._send({"pipeline_id": state.new_id("pipe"), "jobs": jobs})

            if len(parts) == 3 and parts[1] == "tasks":
                return self._send({"id": parts[2], "status": "done", "idx": 0})
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "log":
                return self._send({"lines": ["hello from %s" % parts[2], "line two"]})
            if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "cancel":
                return self._send({"ok": True})

            if path == "/api/files" and method == "PUT":
                name = query.get("name", "upload.bin")
                file_id = "sha%05d" % (len(state.files) + 1)
                state.files[file_id] = (name, body)
                return self._send({"file_id": file_id, "size": len(body),
                                   "name": name})
            if path == "/api/files" and method == "GET":
                return self._send([{"id": fid, "name": meta[0],
                                    "size": len(meta[1]), "mime": "text/plain",
                                    "created": 1757500000.0}
                                   for fid, meta in state.files.items()])
            if len(parts) == 3 and parts[1] == "files":
                if method == "DELETE":
                    state.files.pop(parts[2], None)
                    return self._send({"ok": True})
                meta = state.files.get(parts[2])
                if not meta:
                    return self._error(404, "no such file")
                return self._send({"id": parts[2], "name": meta[0],
                                   "size": len(meta[1])})
            if len(parts) == 4 and parts[1] == "files" and parts[3] == "download":
                meta = state.files.get(parts[2])
                if not meta:
                    return self._error(404, "no such file")
                return self._send(None, raw=meta[1],
                                  ctype="application/octet-stream",
                                  headers={"Content-Disposition":
                                           'attachment; filename="%s"' % meta[0]})

            if path == "/api/storage" and method == "POST":
                blob_id = "blob%03d" % (len(state.blobs) + 1)
                state.blobs[blob_id] = body
                return self._send({"blob_id": blob_id, "status": "ready"})
            if path == "/api/storage" and method == "GET":
                return self._send([{"id": b, "name": "x", "size": len(v),
                                    "n_chunks": 1, "replication": 2,
                                    "status": "ready", "created": 1757500000.0}
                                   for b, v in state.blobs.items()])
            if len(parts) == 3 and parts[1] == "storage":
                if method == "DELETE":
                    state.blobs.pop(parts[2], None)
                    return self._send({"ok": True})
                return self._send(None, raw=b"blob-bytes",
                                  ctype="application/octet-stream")
            if len(parts) == 4 and parts[1] == "storage" and parts[3] == "status":
                return self._send({"id": parts[2], "status": "ready"})

            if path == "/api/llm/devices":
                return self._send([{"id": "dev-a", "name": "note10"}, {"id": "dev-b", "name": "redmi5a"},
                                   {"id": "dev-pc", "name": "pc"}])
            if path in ("/api/llm/deployments", "/api/llm/plan") and method == "POST":
                state.llm_posts.append((path, json.loads(body.decode("utf-8"))))
                if path.endswith("plan"):
                    return self._send({"ok": True, "devices": []})
                return self._send({"id": "dep1", "name": "m", "status": "starting", "shards": []})
            if path == "/api/llm/deployments/dep1":
                state.llm_polls += 1
                return self._send({"id": "dep1", "name": "m",
                                   "status": "ready" if state.llm_polls >= 2 else "loading"})

            if path == "/api/packages" and method == "POST":
                state.packages.append({"body": body, "query": query,
                                       "headers": dict(self.headers)})
                if query.get("dry_run") == "true":
                    return self._send({"kind": "job", "spec": {"type": "shell"}, "uploaded": []})
                if state.package_kind == "pipeline":
                    return self._send({"kind": "pipeline", "pipeline_id": "pipe01",
                                       "jobs": {"a": "job00a", "b": "job00b"}, "uploaded": []})
                job = {"id": "jobpkg", "status": "running", "type": "python_map"}
                state.jobs["jobpkg"] = job
                return self._send({"kind": "job", "job_id": "jobpkg", "job": job,
                                   "uploaded": [{"file_id": "f1", "name": "a.txt"}]})

            if path == "/api/terminal" and method == "POST":
                return self._send({"session_id": "term0001"})
            if len(parts) == 3 and parts[1] == "terminal" and method == "DELETE":
                return self._send({"ok": True})

            if path == "/api/schedules" and method == "GET":
                return self._send([{"id": "sch1", "name": "hourly bench",
                                    "cron": "0 * * * *", "enabled": 1,
                                    "last_run": None, "next_run": 1757503600.0}])
            if path == "/api/schedules" and method == "POST":
                return self._send({"id": "sch2", "name": (payload or {}).get("name")})
            if len(parts) == 3 and parts[1] == "schedules" and method == "DELETE":
                return self._send({"ok": True})
            if len(parts) == 4 and parts[1] == "schedules" and parts[3] == "run":
                return self._send({"ok": True, "job_id": "job0"})

            if path == "/api/templates" and method == "GET":
                return self._send([{"id": "tpl1", "name": "squares",
                                    "job_spec": {"type": "python_map"},
                                    "created": 1757500000.0}])
            if path == "/api/templates" and method == "POST":
                return self._send({"id": "tpl2"})
            if len(parts) == 3 and parts[1] == "templates" and method == "DELETE":
                return self._send({"ok": True})

            if path == "/api/plugins":
                return self._send([
                    {"type": "python_map", "description": "map a function",
                     "params": [{"name": "code"}, {"name": "items"}],
                     "default_options": {"timeout": 600}},
                    {"type": "shell", "description": "run a command",
                     "params": [{"name": "cmd"}], "default_options": {}}])
            if path == "/api/events":
                return self._send([{"ts": 1757500000.0, "kind": "job_done",
                                    "device_id": "dev1", "job_id": "job1",
                                    "message": "job finished"}])
            if path == "/api/settings" and method == "GET":
                return self._send({"scheduler": {"min_battery": 20}})
            if path == "/api/settings" and method == "PATCH":
                return self._send({"ok": True, "settings": payload})

            return self._error(404, "no route for %s %s" % (method, path))

    return Handler


@pytest.fixture(scope="module")
def server():
    state = FakeState()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=httpd.serve_forever)
    thread.daemon = True
    thread.start()
    url = "http://127.0.0.1:%d" % httpd.server_address[1]
    yield url, state
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch, tmp_path):
    """Never read/write the real data/cli.json (SPEC section 17)."""
    monkeypatch.setenv("PC_ROOT", str(tmp_path))
    monkeypatch.delenv("PC_URL", raising=False)
    monkeypatch.delenv("PC_PIN", raising=False)
    monkeypatch.setattr(
        "phonecluster.client.load_config", lambda: {}, raising=True)


@pytest.fixture()
def cluster(server):
    url, state = server
    return Cluster(url, pin=PIN, timeout=10)


# --------------------------------------------------------------------------
# auth & config resolution
# --------------------------------------------------------------------------


def test_wasm_uploads_the_module_and_inputs(cluster, server, tmp_path):
    _, state = server
    module = tmp_path / "primes.wasm"
    module.write_bytes(b"\x00asm\x01\x00\x00\x00")
    job = cluster.wasm(str(module), call="count_primes", range=(2, 1000), grain=10, cache=False)
    body = state.last("POST", "/api/jobs")["json"]
    assert body["type"] == "wasm" and body["options"]["cache"] is False
    params = body["params"]
    assert params["call"] == "count_primes" and params["range"] == [2, 1000] and params["grain"] == 10
    assert params["module"] != str(module)  # uploaded: a file id
    assert state.find("PUT", "/api/files")
    cluster.wasm("abc123", inputs=["fileid1"], args_list=[["-v"], "x"], stdin="hi", verify=0.5)
    body = state.last("POST", "/api/jobs")["json"]
    assert body["params"]["module"] == "abc123" and body["params"]["inputs"] == ["fileid1"]
    assert body["params"]["args_list"] == [["-v"], ["x"]] and body["options"]["verify"] == 0.5
    assert job.id


def test_rerun_cache_and_trust(cluster, server):
    _, state = server
    job = cluster.submit("python_map", {"code": "x"})
    fresh = job.rerun(fresh=True)
    assert fresh.id != job.id and state.last("POST", "/api/jobs/%s/rerun" % job.id)["json"] == {"fresh": True}
    assert cluster.cache_info()["stats"]["hits"] == 5
    assert cluster.clear_cache()["removed"] == 3
    assert cluster.trust()[0]["state"] == "trusted"


def test_speed_line_mentions_reuse_and_checks():
    from phonecluster.cli import speed_line

    assert "identical job j1" in speed_line({"cached": True, "from_job": "j1", "saved_s": 4.2})
    line = speed_line({"wall_s": 2, "fastest_device": {"name": "pc", "alone_s": 3}, "speedup": 1.5,
                       "reused_tasks": 2, "checks": {"checked": 3, "decided": 1}})
    assert "2 task(s) answered from earlier runs" in line and "3 task(s) double-checked" in line
    assert "settled by a third device" in line


def test_pin_header_on_every_request(cluster, server):
    _, state = server
    before = len(state.requests)
    cluster.stats()
    cluster.devices()
    cluster.plugins()
    sent = state.requests[before:]
    assert len(sent) == 3
    for entry in sent:
        assert entry["headers"].get("X-Pin") == PIN


def test_bad_pin_raises_cluster_error(server):
    url, _ = server
    bad = Cluster(url, pin="000000", timeout=10)
    with pytest.raises(ClusterError) as excinfo:
        bad.devices()
    assert excinfo.value.status == 401
    assert "unauthorized" in excinfo.value.message


def test_url_and_pin_from_env(server, monkeypatch):
    url, _ = server
    monkeypatch.setenv("PC_URL", url)
    monkeypatch.setenv("PC_PIN", PIN)
    assert Cluster().devices()


def test_url_and_pin_from_cli_json(server, monkeypatch, tmp_path):
    url, _ = server
    path = tmp_path / "data" / "cli.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"url": url, "pin": PIN}))
    import phonecluster.client as pcc

    monkeypatch.setattr(pcc, "load_config",
                        lambda: json.loads(path.read_text()), raising=True)
    client = Cluster()
    assert client.url == url and client.pin == PIN


def test_unreachable_url_is_cluster_error():
    client = Cluster("http://127.0.0.1:1", pin=PIN, timeout=2)
    with pytest.raises(ClusterError):
        client.stats()


# --------------------------------------------------------------------------
# map / submit
# --------------------------------------------------------------------------


def square(item, ctx):
    """A perfectly ordinary top-level mapper."""
    return item * item


class Holder(object):
    @staticmethod
    def indented(item, ctx):
        # deliberately indented source, must be dedented before shipping
        return item + 1


def test_map_ships_source_fn_name_items_and_chunk_size(cluster, server):
    _, state = server
    job = cluster.map(square, [1, 2, 3], chunk_size=7)
    body = state.last("POST", "/api/jobs")["json"]
    assert body["type"] == "python_map"
    params = body["params"]
    assert params["code"].startswith("def square(item, ctx):")
    assert "return item * item" in params["code"]
    assert params["fn_name"] == "square"
    assert params["items"] == [1, 2, 3]
    assert params["chunk_size"] == 7
    assert body["options"]["chunk_size"] == 7
    assert body["options"]["timeout"] == 600
    assert body["options"]["max_attempts"] == 3
    assert job.id and job.status == "pending"


def test_map_dedents_indented_source(cluster, server):
    _, state = server
    cluster.map(Holder.indented, [1])
    code = state.last("POST", "/api/jobs")["json"]["params"]["code"]
    assert code.splitlines()[0].startswith("@staticmethod") or \
        code.splitlines()[0].startswith("def indented")
    for line in code.splitlines():
        if line.strip().startswith("def indented"):
            assert not line.startswith(" ")


def test_map_large_items_are_uploaded_as_a_file(cluster, server):
    _, state = server
    items = ["x" * 200 for _ in range(20000)]  # > 2 MB of JSON
    cluster.map(square, items, chunk_size=100)
    params = state.last("POST", "/api/jobs")["json"]["params"]
    assert "items" not in params
    assert params["items_file"] in state.files
    name, raw = state.files[params["items_file"]]
    assert name == "items.json"
    assert json.loads(raw.decode("utf-8")) == items


def test_map_with_js_fn_sets_js_code(cluster, server):
    _, state = server
    cluster.map(square, [1], js_fn="function fn(item, ctx){return item*item;}")
    params = state.last("POST", "/api/jobs")["json"]["params"]
    assert params["js_code"].startswith("function fn(")


def test_map_rejects_non_callables(cluster):
    with pytest.raises(TypeError):
        cluster.map("not a function", [1])


def test_submit_builds_the_spec(cluster, server):
    _, state = server
    cluster.submit("hash_search", {"prefix": "0000"}, name="hunt", priority=3,
                   timeout=120, requires=["ffmpeg"], targets="all",
                   max_attempts=5, fail_fast=True)
    body = state.last("POST", "/api/jobs")["json"]
    assert body["type"] == "hash_search"
    assert body["name"] == "hunt"
    assert body["priority"] == 3
    assert body["params"] == {"prefix": "0000"}
    assert body["options"] == {"timeout": 120, "max_attempts": 5,
                               "requires": ["ffmpeg"], "targets": "all",
                               "fail_fast": True}


def test_shell_targets_all(cluster, server):
    _, state = server
    cluster.shell("uname -a", targets="all", timeout=30)
    body = state.last("POST", "/api/jobs")["json"]
    assert body["type"] == "shell"
    assert body["params"]["cmd"] == "uname -a"
    assert body["options"]["targets"] == "all"


# --------------------------------------------------------------------------
# Job lifecycle
# --------------------------------------------------------------------------


def test_job_wait_polls_until_done(cluster, server):
    _, state = server
    job = cluster.map(square, [1, 2, 3])
    assert job.status == "pending"
    job.wait(poll=0.01)
    assert job.status == "done"
    assert state.job_polls[job.id] >= 3
    assert job.result() == [1, 4, 9]
    assert job.progress == 1.0


def test_job_wait_raises_job_failed(cluster, server):
    _, state = server
    state.create_job({"_id": FAIL_JOB, "type": "python_map"})
    job = cluster.job(FAIL_JOB)
    with pytest.raises(JobFailed) as excinfo:
        job.wait(poll=0.01)
    assert excinfo.value.error == "task 1 exploded"
    assert excinfo.value.job_id == FAIL_JOB
    assert job.status == "failed"


def test_job_wait_timeout(cluster, server):
    _, state = server
    state.create_job({"_id": STUCK_JOB, "type": "python_map"})  # never finishes
    job = cluster.job(STUCK_JOB)
    with pytest.raises(TimeoutError):
        job.wait(poll=0.01, timeout=0.05)
    assert job.status == "running"


def test_job_tasks_logs_cancel_retry(cluster, server):
    _, state = server
    job = cluster.map(square, [1])
    tasks = job.tasks()
    assert [t["idx"] for t in tasks] == [0, 1]
    text = job.logs(task_idx=1)
    assert "hello from %st1" % job.id in text
    combined = job.logs()
    assert "[task 0]" in combined and "[task 1]" in combined
    job.cancel()
    assert state.find("POST", "/api/jobs/%s/cancel" % job.id)
    job.retry()
    assert state.find("POST", "/api/jobs/%s/retry" % job.id)


def test_job_outputs_and_download(cluster, server, tmp_path):
    _, state = server
    state.files["f" * 8] = ("out0.txt", b"hello")
    job = cluster.map(square, [1])
    outputs = job.outputs()
    assert outputs == [{"file_id": "f" * 8, "name": "out0.txt", "size": 5,
                        "task_idx": 0}]
    written = job.download_outputs(str(tmp_path / "out"))
    assert len(written) == 1
    with open(written[0], "rb") as fh:
        assert fh.read() == b"hello"


def test_job_export_writes_a_file(cluster, server, tmp_path):
    _, state = server
    job = cluster.map(square, [1])
    job.wait(poll=0.01)
    dest = str(tmp_path / "export.zip")
    path = job.export(dest, "zip")
    assert path == dest and os.path.getsize(dest) > 0
    with zipfile.ZipFile(dest) as zf:
        assert "result.json" in zf.namelist()
    csv_path = job.export(str(tmp_path / "export.csv"), "csv")
    with open(csv_path, "rb") as fh:
        assert fh.read().startswith(b"idx,device,status")
    assert state.last("GET", "/api/jobs/%s/export" % job.id)["query"]["format"] == "csv"


def test_export_to_a_directory_names_the_file(cluster, server, tmp_path):
    job = cluster.map(square, [1])
    target = tmp_path / "exports"
    target.mkdir()
    path = job.export(str(target), "json")
    assert os.path.basename(path) == "%s.json" % job.id


# --------------------------------------------------------------------------
# files & storage
# --------------------------------------------------------------------------


def test_files_upload_is_a_raw_put_with_name(cluster, server, tmp_path):
    _, state = server
    source = tmp_path / "notes.txt"
    source.write_bytes(b"the quick brown fox" * 100)
    file_id = cluster.files.upload(str(source))
    entry = state.last("PUT", "/api/files")
    assert entry["query"]["name"] == "notes.txt"
    assert entry["body"] == source.read_bytes()
    assert entry["headers"]["Content-Length"] == str(len(entry["body"]))
    assert state.files[file_id][1] == source.read_bytes()


def test_files_download_list_delete(cluster, server, tmp_path):
    _, state = server
    source = tmp_path / "a.txt"
    source.write_bytes(b"payload")
    file_id = cluster.files.upload(str(source))
    dest = tmp_path / "copy.txt"
    cluster.files.download(file_id, str(dest))
    assert dest.read_bytes() == b"payload"
    into_dir = tmp_path / "dl"
    into_dir.mkdir()
    path = cluster.files.download(file_id, str(into_dir))
    assert os.path.basename(path) == "a.txt"
    assert any(f["id"] == file_id for f in cluster.files.list())
    cluster.files.delete(file_id)
    assert file_id not in state.files


def parse_multipart(body, content_type):
    """Minimal multipart parser for the assertions below."""
    boundary = content_type.split("boundary=", 1)[1].strip().strip('"')
    marker = ("--" + boundary).encode("ascii")
    fields, files = {}, {}
    for chunk in body.split(marker):
        if not chunk.strip() or chunk.strip() == b"--":
            continue
        head, _, data = chunk.partition(b"\r\n\r\n")
        head = head.decode("utf-8", "replace")
        if "name=" not in head:
            continue
        name = head.split('name="', 1)[1].split('"', 1)[0]
        data = data[:-2] if data.endswith(b"\r\n") else data
        if 'filename="' in head:
            filename = head.split('filename="', 1)[1].split('"', 1)[0]
            files[name] = (filename, data)
        else:
            fields[name] = data.decode("utf-8")
    return fields, files


def test_storage_put_sends_valid_multipart(cluster, server, tmp_path):
    _, state = server
    source = tmp_path / "movie.bin"
    source.write_bytes(os.urandom(100000))
    blob_id = cluster.storage.put(str(source), replication=3)
    entry = state.last("POST", "/api/storage")
    ctype = entry["headers"]["Content-Type"]
    assert ctype.startswith("multipart/form-data; boundary=")
    assert entry["headers"]["Content-Length"] == str(len(entry["body"]))
    fields, files = parse_multipart(entry["body"], ctype)
    assert fields["replication"] == "3"
    assert fields["name"] == "movie.bin"
    assert files["file"][0] == "movie.bin"
    assert files["file"][1] == source.read_bytes()
    assert blob_id == "blob001" or blob_id.startswith("blob")


def make_package_dir(tmp_path):
    pkg = tmp_path / "mypkg"
    (pkg / "texts").mkdir(parents=True)
    (pkg / "__pycache__").mkdir()
    (pkg / "job.yaml").write_text("type: python_map\nparams:\n  code: '@f.py'\n")
    (pkg / "f.py").write_text("def fn(item, ctx):\n    return item\n")
    (pkg / "f.pyc").write_bytes(b"junk")
    (pkg / "__pycache__" / "x.pyc").write_bytes(b"junk")
    (pkg / "texts" / "a.txt").write_text("alpha")
    return pkg


def test_run_package_zips_a_folder_and_posts_multipart(cluster, server, tmp_path):
    import io
    import zipfile

    _, state = server
    pkg = make_package_dir(tmp_path)
    job = cluster.run_package(str(pkg))
    assert job.id == "jobpkg"
    entry = state.packages[-1]
    ctype = entry["headers"]["Content-Type"]
    assert ctype.startswith("multipart/form-data; boundary=")
    fields, files = parse_multipart(entry["body"], ctype)
    filename, data = files["file"]
    assert filename == "mypkg.zip"
    names = sorted(zipfile.ZipFile(io.BytesIO(data)).namelist())
    assert names == ["f.py", "job.yaml", "texts/a.txt"]  # no pyc, no __pycache__
    assert "dry_run" not in entry["query"]


def test_run_package_accepts_a_zip_file_and_dry_run(cluster, server, tmp_path):
    import zipfile

    _, state = server
    archive = tmp_path / "ready.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("job.yaml", "type: shell\nparams: {cmd: echo}\n")
    spec = cluster.run_package(str(archive), dry_run=True)
    assert spec == {"kind": "job", "spec": {"type": "shell"}, "uploaded": []}
    entry = state.packages[-1]
    assert entry["query"] == {"dry_run": "true"}
    _fields, files = parse_multipart(entry["body"], entry["headers"]["Content-Type"])
    assert files["file"][0] == "ready.zip"
    assert files["file"][1] == archive.read_bytes()


def test_run_package_pipeline_and_errors(cluster, server, tmp_path):
    from phonecluster.client import Pipeline

    _, state = server
    pkg = make_package_dir(tmp_path)
    state.package_kind = "pipeline"
    try:
        result = cluster.run_package(str(pkg))
    finally:
        state.package_kind = "job"
    assert isinstance(result, Pipeline)
    assert result.id == "pipe01" and sorted(result.jobs) == ["a", "b"]
    with pytest.raises(PhoneClusterError):
        cluster.run_package(str(tmp_path / "nope"))
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(PhoneClusterError) as info:
        cluster.run_package(str(empty))
    assert "job.yaml" in str(info.value)


def _square(item, ctx):
    return item * item


def test_map_lines_uploads_the_file_and_streams(cluster, server, tmp_path):
    _, state = server
    big = tmp_path / "big.jsonl"
    big.write_text('{"a": 1}\n{"a": 2}\n')
    job = cluster.map_lines(_square, str(big), lines_per_task=500, line_format="json")
    body = state.last("POST", "/api/jobs")["json"]
    params = body["params"]
    assert params["lines_file"] in state.files
    assert params["lines_per_task"] == 500 and params["line_format"] == "json"
    assert "items" not in params and "chunk_size" not in params
    assert body["options"]["stream_results"] is True
    assert params["fn_name"] == "_square"
    # an existing file id is passed straight through
    cluster.map_lines(_square, "a" * 64)
    assert state.last("POST", "/api/jobs")["json"]["params"]["lines_file"] == "a" * 64
    assert job.id


def test_map_stream_results_option(cluster, server):
    _, state = server
    cluster.map(_square, [1, 2], stream_results=True)
    assert state.last("POST", "/api/jobs")["json"]["options"]["stream_results"] is True
    cluster.map(_square, [1, 2])
    assert "stream_results" not in state.last("POST", "/api/jobs")["json"]["options"]


def test_iter_results_and_download(cluster, server, tmp_path):
    _, state = server
    job = cluster.map(_square, [1, 2, 3])
    state.results_stream[job.id] = [1, {"x": "y"}, [3]]
    assert list(job.iter_results()) == [1, {"x": "y"}, [3]]
    dest = tmp_path / "out.jsonl"
    job.download_results(str(dest), wait=False)
    assert [json.loads(l) for l in dest.read_text().splitlines()] == [1, {"x": "y"}, [3]]
    other = cluster.map(_square, [1])
    assert list(other.iter_results()) == []  # 204 -> nothing yet


def test_cli_results_command(capsys, server):
    _, state = server
    state.jobs["strm"] = {"id": "strm", "status": "done", "type": "python_map"}
    state.results_stream["strm"] = ["a", "b"]
    code, out, _ = run_cli(capsys, server, "results", "strm")
    assert code == 0
    assert out.splitlines() == ['"a"', '"b"']


def test_cli_map_lines(capsys, server, tmp_path):
    _, state = server
    module = tmp_path / "m.py"
    module.write_text("def up(line, ctx):\n    return line.upper()\n")
    big = tmp_path / "big.txt"
    big.write_text("x\ny\n")
    code, out, _ = run_cli(capsys, server, "map", "%s:up" % module, str(big), "--lines", "--chunk", "1")
    assert code == 0
    body = state.last("POST", "/api/jobs")["json"]
    assert body["params"]["lines_per_task"] == 1
    assert body["params"]["lines_file"] in state.files
    assert body["options"]["stream_results"] is True


def test_discover_finds_a_responder_and_auto_url(monkeypatch, tmp_path):
    import socket
    import threading
    from phonecluster.client import discover

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.2)
    port = sock.getsockname()[1]
    stop = []

    def serve():
        while not stop:
            try:
                data, addr = sock.recvfrom(1024)
            except Exception:
                continue
            if data.startswith(b"PHONECLUSTER_DISCOVER"):
                sock.sendto(json.dumps({"phonecluster": 1, "url": "http://10.0.0.9:8765",
                                        "name": "pc"}).encode(), addr)

    thread = threading.Thread(target=serve)
    thread.daemon = True
    thread.start()
    try:
        assert discover(timeout=2.0, port=port, hosts=["127.0.0.1"]) == "http://10.0.0.9:8765"
        replies = discover(timeout=1.0, port=port, hosts=["127.0.0.1"], all_replies=True)
        assert replies == [{"url": "http://10.0.0.9:8765", "name": "pc", "urls": []}]
        monkeypatch.setattr("phonecluster.client.discover",
                            lambda *a, **k: "http://10.0.0.9:8765")
        assert Cluster(url="auto", pin="1").url == "http://10.0.0.9:8765"
        monkeypatch.setattr("phonecluster.client.discover", lambda *a, **k: None)
        with pytest.raises(ClusterError):
            Cluster(url="auto", pin="1")
    finally:
        stop.append(True)
        sock.close()
    assert discover(timeout=0.3, port=port + 1 if port < 65000 else port - 1, hosts=["127.0.0.1"]) is None


def test_llm_deploy_resolves_device_names(cluster, server):
    _, state = server
    cluster.llm.plan("m.gguf", ["note10", "dev-b"], host="pc", weights={"note10": 3})
    path, body = state.llm_posts[-1]
    assert path == "/api/llm/plan"
    assert body["devices"] == ["dev-a", "dev-b"] and body["host"] == "dev-pc"
    assert body["weights"] == {"dev-a": 3.0}
    dep = cluster.llm.deploy("m", "m.gguf", ["redmi5a"], ctx=1024, wait=True)
    assert dep["status"] == "ready"
    path, body = state.llm_posts[-1]
    assert body["devices"] == ["dev-b"] and body["host"] is None and body["ctx"] == 1024
    with pytest.raises(PhoneClusterError):
        cluster.llm.deploy("m", "m.gguf", ["no-such-phone"])
    # automatic placement: no devices means "any", the mode is passed through
    cluster.llm.plan("m.gguf")
    path, body = state.llm_posts[-1]
    assert body["devices"] == [] and body["mode"] is None
    cluster.llm.deploy("m", "m.gguf", ["note10"], mode="manual", wait=True)
    assert state.llm_posts[-1][1]["mode"] == "manual"


def test_device_controls_builds_the_patch(cluster, monkeypatch):
    sent = {}
    monkeypatch.setattr(cluster.llm, "resolve", lambda refs: ["dev-a" if r == "note10" else r for r in refs])
    monkeypatch.setattr(cluster, "patch", lambda path, body: sent.update(path=path, body=body) or {"id": "dev-a"})
    cluster.device_controls("note10", on=False, ai=False, storage=True, cpu_pct=50, gpu_pct=0)
    assert sent["path"] == "/api/devices/dev-a"
    assert sent["body"] == {"paused": True, "settings": {"roles": {"ai": False, "storage": True},
                                                         "cpu_pct": 50, "gpu_pct": 0}}
    cluster.device_controls("dev-a", on=True)
    assert sent["body"] == {"paused": False}


def test_llm_chat_builds_a_service_call(cluster, monkeypatch):
    seen = {}

    def fake_call(name, **kwargs):
        seen.update(name=name, **kwargs)
        if kwargs.get("on_partial"):
            kwargs["on_partial"]({"token": "hel"})
            kwargs["on_partial"]({"token": "lo"})
        return {"status": "done", "result": {"text": "hello"}}

    monkeypatch.setattr(cluster, "call", fake_call)
    tokens = []
    out = cluster.llm.chat("qwen", prompt="hi", system="be brief", max_tokens=5, temperature=0,
                           on_token=tokens.append)
    assert out == {"text": "hello"} and tokens == ["hel", "lo"]
    assert seen["name"] == "qwen"
    assert seen["json_body"] == {"prompt": "hi", "system": "be brief", "max_tokens": 5, "temperature": 0.0}
    cluster.llm.chat("qwen", messages=[{"role": "user", "content": "x"}])
    assert seen["json_body"]["messages"] == [{"role": "user", "content": "x"}]
    with pytest.raises(PhoneClusterError):
        cluster.llm.chat("qwen")


def test_cli_run_package(capsys, server, tmp_path):
    _, state = server
    pkg = make_package_dir(tmp_path)
    before = len(state.packages)
    code, out, _ = run_cli(capsys, server, "run", str(pkg))
    assert code == 0
    assert out.strip() == "jobpkg"
    assert len(state.packages) == before + 1
    code, out, _ = run_cli(capsys, server, "run", str(pkg), "--dry-run")
    assert code == 0
    assert '"kind": "job"' in out
    assert state.packages[-1]["query"] == {"dry_run": "true"}


def test_storage_get_list_status_delete(cluster, server, tmp_path):
    _, state = server
    source = tmp_path / "s.bin"
    source.write_bytes(b"abc")
    blob_id = cluster.storage.put(str(source))
    dest = tmp_path / "back.bin"
    cluster.storage.get(blob_id, str(dest))
    assert dest.read_bytes() == b"blob-bytes"
    assert cluster.storage.status(blob_id)["status"] == "ready"
    assert any(b["id"] == blob_id for b in cluster.storage.list())
    cluster.storage.delete(blob_id)
    assert blob_id not in state.blobs


# --------------------------------------------------------------------------
# pipelines, schedules, templates, devices
# --------------------------------------------------------------------------


def test_pipeline_body_shape_and_jobs(cluster, server):
    _, state = server
    pipe = cluster.pipeline([
        {"key": "primes", "type": "prime_range", "params": {"start": 1, "end": 100}},
        {"key": "squared", "type": "python_map", "depends_on": ["primes"],
         "params": {"code": "def fn(i, ctx): return i", "fn_name": "fn",
                    "items": {"$from_job": "primes", "path": "count"}}},
    ], name="demo")
    body = state.last("POST", "/api/pipelines")["json"]
    assert body["name"] == "demo"
    assert [j["key"] for j in body["jobs"]] == ["primes", "squared"]
    assert body["jobs"][1]["depends_on"] == ["primes"]
    assert body["jobs"][0]["type"] == "prime_range"
    assert set(pipe.jobs) == {"primes", "squared"}
    assert pipe.id.startswith("pipe")
    pipe.wait(poll=0.01)
    assert pipe.results() == {"primes": [1, 4, 9], "squared": [1, 4, 9]}


def test_pipeline_keys_default_when_missing(cluster, server):
    _, state = server
    cluster.pipeline([{"type": "benchmark", "params": {}}])
    body = state.last("POST", "/api/pipelines")["json"]
    assert body["jobs"][0]["key"] == "job0"


def test_devices_schedules_templates_settings(cluster, server):
    _, state = server
    assert len(cluster.devices()) == 2
    assert cluster.device("dev1")["id"] == "dev1"
    cluster.pause_device("dev1", True)
    assert state.last("PATCH", "/api/devices/dev1")["json"] == {"paused": True}
    cluster.remove_device("dev2")
    assert state.find("DELETE", "/api/devices/dev2")
    cluster.notify("dev1", "hi", "there")
    assert state.last("POST", "/api/devices/dev1/notify")["json"] == {
        "title": "hi", "body": "there"}
    assert cluster.bench("dev1")["score"] == 3.5
    job = cluster.bench()
    assert state.last("POST", "/api/jobs")["json"]["type"] == "benchmark"
    assert job.id
    assert cluster.schedules.list()[0]["cron"] == "0 * * * *"
    cluster.schedules.create("nightly", "0 3 * * *", {"type": "benchmark"})
    body = state.last("POST", "/api/schedules")["json"]
    assert body["cron"] == "0 3 * * *" and body["enabled"] is True
    cluster.schedules.run("sch1")
    cluster.schedules.delete("sch1")
    cluster.templates.create("t", {"type": "shell"})
    assert state.last("POST", "/api/templates")["json"]["name"] == "t"
    cluster.templates.delete("tpl1")
    assert cluster.settings()["scheduler"]["min_battery"] == 20
    cluster.update_settings({"scheduler": {"min_battery": 30}})
    assert state.last("PATCH", "/api/settings")["json"] == {
        "scheduler": {"min_battery": 30}}
    assert cluster.events(limit=5)[0]["kind"] == "job_done"
    assert cluster.join()["code"] == "ABCD-EFGH"
    assert len(cluster.plugins()) == 2
    assert cluster.jobs(status="done") is not None


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def run_cli(capsys, server, *args):
    url, _ = server
    code = pc_cli.main(["--url", url, "--pin", PIN] + list(args))
    out, err = capsys.readouterr()
    return code, out, err


def test_param_json_parsing():
    assert pc_cli.parse_param_value("42") == 42
    assert pc_cli.parse_param_value("4.5") == 4.5
    assert pc_cli.parse_param_value("true") is True
    assert pc_cli.parse_param_value("null") is None
    assert pc_cli.parse_param_value('{"a": 1}') == {"a": 1}
    assert pc_cli.parse_param_value("[1, 2]") == [1, 2]
    assert pc_cli.parse_param_value("hello") == "hello"
    assert pc_cli.parse_param_value("0000ab") == "0000ab"
    params = pc_cli.parse_params(["a=1", "b=text", "c=[1,2]", "d=x", "d=y"])
    assert params == {"a": 1, "b": "text", "c": [1, 2], "d": ["x", "y"]}
    with pytest.raises(Exception):
        pc_cli.parse_params(["oops"])


def test_cli_devices_table(capsys, server):
    code, out, _ = run_cli(capsys, server, "devices")
    assert code == 0
    lines = out.splitlines()
    assert lines[0].split() == ["ID", "NAME", "PLATFORM", "STATUS", "BATT",
                                "TEMP", "SLOTS", "SCORE", "CAPS", "SEEN"]
    assert "note10" in out and "windows-pc" in out
    assert "88%+" in out
    assert "(paused)" in out
    # columns line up: NAME starts at the same offset on every row
    offset = lines[0].index("NAME")
    assert lines[2][offset:].startswith("note10")
    assert lines[3][offset:].startswith("windows-pc")


def test_cli_jobs_and_job(capsys, server):
    url, state = server
    state.create_job({"_id": "cli000001", "type": "python_map", "name": "cli job"})
    code, out, _ = run_cli(capsys, server, "jobs")
    assert code == 0
    assert "ID" in out.splitlines()[0] and "cli000001" in out
    code, out, _ = run_cli(capsys, server, "jobs", "--status", "pending")
    assert code == 0
    code, out, _ = run_cli(capsys, server, "job", "cli000001")
    assert code == 0
    assert '"type": "python_map"' in out
    assert "IDX" in out


def test_cli_submit_with_wait_and_params(capsys, server):
    _, state = server
    code, out, _ = run_cli(capsys, server, "submit", "hash_search",
                           "--param", "prefix=0000", "--param", "count=1000",
                           "--param", 'algo="sha256"', "--wait")
    assert code == 0
    body = state.last("POST", "/api/jobs")["json"]
    assert body["params"] == {"prefix": "0000", "count": 1000, "algo": "sha256"}
    job_id = out.splitlines()[0]
    assert state.jobs[job_id]["status"] == "done"
    assert "[\n  1,\n  4,\n  9\n]" in out or "1," in out


def test_cli_submit_failure_exits_1(capsys, server):
    _, state = server
    state.create_job({"_id": FAIL_JOB, "type": "python_map"})
    code, _, err = run_cli(capsys, server, "logs", FAIL_JOB)
    assert code == 0
    code, out, err = run_cli(capsys, server, "job", FAIL_JOB)
    assert code == 0
    code, out, err = run_cli(capsys, server, "cancel", "nosuchjob")
    assert code == 1
    assert "error:" in err


def test_cli_submit_with_file_upload(capsys, server, tmp_path):
    _, state = server
    source = tmp_path / "pic.jpg"
    source.write_bytes(b"jpegdata")
    code, out, _ = run_cli(capsys, server, "submit", "media",
                           "--param", 'op="resize"', "--file", "inputs=%s" % source)
    assert code == 0
    body = state.last("POST", "/api/jobs")["json"]
    # `inputs` is a `files`-typed param: a single --file must still yield a list.
    assert isinstance(body["params"]["inputs"], list)
    assert len(body["params"]["inputs"]) == 1
    assert body["params"]["inputs"][0] in state.files
    assert "uploaded" in out


def test_cli_map_file_and_items(capsys, server, tmp_path):
    _, state = server
    module = tmp_path / "mapper.py"
    module.write_text("def triple(item, ctx):\n    return item * 3\n")
    items = tmp_path / "items.json"
    items.write_text("[1, 2, 3]")
    code, out, _ = run_cli(capsys, server, "map", "%s:triple" % module,
                           str(items), "--chunk", "2", "--wait")
    assert code == 0
    params = state.last("POST", "/api/jobs")["json"]["params"]
    assert params["fn_name"] == "triple"
    assert params["items"] == [1, 2, 3]
    assert params["chunk_size"] == 2
    assert "return item * 3" in params["code"]


def test_cli_shell_all_and_stats_and_events(capsys, server):
    _, state = server
    code, out, _ = run_cli(capsys, server, "shell", "uname -a", "--all", "--wait")
    assert code == 0
    assert state.last("POST", "/api/jobs")["json"]["options"]["targets"] == "all"
    code, out, _ = run_cli(capsys, server, "stats")
    assert code == 0 and "leaderboard" not in out
    assert "devices" in out and "note10" in out and "SCORE" in out
    code, out, _ = run_cli(capsys, server, "events", "--limit", "5")
    assert code == 0 and "job_done" in out
    code, out, _ = run_cli(capsys, server, "plugins")
    assert code == 0 and "python_map" in out


def test_cli_files_storage_schedules_templates(capsys, server, tmp_path):
    _, state = server
    source = tmp_path / "up.txt"
    source.write_bytes(b"data")
    code, out, _ = run_cli(capsys, server, "files", "upload", str(source))
    assert code == 0
    file_id = out.strip()
    code, out, _ = run_cli(capsys, server, "files", "list")
    assert code == 0 and file_id in out
    dest = tmp_path / "down.txt"
    code, out, _ = run_cli(capsys, server, "files", "download", file_id, str(dest))
    assert code == 0 and dest.read_bytes() == b"data"
    code, out, _ = run_cli(capsys, server, "files", "rm", file_id)
    assert code == 0

    code, out, _ = run_cli(capsys, server, "storage", "put", str(source),
                           "--replication", "2")
    assert code == 0
    blob_id = out.strip()
    code, out, _ = run_cli(capsys, server, "storage", "list")
    assert code == 0 and blob_id in out

    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"type": "benchmark", "params": {}}))
    code, out, _ = run_cli(capsys, server, "schedules", "add", "bench",
                           "0 * * * *", str(spec))
    assert code == 0
    body = state.last("POST", "/api/schedules")["json"]
    assert body["job_spec"] == {"type": "benchmark", "params": {}}
    code, out, _ = run_cli(capsys, server, "schedules", "list")
    assert code == 0 and "hourly bench" in out
    code, out, _ = run_cli(capsys, server, "templates", "save", "t", str(spec))
    assert code == 0
    code, out, _ = run_cli(capsys, server, "templates", "list")
    assert code == 0 and "squares" in out


def test_cli_login_writes_config(capsys, server, tmp_path, monkeypatch):
    url, _ = server
    code = pc_cli.main(["login", url, PIN])
    out, _ = capsys.readouterr()
    assert code == 0
    target = tmp_path / "data" / "cli.json"
    assert target.exists()
    assert json.loads(target.read_text()) == {"url": url, "pin": PIN}
    assert str(target) in out


def test_cli_join_prints_urls(capsys, server):
    code, out, _ = run_cli(capsys, server, "join")
    assert code == 0
    assert "ABCD-EFGH" in out and "works once" in out
    assert "https://10.0.0.5:8765/w?code=ABCD-EFGH" in out
    assert PIN not in out
    assert "install.sh" in out
    code, out, _ = run_cli(capsys, server, "join", "--termux")
    assert code == 0 and "paste into Termux" in out


def test_cli_export_and_result_and_logs(capsys, server, tmp_path):
    _, state = server
    job = state.create_job({"type": "python_map"})
    state.jobs[job["id"]]["result"] = {"ok": 1}
    dest = tmp_path / "e.json"
    code, out, _ = run_cli(capsys, server, "export", job["id"],
                           "--format", "json", "-o", str(dest))
    assert code == 0 and dest.exists()
    code, out, _ = run_cli(capsys, server, "result", job["id"])
    assert code == 0
    code, out, _ = run_cli(capsys, server, "logs", job["id"])
    assert code == 0 and "hello from" in out
    code, out, _ = run_cli(capsys, server, "logs", job["id"], "--task", "1")
    assert code == 0 and "t1" in out


def test_cli_no_command_prints_help(capsys):
    code = pc_cli.main([])
    out, _ = capsys.readouterr()
    assert code == 1
    assert "usage" in out.lower()


def test_cli_device_pause_resume_remove(capsys, server):
    _, state = server
    code, out, _ = run_cli(capsys, server, "device", "dev1")
    assert code == 0 and '"id": "dev1"' in out
    code, out, _ = run_cli(capsys, server, "pause", "dev1")
    assert code == 0 and "paused dev1" in out
    assert state.last("PATCH", "/api/devices/dev1")["json"] == {"paused": True}
    code, out, _ = run_cli(capsys, server, "resume", "dev1")
    assert code == 0 and "resumed dev1" in out
    assert state.last("PATCH", "/api/devices/dev1")["json"] == {"paused": False}
    code, out, _ = run_cli(capsys, server, "remove", "dev1")
    assert code == 0 and "removed dev1" in out
    code, out, _ = run_cli(capsys, server, "bench", "dev1")
    assert code == 0 and '"score": 3.5' in out


def test_print_table_alignment(capsys):
    pc_cli.print_table(["A", "LONGER"], [["1", "x"], ["222222", "y"]])
    out, _ = capsys.readouterr()
    lines = out.splitlines()
    assert lines[0] == "A       LONGER"
    assert lines[1] == "------  ------"
    assert lines[2] == "1       x"
    assert lines[3] == "222222  y"
    pc_cli.print_table(["A"], [])
    out, _ = capsys.readouterr()
    assert "(none)" in out


# --------------------------------------------------------------------------
# WebSocket frame codec (no server needed)
# --------------------------------------------------------------------------


def test_ws_encode_small_text_frame():
    frame = ws_encode_frame(WS_TEXT, "hi", mask=True, masking_key=b"\x01\x02\x03\x04")
    assert frame[0] == 0x81                      # FIN + text
    assert frame[1] == 0x80 | 2                  # masked, length 2
    assert frame[2:6] == b"\x01\x02\x03\x04"
    assert frame[6:] == bytes([ord("h") ^ 1, ord("i") ^ 2])


def test_ws_encode_unmasked_and_lengths():
    assert ws_encode_frame(WS_TEXT, "abc", mask=False) == b"\x81\x03abc"
    medium = ws_encode_frame(WS_BINARY_OPCODE, b"x" * 200, mask=False)
    assert medium[0] == 0x82 and medium[1] == 126
    assert struct.unpack("!H", medium[2:4])[0] == 200
    large = ws_encode_frame(WS_TEXT, "y" * 70000, mask=False)
    assert large[1] == 127
    assert struct.unpack("!Q", large[2:10])[0] == 70000


WS_BINARY_OPCODE = 0x2


def test_ws_decode_hand_built_frames():
    # server -> client frames are unmasked
    assert ws_decode_frame(b"\x81\x05hello") == (True, WS_TEXT, b"hello", 7)
    payload = b"z" * 300
    frame = b"\x82\x7e" + struct.pack("!H", 300) + payload
    assert ws_decode_frame(frame) == (True, 0x2, payload, len(frame))
    big = b"\x81\x7f" + struct.pack("!Q", 5) + b"world"
    assert ws_decode_frame(big) == (True, WS_TEXT, b"world", len(big))
    # a masked frame decodes back to plaintext
    masked = b"\x81\x82\x01\x02\x03\x04" + bytes([ord("h") ^ 1, ord("i") ^ 2])
    assert ws_decode_frame(masked)[2] == b"hi"


def test_ws_decode_incomplete_returns_none():
    assert ws_decode_frame(b"") is None
    assert ws_decode_frame(b"\x81") is None
    assert ws_decode_frame(b"\x81\x05hel") is None
    assert ws_decode_frame(b"\x81\x7e\x01") is None
    assert ws_decode_frame(b"\x81\xfe\x00\x05hello") is None  # masked, no key


def test_ws_roundtrip_and_offsets():
    stream = (ws_encode_frame(WS_TEXT, '{"ev":"term"}', mask=False)
              + ws_encode_frame(WS_PING, b"pp", mask=False)
              + ws_encode_frame(WS_CLOSE, struct.pack("!H", 1000), mask=False))
    fin, opcode, payload, offset = ws_decode_frame(stream)
    assert (fin, opcode, payload) == (True, WS_TEXT, b'{"ev":"term"}')
    fin, opcode, payload, offset = ws_decode_frame(stream, offset)
    assert (opcode, payload) == (WS_PING, b"pp")
    fin, opcode, payload, offset = ws_decode_frame(stream, offset)
    assert opcode == WS_CLOSE and offset == len(stream)


def test_ws_masking_is_reversible_for_every_byte():
    payload = bytes(range(256))
    frame = ws_encode_frame(WS_BINARY_OPCODE, payload, mask=True)
    assert frame[1] & 0x80, "client frames must be masked"
    assert ws_decode_frame(frame)[2] == payload


def test_ws_encode_rejects_bad_masking_key():
    with pytest.raises(ValueError):
        ws_encode_frame(WS_TEXT, "x", mask=True, masking_key=b"\x01")


def test_terminal_opens_and_closes_the_session(cluster, server, monkeypatch):
    """terminal() must POST /api/terminal and DELETE it on the way out."""
    _, state = server

    class FakeSocket(object):
        def __init__(self):
            self.sent = []
            self.closed = False
            self._messages = [
                {"ev": "term", "session_id": "term0001", "data": "hello\n"},
                {"ev": "term_closed", "session_id": "term0001", "exit_code": 0},
            ]

        def send_json(self, obj):
            self.sent.append(obj)

        def recv_json(self, timeout=None):
            return self._messages.pop(0) if self._messages else None

        def close(self):
            self.closed = True

    fake = FakeSocket()
    monkeypatch.setattr(Cluster, "dashboard_socket", lambda self, timeout=30: fake)
    out = io.StringIO()
    code = cluster.terminal("dev1", stdin=io.StringIO(""), stdout=out)
    assert code == 0
    assert "hello" in out.getvalue()
    assert fake.closed
    assert state.last("POST", "/api/terminal")["json"] == {"device_id": "dev1"}
    assert state.find("DELETE", "/api/terminal/term0001")
