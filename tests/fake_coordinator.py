"""A minimal stand-in for the PhoneCluster coordinator's worker API (SPEC §8).

Implements just enough of ``/api/w/*`` to exercise ``worker/worker.py`` without
the real FastAPI coordinator: register, long-poll with scripted commands, task
start/log/done, file GET/PUT, chunk GET/PUT/ack, bench, terminal output/closed
and leave.  Everything the worker sends is recorded for assertions.

Used by tests/test_worker_unit.py; it can also be run standalone:

    python tests/fake_coordinator.py --port 8765 --pin 123456
"""

import hashlib
import json
import os
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

__all__ = ["FakeCoordinator"]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeCoordinator/1.0"

    # -- plumbing --------------------------------------------------------
    @property
    def fake(self):
        return self.server.fake

    def log_message(self, fmt, *args):  # silence stderr noise
        if self.fake.verbose:
            print("[fake] " + (fmt % args))

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _json_body(self):
        raw = self._body()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}

    def _send(self, status, payload=None, raw=None, ctype="application/json",
              headers=None):
        if raw is None:
            raw = json.dumps(payload if payload is not None else {}).encode("utf-8")
        closing = self.fake.stopping
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            if closing:
                # stop() must really disconnect workers, not leave keep-alive
                # sockets served by lingering handler threads.
                self.send_header("Connection", "close")
                self.close_connection = True
            for key, val in (headers or {}).items():
                self.send_header(key, val)
            self.end_headers()
            if raw:
                self.wfile.write(raw)
        except (OSError, ValueError):
            # the worker was killed mid-request; nothing to report
            self.close_connection = True

    def _device(self):
        """Authenticate; returns device_id or sends the error response."""
        token = self.headers.get("X-Device-Token")
        with self.fake.lock:
            device_id = self.fake.tokens.get(token)
            if device_id is None:
                self._send(401, {"error": "unauthorized"})
                return None
            if device_id in self.fake.removed:
                self._send(410, {"error": "removed"})
                return None
        return device_id

    # -- routing ---------------------------------------------------------
    def _send_blob(self, data, key):
        """Serve bytes with Range support (runtime archives, models)."""
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            first, _, last = rng[6:].partition("-")
            start = int(first or 0)
            end = min(int(last) if last else len(data) - 1, len(data) - 1)
            with self.fake.lock:
                self.fake.range_requests.append((key, start, end))
            return self._send(206, raw=data[start:end + 1], ctype="application/octet-stream",
                              headers={"Content-Range": "bytes %d-%d/%d" % (start, end, len(data))})
        return self._send(200, raw=data, ctype="application/octet-stream")

    def do_GET(self):
        parts = urlparse(self.path)
        path = parts.path
        if path == "/api/w/runtime/llama":
            if self._device() is None:
                return
            return self._send(200, self.fake.runtime)
        if path.startswith("/api/w/runtime/llama/"):
            if self._device() is None:
                return
            name = path[len("/api/w/runtime/llama/"):]
            with self.fake.lock:
                data = self.fake.runtime_files.get(name)
                self.fake.runtime_downloads.append(name)
            if data is None:
                return self._send(404, {"error": "no such runtime file"})
            return self._send_blob(data, name)
        if path.startswith("/api/w/models/"):
            if self._device() is None:
                return
            name = path[len("/api/w/models/"):]
            with self.fake.lock:
                data = self.fake.models.get(name)
                cut = self.fake.model_cut.pop(name, None)
            if data is None:
                return self._send(404, {"error": "no such model"})
            if cut is not None and not self.headers.get("Range"):
                # simulate a dropped connection: promise everything, send a part
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data[:cut])
                self.wfile.flush()
                self.close_connection = True
                return None
            return self._send_blob(data, name)
        if path.startswith("/api/w/files/"):
            if self._device() is None:
                return
            file_id = path[len("/api/w/files/"):]
            with self.fake.lock:
                entry = self.fake.files.get(file_id)
            if entry is None:
                return self._send(404, {"error": "not found"})
            name, data = entry
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                first, _, last = rng[6:].partition("-")
                start = int(first or 0)
                end = int(last) if last else len(data) - 1
                end = min(end, len(data) - 1)
                with self.fake.lock:
                    self.fake.range_requests.append((file_id, start, end))
                return self._send(206, raw=data[start:end + 1], ctype="application/octet-stream",
                                  headers={"Content-Range": "bytes %d-%d/%d" % (start, end, len(data)),
                                           "Content-Disposition":
                                           'attachment; filename="%s"' % name})
            return self._send(200, raw=data, ctype="application/octet-stream",
                              headers={"Content-Disposition":
                                       'attachment; filename="%s"' % name})
        if path.startswith("/api/w/requests/") and path.endswith("/input"):
            if self._device() is None:
                return
            rid = path[len("/api/w/requests/"):-len("/input")]
            with self.fake.lock:
                entry = self.fake.request_inputs.get(rid)
            if entry is None:
                return self._send(404, {"error": "unknown request"})
            data, ctype = entry
            # chunked transfer in slices, with small pauses: the worker must
            # feed the model incrementally, not wait for the whole body
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            step = max(1, len(data) // 4) if data else 1
            for i in range(0, len(data), step):
                piece = data[i:i + step]
                self.wfile.write(("%x\r\n" % len(piece)).encode("ascii") + piece + b"\r\n")
                self.wfile.flush()
                time.sleep(0.05)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return None
        if path.startswith("/api/w/chunk/"):
            if self._device() is None:
                return
            chunk_id = path[len("/api/w/chunk/"):]
            with self.fake.lock:
                data = self.fake.chunks.get(chunk_id)
            if data is None:
                return self._send(404, {"error": "not found"})
            return self._send(200, raw=data, ctype="application/octet-stream")
        if path == "/ping":
            return self._send(200, {"ok": True})
        if path == "/w" or path.startswith("/w/"):
            return self._static(path)
        return self._send(404, {"error": "not found"})

    #: mirrors the coordinator's static routes for worker/browser (SPEC §11)
    STATIC_TYPES = {".html": "text/html; charset=utf-8",
                    ".js": "application/javascript; charset=utf-8",
                    ".css": "text/css; charset=utf-8"}

    def _static(self, path):
        base = self.fake.browser_dir
        if not base:
            return self._send(404, {"error": "not found"})
        rel = "index.html" if path == "/w" else path[len("/w/"):]
        if not rel or ".." in rel or rel.startswith("/"):
            return self._send(404, {"error": "not found"})
        full = os.path.join(base, rel.replace("/", os.sep))
        if not os.path.isfile(full):
            return self._send(404, {"error": "not found"})
        with open(full, "rb") as handle:
            data = handle.read()
        ext = os.path.splitext(full)[1].lower()
        return self._send(200, raw=data,
                          ctype=self.STATIC_TYPES.get(ext, "application/octet-stream"))

    def do_PUT(self):
        parts = urlparse(self.path)
        path = parts.path
        query = parse_qs(parts.query)
        if self._device() is None:
            return
        if path == "/api/w/files":
            data = self._body()
            name = (query.get("name") or ["upload.bin"])[0]
            file_id = hashlib.sha256(data).hexdigest()
            with self.fake.lock:
                self.fake.files[file_id] = (name, data)
                self.fake.uploads.append({"file_id": file_id, "name": name,
                                          "size": len(data)})
            return self._send(200, {"file_id": file_id, "size": len(data),
                                    "name": name})
        if path.startswith("/api/w/chunk/"):
            chunk_id = path[len("/api/w/chunk/"):]
            data = self._body()
            req_id = (query.get("req_id") or [""])[0]
            with self.fake.lock:
                self.fake.chunk_puts.append({"chunk_id": chunk_id, "req_id": req_id,
                                             "data": data})
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        parts = urlparse(self.path)
        path = parts.path
        if path == "/api/w/register":
            return self._register()
        device_id = self._device()
        if device_id is None:
            return
        if path == "/api/w/poll":
            return self._poll(device_id)
        if path == "/api/w/chunk_ack":
            body = self._json_body()
            with self.fake.lock:
                self.fake.chunk_acks.append(body)
            return self._send(200, {"ok": True})
        if path == "/api/w/bench":
            body = self._json_body()
            with self.fake.lock:
                self.fake.benches.append(body)
            return self._send(200, {"ok": True})
        if path == "/api/w/leave":
            with self.fake.lock:
                self.fake.leaves.append(device_id)
            return self._send(200, {"ok": True})
        if path.startswith("/api/w/task/"):
            rest = path[len("/api/w/task/"):]
            task_id, _, action = rest.partition("/")
            body = self._json_body()
            with self.fake.lock:
                if action == "start":
                    self.fake.started.append(task_id)
                elif action == "log":
                    self.fake.logs.setdefault(task_id, []).extend(body.get("lines") or [])
                elif action == "checkpoint":
                    self.fake.checkpoints.setdefault(task_id, []).append(body)
                    self.fake.checkpoint_times[task_id] = time.time()
                elif action == "done":
                    if task_id in self.fake.reject_done:
                        return self._send(409, {"error": "task already finished"})
                    self.fake.results[task_id] = body
                    self.fake.done_order.append(task_id)
                    self.fake.done_times[task_id] = time.time()
                else:
                    return self._send(404, {"error": "not found"})
            return self._send(200, {"ok": True})
        if path.startswith("/api/w/service/"):
            task_id = path[len("/api/w/service/"):].split("/")[0]
            body = self._json_body()
            with self.fake.lock:
                self.fake.service_ready.append((task_id, body.get("name"), body.get("meta")))
            return self._send(200, {"ok": True})
        if path.startswith("/api/w/requests/"):
            rest = path[len("/api/w/requests/"):]
            rid, _, action = rest.partition("/")
            body = self._json_body()
            with self.fake.lock:
                if action == "partial":
                    self.fake.request_partials.setdefault(rid, []).append(body.get("data"))
                elif action == "done":
                    self.fake.request_results[rid] = body
                else:
                    return self._send(404, {"error": "not found"})
            return self._send(200, {"ok": True})
        if path.startswith("/api/w/term/"):
            rest = path[len("/api/w/term/"):]
            sid, _, action = rest.partition("/")
            body = self._json_body()
            with self.fake.lock:
                if action == "output":
                    self.fake.term_output[sid] = (
                        self.fake.term_output.get(sid, "") + (body.get("data") or ""))
                elif action == "closed":
                    self.fake.term_closed[sid] = body.get("exit_code")
                else:
                    return self._send(404, {"error": "not found"})
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})

    # -- endpoints -------------------------------------------------------
    def _register(self):
        body = self._json_body()
        fake = self.fake
        with fake.lock:
            fake.registrations.append(body)
            device_id = body.get("device_id")
            token = body.get("token")
            if device_id and token:
                if device_id in fake.removed:
                    return self._send(410, {"error": "removed"})
                if fake.tokens.get(token) != device_id:
                    return self._send(401, {"error": "unauthorized"})
                fake.devices[device_id] = body
                new_id, new_token = device_id, token
            else:
                if body.get("pin") != fake.pin:
                    return self._send(401, {"error": "unauthorized"})
                fake.counter += 1
                new_id = "dev%09d" % fake.counter
                new_token = hashlib.sha256(
                    ("tok%s%s" % (new_id, time.time())).encode()).hexdigest()[:32]
                fake.tokens[new_token] = new_id
                fake.devices[new_id] = body
                fake.queues[new_id] = deque()
            resp = {"device_id": new_id, "token": new_token,
                    "config": dict(fake.config)}
            if fake.bench_done:
                resp["bench_done"] = True
        return self._send(200, resp)

    def _poll(self, device_id):
        body = self._json_body()
        fake = self.fake
        with fake.lock:
            fake.polls.append({"device_id": device_id, "ts": time.time(),
                               "status": body.get("status") or {}})
            fake.last_status[device_id] = body.get("status") or {}
        deadline = time.time() + float(fake.config["poll_timeout"])
        commands = []
        while time.time() < deadline:
            with fake.lock:
                queue = fake.queues.setdefault(device_id, deque())
                while queue:
                    commands.append(queue.popleft())
                stop = fake.stopping
            if commands or stop:
                break
            time.sleep(0.02)
        return self._send(200, {"commands": commands})


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # False on purpose: on Windows SO_REUSEADDR lets a second server silently
    # bind a port another process already owns, which makes tests talk to the
    # wrong server.  Tests bind port 0 anyway.
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        # Workers get killed abruptly in tests; do not spam stderr for that.
        if getattr(self.fake, "verbose", False):
            ThreadingHTTPServer.handle_error(self, request, client_address)


class FakeCoordinator(object):
    """Records every worker call and lets tests push commands into the poll."""

    #: worker/browser assets are served at /w and /w/* like the real coordinator
    BROWSER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "worker", "browser")

    def __init__(self, pin="123456", poll_timeout=1.0, log_batch_ms=100,
                 cache_mb=500, bench_done=True, verbose=False, browser_dir=None):
        self.pin = pin
        self.verbose = verbose
        self.bench_done = bench_done
        self.browser_dir = browser_dir if browser_dir is not None else self.BROWSER_DIR
        self.config = {"poll_timeout": poll_timeout, "offline_after": 30,
                       "log_batch_ms": log_batch_ms, "cache_mb": cache_mb}
        self.lock = threading.RLock()
        self.counter = 0
        self.stopping = False
        self.devices = {}
        self.tokens = {}
        self.removed = set()
        self.queues = {}
        self.registrations = []
        self.polls = []
        self.last_status = {}
        self.files = {}
        self.uploads = []
        self.chunks = {}
        self.chunk_puts = []
        self.chunk_acks = []
        self.benches = []
        self.leaves = []
        self.started = []
        self.logs = {}
        self.results = {}
        self.done_order = []
        self.reject_done = set()  # task ids whose /done gets a 409
        self.checkpoints = {}     # task_id -> [checkpoint bodies]
        self.checkpoint_times = {}
        self.done_times = {}
        self.range_requests = []  # (file_id, start, end)
        # llama.cpp runtime and models (SPEC 6.8)
        self.runtime = {"tag": None, "assets": {}}
        self.runtime_files = {}   # file name -> bytes
        self.runtime_downloads = []
        self.models = {}          # model name -> bytes
        self.model_cut = {}       # model name -> bytes to send before "dropping" once
        # services (SPEC 6.7)
        self.service_ready = []   # (task_id, name, meta)
        self.request_inputs = {}  # request_id -> (bytes, content_type)
        self.request_partials = {}
        self.request_results = {}
        self.term_output = {}
        self.term_closed = {}
        self._server = None
        self._thread = None

    # -- lifecycle -------------------------------------------------------
    def start(self, port=0):
        self._server = _Server(("127.0.0.1", port), _Handler)
        self._server.fake = self
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.05})
        self._thread.daemon = True
        self._thread.start()
        return self

    def stop(self):
        self.stopping = True
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:
                pass
            try:
                self._server.server_close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()

    @property
    def port(self):
        return self._server.server_address[1]

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    # -- test helpers ----------------------------------------------------
    def push(self, command, device_id=None):
        with self.lock:
            targets = [device_id] if device_id else list(self.queues.keys())
            if not targets:
                targets = list(self.devices.keys())
            for target in targets:
                self.queues.setdefault(target, deque()).append(command)
        return command

    def device_ids(self):
        with self.lock:
            return list(self.devices.keys())

    def add_file(self, name, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        file_id = hashlib.sha256(data).hexdigest()
        with self.lock:
            self.files[file_id] = (name, data)
        return file_id

    def add_chunk(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        chunk_id = hashlib.sha256(data).hexdigest()
        with self.lock:
            self.chunks[chunk_id] = data
        return chunk_id

    def remove_device(self, device_id):
        with self.lock:
            self.removed.add(device_id)

    def send_request(self, device_id, task_id, request_id, data, content_type="application/octet-stream",
                     params=None, name=None):
        """Stage a service request body and hand the request to the device."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        with self.lock:
            self.request_inputs[request_id] = (data, content_type)
        self.push({"cmd": "service_request", "task_id": task_id,
                   "request": {"id": request_id, "name": name, "params": params or {},
                               "content_type": content_type, "size": len(data)}}, device_id)
        return request_id

    def request_result(self, request_id):
        with self.lock:
            return self.request_results.get(request_id)

    def result(self, task_id):
        with self.lock:
            return self.results.get(task_id)

    def log_text(self, task_id):
        with self.lock:
            return "\n".join(self.logs.get(task_id, []))

    def file_bytes(self, file_id):
        with self.lock:
            entry = self.files.get(file_id)
        return entry[1] if entry else None

    def find_upload(self, name):
        with self.lock:
            for item in reversed(self.uploads):
                if item["name"] == name or item["name"].endswith("/" + name):
                    return item
        return None


def _cli():
    import argparse

    parser = argparse.ArgumentParser(description="Fake PhoneCluster coordinator")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--pin", default="123456")
    parser.add_argument("--poll-timeout", type=float, default=5.0)
    args = parser.parse_args()
    fake = FakeCoordinator(pin=args.pin, poll_timeout=args.poll_timeout,
                           verbose=True).start(args.port)
    print("fake coordinator on %s (pin %s)" % (fake.url, fake.pin))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        fake.stop()


if __name__ == "__main__":
    _cli()
