"""PhoneCluster client library.

Stdlib only, Python 3.8+ compatible (it also runs inside Termux on a phone).
See docs/SPEC.md sections 11 and 13 for the API contract this consumes.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import mimetypes
import os
import shutil
import socket
import struct
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

__all__ = [
    "Cluster",
    "Job",
    "Pipeline",
    "PhoneClusterError",
    "ClusterError",
    "JobFailed",
    "WebSocket",
    "ws_encode_frame",
    "ws_decode_frame",
]

#: the PC-only door: the dashboard API answers only on the PC itself (SPEC 3)
DEFAULT_URL = "http://127.0.0.1:8764"
TERMINAL_STATES = ("done", "failed", "cancelled")
#: items larger than this (as encoded JSON) are uploaded as a file instead of inlined
INLINE_ITEMS_LIMIT = 2 * 1024 * 1024


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class PhoneClusterError(Exception):
    """Base class for every error raised by this package."""


class ClusterError(PhoneClusterError):
    """A non-2xx response from the coordinator (or a transport failure)."""

    def __init__(self, status, message):
        self.status = status
        self.message = message
        if status:
            super(ClusterError, self).__init__("[%s] %s" % (status, message))
        else:
            super(ClusterError, self).__init__(str(message))


class JobFailed(PhoneClusterError):
    """A job finished in the ``failed`` or ``cancelled`` state."""

    def __init__(self, job, error=None):
        self.job = job
        self.job_id = getattr(job, "id", None)
        self.status = getattr(job, "status", None)
        self.error = error or "job %s" % (self.status,)
        super(JobFailed, self).__init__(
            "job %s %s: %s" % (self.job_id, self.status, self.error)
        )


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


def project_root():
    """Root of the PhoneCluster checkout (the folder that holds ``sdk/``)."""
    env = os.environ.get("PC_ROOT")
    if env:
        return os.path.abspath(env)
    # <root>/sdk/phonecluster/client.py -> <root>
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(os.path.dirname(here))


def config_path():
    """Path of ``data/cli.json`` (written by ``pc login``)."""
    return os.path.join(project_root(), "data", "cli.json")


def load_config():
    """Read ``data/cli.json``; falls back to ``~/.phonecluster/cli.json``."""
    for path in (config_path(), os.path.join(
            os.path.expanduser("~"), ".phonecluster", "cli.json")):
        try:
            with open(path, "r") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return {}


def save_config(url, pin, path=None):
    """Write ``data/cli.json``. Returns the path written."""
    path = path or config_path()
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(path, "w") as fh:
        json.dump({"url": url, "pin": pin}, fh, indent=2)
        fh.write("\n")
    return path


def ca_file():
    """The cluster's own CA (``data/tls/ca.pem``), for HTTPS to the LAN port."""
    path = os.environ.get("PC_CA") or os.path.join(project_root(), "data", "tls", "ca.pem")
    return path if os.path.isfile(path) else None


def ssl_context():
    """Trust only the cluster CA; host names are not checked (the PC's
    address changes, and only the cluster can sign)."""
    import ssl

    path = ca_file()
    if not path:
        raise ClusterError(0, "no cluster certificate (data/tls/ca.pem): use the PC-only "
                              "address %s instead" % DEFAULT_URL)
    ctx = ssl.create_default_context(cafile=path)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def normalize_url(url):
    url = (url or "").strip().rstrip("/")
    if not url:
        return url
    if "://" not in url:
        url = "http://" + url
    return url


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _as_list(data, *keys):
    """Coordinator list endpoints may return a bare list or an envelope."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys + ("items", "results", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        lists = [v for v in data.values() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0]
    return []


def _unwrap_result(data):
    """``GET /api/jobs/{id}/result`` may return the value or an envelope."""
    if isinstance(data, dict) and "result" in data:
        envelope = set(["result", "error", "status", "id", "job_id", "name", "ok"])
        if set(data.keys()) <= envelope:
            return data["result"]
    return data


DISCOVERY_PORT = 8766
DISCOVERY_MAGIC = b"PHONECLUSTER_DISCOVER v1"


def discover(timeout=2.0, port=DISCOVERY_PORT, hosts=None, all_replies=False):
    """Find a coordinator on the LAN with a UDP broadcast (SPEC 2.1).

    Returns its base URL (or a list of ``{url, name, urls}`` replies with
    ``all_replies=True``), or None / ``[]`` when nothing answers.
    """
    targets = list(hosts) if hosts else ["255.255.255.255"]
    if not hosts:
        probe = None
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("10.255.255.255", 1))
            parts = probe.getsockname()[0].split(".")
            if len(parts) == 4:
                targets.append(".".join(parts[:3] + ["255"]))
        except OSError:
            pass
        finally:
            if probe is not None:
                probe.close()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    found = []
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(timeout)
        for host in targets:
            try:
                sock.sendto(DISCOVERY_MAGIC, (host, int(port)))
            except OSError:
                pass
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, _addr = sock.recvfrom(4096)
            except (socket.timeout, OSError):
                break
            try:
                reply = json.loads(data.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(reply, dict) and reply.get("phonecluster") and reply.get("url"):
                if not all_replies:
                    return str(reply["url"])
                if reply["url"] not in [f["url"] for f in found]:
                    found.append({"url": str(reply["url"]), "name": reply.get("name"),
                                  "urls": reply.get("urls") or []})
    finally:
        sock.close()
    return found if all_replies else None


PACKAGE_SKIP_DIRS = ("__pycache__", ".git", ".venv", ".pytest_cache")
PACKAGE_SKIP_SUFFIXES = (".pyc", ".pyo")


def package_bytes(path):
    """``(zip bytes, upload name)`` for a job package folder or ``.zip`` file."""
    import io
    import zipfile

    path = os.path.abspath(str(path))
    if os.path.isfile(path):
        with open(path, "rb") as fh:
            return fh.read(), os.path.basename(path)
    if not os.path.isdir(path):
        raise PhoneClusterError("no such package: %s" % path)
    manifest = [n for n in ("job.yaml", "job.yml", "job.json")
                if os.path.isfile(os.path.join(path, n))]
    if not manifest:
        raise PhoneClusterError("%s has no job.yaml / job.yml / job.json" % path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(path):
            dirs[:] = sorted(d for d in dirs if d not in PACKAGE_SKIP_DIRS)
            for name in sorted(files):
                if name.endswith(PACKAGE_SKIP_SUFFIXES):
                    continue
                full = os.path.join(root, name)
                rel = os.path.relpath(full, path).replace(os.sep, "/")
                zf.write(full, rel)
    return buf.getvalue(), os.path.basename(path.rstrip("\\/")) + ".zip"


def function_source(fn):
    """Dedented source of ``fn``, ready to ship as plugin ``code``."""
    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError) as exc:
        raise PhoneClusterError(
            "cannot read the source of %r (define it in a .py file, not a REPL): %s"
            % (getattr(fn, "__name__", fn), exc)
        )
    return textwrap.dedent(src).strip() + "\n"


def _safe_name(name, fallback):
    name = os.path.basename(str(name or "")).strip()
    bad = '<>:"/\\|?*'
    name = "".join("_" if c in bad or ord(c) < 32 else c for c in name)
    return name or fallback


# --------------------------------------------------------------------------
# multipart bodies (built by hand: no requests, no email package games)
# --------------------------------------------------------------------------


class _MultipartBody(object):
    """A streaming ``multipart/form-data`` body with a known Content-Length."""

    def __init__(self, fields=None, files=None):
        self.boundary = "----PhoneCluster" + os.urandom(16).hex()
        self._parts = []  # list of bytes | (path, size)
        self._length = 0
        for key, value in (fields or {}).items():
            if value is None:
                continue
            head = (
                "--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                % (self.boundary, key, value)
            ).encode("utf-8")
            self._parts.append(head)
            self._length += len(head)
        for field, filename, ctype, source in (files or []):
            head = (
                "--%s\r\nContent-Disposition: form-data; name=\"%s\"; "
                "filename=\"%s\"\r\nContent-Type: %s\r\n\r\n"
                % (self.boundary, field, filename, ctype)
            ).encode("utf-8")
            self._parts.append(head)
            self._length += len(head)
            if isinstance(source, (bytes, bytearray)):
                self._parts.append(bytes(source))
                self._length += len(source)
            else:  # a path on disk, streamed at send time
                size = os.path.getsize(source)
                self._parts.append((source, size))
                self._length += size
            self._parts.append(b"\r\n")
            self._length += 2
        tail = ("--%s--\r\n" % self.boundary).encode("utf-8")
        self._parts.append(tail)
        self._length += len(tail)
        self._index = 0
        self._fh = None
        self._buf = b""

    content_length = property(lambda self: self._length)
    content_type = property(
        lambda self: "multipart/form-data; boundary=%s" % self.boundary)

    def __len__(self):
        return self._length

    def read(self, size=-1):
        if size is None or size < 0:
            size = self._length
        out = []
        want = size
        while want > 0:
            if self._buf:
                take, self._buf = self._buf[:want], self._buf[want:]
                out.append(take)
                want -= len(take)
                continue
            if self._fh is not None:
                chunk = self._fh.read(min(want, 262144))
                if chunk:
                    out.append(chunk)
                    want -= len(chunk)
                    continue
                self._fh.close()
                self._fh = None
                continue
            if self._index >= len(self._parts):
                break
            part = self._parts[self._index]
            self._index += 1
            if isinstance(part, tuple):
                self._fh = open(part[0], "rb")
            else:
                self._buf = part
        return b"".join(out)

    def close(self):
        if self._fh is not None:
            self._fh.close()
            self._fh = None


class _FileBody(object):
    """Streams a file with a known Content-Length (raw ``PUT``)."""

    def __init__(self, path):
        self.path = path
        self.content_length = os.path.getsize(path)
        self._fh = open(path, "rb")

    def __len__(self):
        return self.content_length

    def read(self, size=-1):
        return self._fh.read(size if size and size > 0 else -1)

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# minimal RFC 6455 WebSocket client
# --------------------------------------------------------------------------

WS_CONT = 0x0
WS_TEXT = 0x1
WS_BINARY = 0x2
WS_CLOSE = 0x8
WS_PING = 0x9
WS_PONG = 0xA
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_encode_frame(opcode, payload=b"", mask=True, fin=True, masking_key=None):
    """Encode one WebSocket frame. Client frames MUST be masked (RFC 6455 5.3)."""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    payload = bytes(payload)
    out = bytearray()
    out.append((0x80 if fin else 0x00) | (opcode & 0x0F))
    length = len(payload)
    flag = 0x80 if mask else 0x00
    if length < 126:
        out.append(flag | length)
    elif length < 65536:
        out.append(flag | 126)
        out.extend(struct.pack("!H", length))
    else:
        out.append(flag | 127)
        out.extend(struct.pack("!Q", length))
    if mask:
        key = masking_key if masking_key is not None else os.urandom(4)
        key = bytes(key)
        if len(key) != 4:
            raise ValueError("masking key must be 4 bytes")
        out.extend(key)
        out.extend(bytes(b ^ key[i % 4] for i, b in enumerate(payload)))
    else:
        out.extend(payload)
    return bytes(out)


def ws_decode_frame(data, offset=0):
    """Decode one frame from ``data``.

    Returns ``(fin, opcode, payload, next_offset)`` or ``None`` when more
    bytes are needed.
    """
    view = memoryview(data)
    end = len(data)
    if end - offset < 2:
        return None
    b0 = view[offset]
    b1 = view[offset + 1]
    if not isinstance(b0, int):  # pragma: no cover - py2-style memoryview
        b0, b1 = ord(b0), ord(b1)
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    pos = offset + 2
    if length == 126:
        if end - pos < 2:
            return None
        length = struct.unpack("!H", bytes(view[pos:pos + 2]))[0]
        pos += 2
    elif length == 127:
        if end - pos < 8:
            return None
        length = struct.unpack("!Q", bytes(view[pos:pos + 8]))[0]
        pos += 8
    key = None
    if masked:
        if end - pos < 4:
            return None
        key = bytes(view[pos:pos + 4])
        pos += 4
    if end - pos < length:
        return None
    payload = bytes(view[pos:pos + length])
    pos += length
    if key:
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload, pos


class WebSocket(object):
    """A tiny client: handshake, masked text frames, ping/pong, close."""

    def __init__(self, url, timeout=30, headers=None):
        self.url = url
        self.timeout = timeout
        self.closed = False
        self._buf = bytearray()
        self._frag = []
        self._frag_opcode = None
        self._send_lock = threading.Lock()
        parts = urllib.parse.urlsplit(url)
        secure = parts.scheme in ("wss", "https")
        port = parts.port or (443 if secure else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        self._sock = socket.create_connection((parts.hostname, port), timeout)
        if secure:
            self._sock = ssl_context().wrap_socket(self._sock, server_hostname=parts.hostname)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        host = parts.hostname
        if parts.port:
            host = "%s:%d" % (host, parts.port)
        lines = [
            "GET %s HTTP/1.1" % path,
            "Host: %s" % host,
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Key: %s" % key,
            "Sec-WebSocket-Version: 13",
        ]
        for name, value in (headers or {}).items():
            lines.append("%s: %s" % (name, value))
        self._sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        head = self._read_headers()
        status_line = head.split("\r\n", 1)[0]
        if " 101" not in status_line:
            self.close()
            raise ClusterError(0, "websocket handshake failed: %s" % status_line)
        expect = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
        got = ""
        for line in head.split("\r\n")[1:]:
            if line.lower().startswith("sec-websocket-accept:"):
                got = line.split(":", 1)[1].strip()
        if got and got != expect:
            self.close()
            raise ClusterError(0, "websocket handshake: bad Sec-WebSocket-Accept")

    def _read_headers(self):
        data = bytearray()
        while b"\r\n\r\n" not in bytes(data):
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ClusterError(0, "websocket handshake: connection closed")
            data.extend(chunk)
            if len(data) > 65536:
                raise ClusterError(0, "websocket handshake: headers too large")
        raw = bytes(data)
        head, rest = raw.split(b"\r\n\r\n", 1)
        self._buf.extend(rest)
        return head.decode("latin-1")

    def send(self, payload, opcode=WS_TEXT):
        if self.closed:
            raise ClusterError(0, "websocket is closed")
        frame = ws_encode_frame(opcode, payload)
        with self._send_lock:
            self._sock.sendall(frame)

    def send_json(self, obj):
        self.send(json.dumps(obj))

    def recv(self, timeout=None):
        """Return the next text/binary message, or ``None`` once closed."""
        if timeout is not None:
            self._sock.settimeout(timeout)
        while not self.closed:
            decoded = ws_decode_frame(self._buf)
            if decoded is None:
                try:
                    chunk = self._sock.recv(65536)
                except socket.timeout:
                    return None
                if not chunk:
                    self.closed = True
                    return None
                self._buf.extend(chunk)
                continue
            fin, opcode, payload, offset = decoded
            del self._buf[:offset]
            if opcode == WS_PING:
                self.send(payload, WS_PONG)
                continue
            if opcode == WS_PONG:
                continue
            if opcode == WS_CLOSE:
                try:
                    self.send(payload[:2] if len(payload) >= 2 else b"", WS_CLOSE)
                except Exception:
                    pass
                self.closed = True
                return None
            if opcode == WS_CONT:
                self._frag.append(payload)
            else:
                self._frag = [payload]
                self._frag_opcode = opcode
            if not fin:
                continue
            data = b"".join(self._frag)
            self._frag = []
            if self._frag_opcode == WS_TEXT:
                return data.decode("utf-8", "replace")
            return data
        return None

    def recv_json(self, timeout=None):
        message = self.recv(timeout)
        if message is None:
            return None
        try:
            return json.loads(message)
        except Exception:
            return None

    def close(self, code=1000):
        if not self.closed:
            self.closed = True
            try:
                self._sock.sendall(ws_encode_frame(WS_CLOSE, struct.pack("!H", code)))
            except Exception:
                pass
        try:
            self._sock.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Cluster
# --------------------------------------------------------------------------


class Cluster(object):
    """Client for a PhoneCluster coordinator.

    ``url`` and ``pin`` resolve from the arguments, then ``PC_URL`` /
    ``PC_PIN``, then ``data/cli.json`` (written by ``pc login``).
    """

    def __init__(self, url=None, pin=None, timeout=30):
        cfg = None
        if not url or not pin:
            cfg = load_config()
        if not url:
            url = os.environ.get("PC_URL") or cfg.get("url") or DEFAULT_URL
        if not pin:
            pin = os.environ.get("PC_PIN") or cfg.get("pin") or ""
        if str(url).lower() == "auto":
            found = discover()
            if not found:
                raise ClusterError(0, "no coordinator answered the LAN discovery probe")
            url = found
        self.url = normalize_url(url)
        self.pin = str(pin or "")
        self.timeout = timeout
        self.files = Files(self)
        self.storage = Storage(self)
        self.schedules = Schedules(self)
        self.templates = Templates(self)
        self.llm = LLM(self)

    def __repr__(self):
        return "<Cluster %s>" % self.url

    # -- HTTP ------------------------------------------------------------

    def _full_url(self, path, params=None):
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            if not path.startswith("/"):
                path = "/" + path
            url = self.url + path
        if params:
            clean = dict((k, v) for k, v in params.items() if v is not None)
            if clean:
                sep = "&" if "?" in url else "?"
                url += sep + urllib.parse.urlencode(clean)
        return url

    def open(self, method, path, data=None, params=None, headers=None,
             content_type=None, content_length=None, timeout=None):
        """Perform a request and return the raw response object (unclosed)."""
        url = self._full_url(path, params)
        request = urllib.request.Request(url, data=data, method=method.upper())
        if self.pin:
            request.add_header("X-Pin", self.pin)
        request.add_header("Accept", "application/json")
        if content_type:
            request.add_header("Content-Type", content_type)
        if content_length is not None:
            request.add_header("Content-Length", str(content_length))
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        kwargs = {}
        if url.startswith("https://"):
            kwargs["context"] = ssl_context()
        try:
            return urllib.request.urlopen(
                request, timeout=self.timeout if timeout is None else timeout, **kwargs)
        except urllib.error.HTTPError as exc:
            body = b""
            try:
                body = exc.read()
            except Exception:
                pass
            message = exc.reason or "http error"
            try:
                parsed = json.loads(body.decode("utf-8", "replace"))
                if isinstance(parsed, dict) and parsed.get("error"):
                    message = parsed["error"]
                elif isinstance(parsed, dict) and parsed.get("detail"):
                    message = parsed["detail"]
                elif body:
                    message = body.decode("utf-8", "replace")[:500]
            except Exception:
                if body:
                    message = body.decode("utf-8", "replace")[:500]
            raise ClusterError(exc.code, message)
        except urllib.error.URLError as exc:
            raise ClusterError(0, "cannot reach %s: %s" % (self.url, exc.reason))
        except socket.timeout:
            raise ClusterError(0, "timed out talking to %s" % self.url)

    def request(self, method, path, body=None, params=None, timeout=None):
        """Send an optional JSON body, return the parsed JSON response."""
        data = None
        ctype = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            ctype = "application/json"
        response = self.open(method, path, data=data, params=params,
                             content_type=ctype, timeout=timeout)
        try:
            raw = response.read()
        finally:
            response.close()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return raw.decode("utf-8", "replace")

    def get(self, path, params=None, timeout=None):
        return self.request("GET", path, params=params, timeout=timeout)

    def post(self, path, body=None, params=None, timeout=None):
        return self.request("POST", path, body=body if body is not None else {},
                            params=params, timeout=timeout)

    def patch(self, path, body=None, params=None):
        return self.request("PATCH", path, body=body if body is not None else {},
                            params=params)

    def delete(self, path, params=None):
        return self.request("DELETE", path, params=params)

    def download(self, path, dest, params=None, timeout=None):
        """Stream a response body to ``dest`` (a path or a directory)."""
        response = self.open("GET", path, params=params, timeout=timeout)
        try:
            name = None
            disposition = response.headers.get("Content-Disposition") or ""
            if "filename=" in disposition:
                name = disposition.split("filename=", 1)[1].strip().strip('";')
                name = _safe_name(name, "")
            if os.path.isdir(dest):
                dest = os.path.join(dest, name or "download")
            directory = os.path.dirname(os.path.abspath(dest))
            if directory and not os.path.isdir(directory):
                os.makedirs(directory)
            with open(dest, "wb") as fh:
                shutil.copyfileobj(response, fh, 262144)
        finally:
            response.close()
        return dest

    # -- info / misc -----------------------------------------------------

    def info(self):
        return self.get("/api/info")

    def join(self):
        """A fresh one-time join code (``POST /api/join/code``): ``code``,
        ``expires``, ``install_cmd``, ``browser_url``, ``manual_cmd``,
        ``ca_fingerprint``.  Works only on the PC itself (SPEC 3)."""
        return self.post("/api/join/code", {})

    def stats(self):
        return self.get("/api/stats")

    def speeds(self):
        """What the cluster learned: ``[{key, device, rate, samples}]`` (units/s)."""
        return _as_list(self.get("/api/speeds"), "speeds")

    def events(self, limit=50, kind=None):
        return _as_list(self.get("/api/events", {"limit": limit, "kind": kind}),
                        "events")

    def plugins(self):
        return _as_list(self.get("/api/plugins"), "plugins")

    def settings(self):
        return self.get("/api/settings")

    def update_settings(self, values):
        return self.patch("/api/settings", values)

    def login(self):
        """Validate the PIN (``POST /api/login``); raises on a bad PIN."""
        return self.post("/api/login", {"pin": self.pin})

    # -- devices ---------------------------------------------------------

    def devices(self):
        return _as_list(self.get("/api/devices"), "devices")

    def device(self, device_id):
        return self.get("/api/devices/%s" % device_id)

    def update_device(self, device_id, **fields):
        return self.patch("/api/devices/%s" % device_id, fields)

    def remove_device(self, device_id):
        return self.delete("/api/devices/%s" % device_id)

    def pause_device(self, device_id, paused=True):
        return self.update_device(device_id, paused=bool(paused))

    def rename_device(self, device_id, name):
        return self.update_device(device_id, name=name)

    def device_controls(self, device_id, on=None, jobs=None, ai=None, storage=None,
                        cpu_pct=None, mem_pct=None, gpu_pct=None):
        """What a connected device is used for and how hard it may work.

        ``on``: the master switch (False = used for nothing).  ``jobs`` /
        ``ai`` / ``storage``: roles.  ``cpu_pct``, ``mem_pct``, ``gpu_pct``:
        limits, capped by the Balanced safety profile.  Returns the device
        with its ``control`` block (limits, safety state and notes).  The
        device may be given by id or by name."""
        device_id = self.llm.resolve([device_id])[0]
        fields = {}
        if on is not None:
            fields["paused"] = not on
        settings = {}
        roles = dict((k, bool(v)) for k, v in (("jobs", jobs), ("ai", ai), ("storage", storage))
                     if v is not None)
        if roles:
            settings["roles"] = roles
        for key, value in (("cpu_pct", cpu_pct), ("mem_pct", mem_pct), ("gpu_pct", gpu_pct)):
            if value is not None:
                settings[key] = value
        if settings:
            fields["settings"] = settings
        return self.update_device(device_id, **fields)

    def notify(self, device_id, title, body):
        return self.post("/api/devices/%s/notify" % device_id,
                         {"title": title, "body": body})

    def bench(self, device_id=None):
        """Benchmark one device (returns the API response) or all of them
        (submits a ``benchmark`` job with ``targets: "all"`` and returns it)."""
        if device_id:
            return self.post("/api/devices/%s/benchmark" % device_id)
        return self.submit("benchmark", {}, targets="all", name="benchmark")

    # -- jobs ------------------------------------------------------------

    def submit(self, type, params=None, name=None, priority=0, depends_on=None,
               timeout=None, max_attempts=None, requires=None, targets=None,
               chunk_size=None, fail_fast=None, options=None, wait=False,
               stream_results=None, cache=None, verify=None):
        """Submit any plugin job (SPEC section 5) and return a :class:`Job`.

        ``cache``: reuse stored results of identical work (default for
        repeatable types), ``False`` to compute anyway, ``"refresh"`` to
        compute and replace them (SPEC 9.8).  ``verify``: ``False``, or the
        share of tasks a second device runs again to catch wrong answers
        (SPEC 9.9)."""
        spec = {"type": type, "params": params or {}}
        if name:
            spec["name"] = name
        if priority:
            spec["priority"] = priority
        if depends_on:
            spec["depends_on"] = list(depends_on)
        opts = dict(options or {})
        for key, value in (("timeout", timeout), ("max_attempts", max_attempts),
                           ("requires", requires), ("targets", targets),
                           ("chunk_size", chunk_size), ("fail_fast", fail_fast),
                           ("stream_results", stream_results or None),
                           ("cache", cache), ("verify", verify)):
            if value is not None:
                opts[key] = value
        if opts:
            spec["options"] = opts
        data = self.post("/api/jobs", spec)
        job = Job(self, data)
        if wait:
            job.wait()
        return job

    def submit_spec(self, spec, wait=False):
        """Submit a raw job spec dict."""
        job = Job(self, self.post("/api/jobs", spec))
        if wait:
            job.wait()
        return job

    def jobs(self, status=None, limit=None, offset=None):
        data = self.get("/api/jobs",
                        {"status": status, "limit": limit, "offset": offset})
        return [Job(self, item) for item in _as_list(data, "jobs")]

    def job(self, job_id):
        return Job(self, self.get("/api/jobs/%s" % job_id))

    def task(self, task_id):
        return self.get("/api/tasks/%s" % task_id)

    def task_log(self, task_id, tail=None):
        data = self.get("/api/tasks/%s/log" % task_id, {"tail": tail})
        if isinstance(data, str):
            return data
        if isinstance(data, list):
            return "\n".join(str(x) for x in data)
        if isinstance(data, dict):
            lines = data.get("lines")
            if isinstance(lines, list):
                return "\n".join(str(x) for x in lines)
            if isinstance(lines, str):
                return lines
            for key in ("log", "text", "content"):
                if isinstance(data.get(key), str):
                    return data[key]
        return ""

    def cancel_task(self, task_id):
        return self.post("/api/tasks/%s/cancel" % task_id)

    # -- high level ------------------------------------------------------

    def map(self, fn, items, chunk_size=None, name=None, js_fn=None, inputs=None,
            timeout=600, priority=0, max_attempts=3, wait=False,
            stream_results=False, cache=None, verify=None):
        """Run ``fn(item, ctx)`` over ``items`` on the cluster.

        ``fn`` must be a top-level function in a real file; its source is
        shipped with :func:`inspect.getsource`.  Without ``chunk_size`` the
        cluster sizes each device's pieces to its speed as it goes (SPEC
        9.6).  With ``stream_results=True`` results are written to a JSONL
        file as tasks finish (read them with ``job.iter_results()``) instead
        of being collected in memory.
        """
        if not callable(fn):
            raise TypeError("map() needs a function, got %r" % (fn,))
        items = list(items)
        params = {
            "code": function_source(fn),
            "fn_name": getattr(fn, "__name__", "fn"),
        }
        if chunk_size:
            params["chunk_size"] = int(chunk_size)
        encoded = json.dumps(items).encode("utf-8")
        if len(encoded) > INLINE_ITEMS_LIMIT:
            params["items_file"] = self.files.upload_bytes(encoded, "items.json")
        else:
            params["items"] = items
        if js_fn is not None:
            params["js_code"] = self._js_source(js_fn)
        if inputs:
            params["inputs"] = list(inputs)
        return self.submit(
            "python_map", params, name=name or ("map %s" % params["fn_name"]),
            priority=priority, timeout=timeout, max_attempts=max_attempts,
            chunk_size=chunk_size, wait=wait, stream_results=stream_results,
            cache=cache, verify=verify)

    def map_lines(self, fn, path_or_file_id, lines_per_task=None, line_format="auto",
                  name=None, js_fn=None, inputs=None, timeout=600, priority=0,
                  max_attempts=3, wait=False, stream_results=True):
        """Run ``fn(line, ctx)`` over every line of a big file (SPEC 6.6).

        ``path_or_file_id`` is a local path (uploaded for you) or a file id
        already in the store.  The coordinator never loads the file: tasks
        fetch their own byte range.  ``line_format`` is ``text``, ``json``
        (one JSON value per line, e.g. ``.jsonl``) or ``auto`` by extension.
        Without ``lines_per_task`` pieces are sized to each device's speed.
        Results stream to a JSONL file by default.
        """
        if not callable(fn):
            raise TypeError("map_lines() needs a function, got %r" % (fn,))
        file_id = str(path_or_file_id)
        if os.path.isfile(file_id):
            file_id = self.files.upload(file_id)
        params = {
            "code": function_source(fn),
            "fn_name": getattr(fn, "__name__", "fn"),
            "lines_file": file_id,
            "line_format": line_format,
        }
        if lines_per_task:
            params["lines_per_task"] = int(lines_per_task)
        if js_fn is not None:
            params["js_code"] = self._js_source(js_fn)
        if inputs:
            params["inputs"] = list(inputs)
        return self.submit(
            "python_map", params, name=name or ("map lines %s" % params["fn_name"]),
            priority=priority, timeout=timeout, max_attempts=max_attempts,
            wait=wait, stream_results=stream_results)

    @staticmethod
    def _js_source(js_fn):
        if callable(js_fn):
            return function_source(js_fn)
        text = str(js_fn)
        if "\n" not in text and text.endswith(".js") and os.path.isfile(text):
            with open(text, "r") as fh:
                return fh.read()
        return text

    def wasm(self, module, call=None, range=None, args_list=None, inputs=None, stdin=None,
             env=None, combine="auto", grain=None, memory_mb=None, name=None, timeout=600,
             priority=0, wait=False, cache=None, verify=None):
        """Run one WebAssembly module on every kind of device (SPEC 6.9).

        ``module``: a ``.wasm`` path (uploaded for you) or a file id.  With
        ``call`` an exported function is called with numbers: per entry of
        ``args_list``, or as ``f(lo, hi)`` over pieces of ``range=(start,
        end)`` sized to each device (results added up).  Without ``call`` the
        module runs as a WASI program: ``args_list`` gives command lines,
        ``stdin`` a text, and each of ``inputs`` (paths or file ids) becomes
        one task reading that file on stdin and at ``/in/<name>``.
        """
        module_id = self._file_ref(module)
        params = {"module": module_id, "combine": combine}
        if call:
            params["call"] = call
        if range is not None:
            params["range"] = [int(range[0]), int(range[1])]
        if args_list is not None:
            params["args_list"] = [list(a) if isinstance(a, (list, tuple)) else [a] for a in args_list]
        if inputs:
            params["inputs"] = [self._file_ref(i) for i in inputs]
        if stdin is not None:
            params["stdin"] = stdin
        if env:
            params["env"] = dict(env)
        if grain:
            params["grain"] = int(grain)
        if memory_mb:
            params["memory_mb"] = int(memory_mb)
        label = call or os.path.basename(str(module))
        return self.submit("wasm", params, name=name or ("wasm %s" % label), priority=priority,
                           timeout=timeout, wait=wait, cache=cache, verify=verify)

    def _file_ref(self, value):
        """A local path is uploaded; anything else is taken as a file id."""
        text = str(value)
        if os.path.isfile(text):
            return self.files.upload(text)
        return text

    def rerun(self, job_id, fresh=False, wait=False):
        """Submit a job again.  ``fresh=True`` ignores stored results and
        replaces them ("Run again fresh", SPEC 9.8)."""
        job = Job(self, self.post("/api/jobs/%s/rerun" % job_id, {"fresh": bool(fresh)}))
        if wait:
            job.wait()
        return job

    def cache_info(self, limit=50):
        """Stored results: ``{stats: {entries, hits, saved_s, ...}, entries: [...]}``."""
        return self.get("/api/cache", {"limit": limit})

    def clear_cache(self):
        return self.delete("/api/cache")

    def trust(self):
        """Per device: how its checked answers went (SPEC 9.9)."""
        return _as_list(self.get("/api/trust"), "devices")

    def trust_again(self, device):
        """Forgive a device that gave wrong answers; it takes jobs again."""
        device_id = self.llm.resolve([device])[0]
        return self.post("/api/devices/%s/trust" % device_id, {"reset": True})

    def shell(self, cmd, targets="any", timeout=600, cmd_windows=None,
              name=None, priority=0, wait=False):
        """Run a shell command on one device (``any``), all of them, or a list."""
        params = {"cmd": cmd, "timeout": timeout}
        if cmd_windows:
            params["cmd_windows"] = cmd_windows
        return self.submit("shell", params, name=name or ("shell: %s" % cmd),
                           targets=targets, timeout=timeout, priority=priority,
                           wait=wait)

    def pipeline(self, jobs, name=None):
        """Submit several dependent jobs at once (SPEC 5.1)."""
        specs = []
        for index, spec in enumerate(jobs):
            spec = dict(spec)
            spec.setdefault("key", "job%d" % index)
            specs.append(spec)
        body = {"jobs": specs}
        if name:
            body["name"] = name
        return Pipeline(self, self.post("/api/pipelines", body), specs)

    # -- services (SPEC 6.7) ---------------------------------------------

    def services(self):
        """Running services with their instances (``GET /api/services``)."""
        return _as_list(self.get("/api/services"), "services")

    def call(self, service, data=None, path=None, text=None, json_body=None,
             params=None, content_type=None, name=None, timeout=None,
             queue_timeout=None, on_partial=None, request_id=None):
        """Send one request to a running service and return its answer.

        Give the input as ``path`` (streamed from disk), ``data`` (bytes),
        ``text`` or ``json_body``.  ``on_partial(value)`` is called for every
        intermediate result while the request runs (watched on a second
        connection).  Returns the request dict: ``result``, ``outputs``
        (files to download), ``partials``, ``ms``, ``device_id``.  Raises
        :class:`ClusterError` when the service fails or is not running.
        """
        if sum(x is not None for x in (data, path, text, json_body)) != 1:
            raise PhoneClusterError("call() needs exactly one of data, path, text, json_body")
        body = None
        length = None
        if path is not None:
            body = _FileBody(path)
            length = body.content_length
            content_type = content_type or (mimetypes.guess_type(path)[0] or "application/octet-stream")
            name = name or os.path.basename(path)
        elif data is not None:
            body = data if isinstance(data, (bytes, bytearray)) else bytes(data)
            length = len(body)
            content_type = content_type or "application/octet-stream"
        elif text is not None:
            body = str(text).encode("utf-8")
            length = len(body)
            content_type = content_type or "text/plain; charset=utf-8"
        else:
            body = json.dumps(json_body).encode("utf-8")
            length = len(body)
            content_type = content_type or "application/json"
        rid = request_id or ("c" + os.urandom(6).hex())
        query = {"request_id": rid}
        if params:
            query["params"] = json.dumps(params)
        if timeout is not None:
            query["timeout"] = timeout
        if queue_timeout is not None:
            query["queue_timeout"] = queue_timeout
        headers = {}
        if name:
            headers["X-File-Name"] = str(name)
        watcher = None
        stop = threading.Event()
        if on_partial is not None:
            watcher = threading.Thread(target=self._watch_partials, args=(rid, on_partial, stop))
            watcher.daemon = True
            watcher.start()
        try:
            try:
                response = self.open("POST", "/api/services/%s/call" % urllib.parse.quote(service),
                                     data=body, params=query, headers=headers,
                                     content_type=content_type, content_length=length,
                                     timeout=(timeout or 300) + 60)
            except ClusterError as exc:
                # the coordinator returns the request dict with a 502/504
                raise ClusterError(exc.status, exc.message)
            try:
                raw = response.read()
            finally:
                response.close()
        finally:
            stop.set()
            if hasattr(body, "close"):
                body.close()
            if watcher is not None:
                watcher.join(timeout=5)
        try:
            result = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ClusterError(0, "unexpected response from the service call")
        return result

    def _watch_partials(self, rid, on_partial, stop):
        since = 0
        misses = 0
        # the request may not exist for a moment before the upload starts;
        # after the call returns, one last fetch picks up anything missed
        while True:
            try:
                page = self.get("/api/requests/%s/partials" % rid,
                                {"since": since, "wait": 0 if stop.is_set() else 5})
            except ClusterError as exc:
                if exc.status == 404 and (not stop.is_set() or misses < 3):
                    misses += 1
                    time.sleep(0.1)
                    continue
                return
            fresh = page.get("partials") or []
            for item in fresh:
                try:
                    on_partial(item)
                except Exception:
                    pass
            since = int(page.get("next") or since)
            if page.get("status") in ("done", "failed", "cancelled"):
                return
            if stop.is_set() and not fresh:
                return

    def requests(self, limit=50):
        return _as_list(self.get("/api/requests", {"limit": limit}), "requests")

    def service_request(self, request_id):
        """One service request: status, partials, result, outputs."""
        return self.get("/api/requests/%s" % request_id)

    def cancel_request(self, request_id):
        return self.post("/api/requests/%s/cancel" % request_id, {})

    def start_service(self, name, code, inputs=None, setup_cmd=None, description="",
                      accepts="", produces="", streaming_output=False, request_timeout=300,
                      params=None, targets=None, job_name=None):
        """Host a model: submit a ``service`` job (SPEC 6.7). Returns the Job.

        ``code`` is Python source (or a ``.py`` path) defining ``load(ctx)``
        and ``serve(request, ctx)``.  ``targets`` pins it to device ids, or
        ``"all"`` runs one instance on every capable device.
        """
        if "\n" not in str(code) and str(code).endswith(".py") and os.path.isfile(code):
            with open(code, "r") as fh:
                code = fh.read()
        spec_params = {
            "name": name, "code": code, "description": description, "accepts": accepts,
            "produces": produces, "streaming_output": bool(streaming_output),
            "request_timeout": int(request_timeout), "params": params or {},
        }
        if setup_cmd:
            spec_params["setup_cmd"] = setup_cmd
        if inputs:
            spec_params["inputs"] = list(inputs)
        return self.submit("service", spec_params, name=job_name or ("service: %s" % name),
                           targets=targets)

    def stop_service(self, name):
        """Cancel every job hosting ``name``; returns the cancelled job ids."""
        cancelled = []
        for svc in self.services():
            if svc.get("name") != name:
                continue
            for inst in svc.get("instances") or []:
                job_id = inst.get("job_id")
                if job_id and job_id not in cancelled:
                    self.post("/api/jobs/%s/cancel" % job_id, {})
                    cancelled.append(job_id)
        return cancelled

    # -- packages --------------------------------------------------------

    def run_package(self, path, dry_run=False, wait=False):
        """Submit a job package: a folder or ``.zip`` holding ``job.yaml``
        (or ``job.json``) plus the code and data files it references.

        A folder is zipped in memory first (``__pycache__``, ``.git`` and
        ``*.pyc`` are skipped).  Returns a :class:`Job` or :class:`Pipeline`;
        with ``dry_run=True`` returns the resolved spec dict instead.  See
        docs/PACKAGES.md for the manifest format.
        """
        data, name = package_bytes(path)
        body = _MultipartBody(None, [("file", name, "application/zip", data)])
        params = {"dry_run": "true"} if dry_run else None
        try:
            response = self.open(
                "POST", "/api/packages", data=body, params=params,
                content_type=body.content_type, content_length=body.content_length)
            try:
                raw = response.read()
            finally:
                response.close()
        finally:
            body.close()
        try:
            result = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ClusterError(0, "unexpected response from /api/packages")
        if dry_run:
            return result
        if result.get("kind") == "pipeline":
            pipeline = Pipeline(self, result)
            return pipeline.wait() if wait else pipeline
        job = Job(self, result.get("job") or {"id": result.get("job_id")})
        return job.wait() if wait else job

    # -- terminal --------------------------------------------------------

    def open_terminal(self, device_id):
        """``POST /api/terminal`` -> session id."""
        data = self.post("/api/terminal", {"device_id": device_id})
        session = data.get("session_id") or data.get("id") if isinstance(data, dict) else None
        if not session:
            raise ClusterError(0, "coordinator did not return a session_id")
        return session

    def close_terminal(self, session_id):
        try:
            return self.delete("/api/terminal/%s" % session_id)
        except ClusterError:
            return {}

    def dashboard_socket(self, timeout=30):
        """Connect to ``/ws/dashboard`` and return a :class:`WebSocket`."""
        parts = urllib.parse.urlsplit(self.url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        url = "%s://%s/ws/dashboard" % (scheme, parts.netloc)
        if self.pin:
            url += "?" + urllib.parse.urlencode({"pin": self.pin})
        return WebSocket(url, timeout=timeout)

    def terminal(self, device_id, stdin=None, stdout=None):
        """Interactive line terminal on ``device_id``. Blocks until closed."""
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        session = self.open_terminal(device_id)
        socket_ = self.dashboard_socket()
        stop = threading.Event()

        def pump():
            # EOF on stdin only stops sending; output keeps streaming until
            # the remote shell closes the session or the user hits Ctrl+C.
            try:
                while not stop.is_set():
                    line = stdin.readline()
                    if not line:
                        return
                    socket_.send_json({"op": "term_input", "session_id": session,
                                       "data": line})
            except Exception:
                return

        reader = threading.Thread(target=pump)
        reader.daemon = True
        reader.start()
        stdout.write("connected to %s (session %s). Ctrl+C to exit.\n"
                     % (device_id, session))
        stdout.flush()
        code = 0
        try:
            while True:
                message = socket_.recv_json(timeout=1.0)
                if message is None:
                    if socket_.closed:
                        break
                    continue
                event = message.get("ev")
                if message.get("session_id") not in (None, session):
                    continue
                if event == "term":
                    stdout.write(message.get("data") or "")
                    stdout.flush()
                elif event == "term_closed":
                    code = message.get("exit_code") or 0
                    stdout.write("\n[session closed, exit code %s]\n" % code)
                    stdout.flush()
                    break
        except KeyboardInterrupt:
            stdout.write("\n")
        finally:
            stop.set()
            socket_.close()
            self.close_terminal(session)
        return code


# --------------------------------------------------------------------------
# Job / Pipeline
# --------------------------------------------------------------------------


class Job(object):
    """A submitted job. ``.data`` holds the last server payload."""

    def __init__(self, cluster, data):
        self.cluster = cluster
        if isinstance(data, str):
            data = {"id": data}
        self.data = dict(data or {})
        job_id = self.data.get("id") or self.data.get("job_id")
        if not job_id:
            raise ClusterError(0, "coordinator did not return a job id: %r" % (data,))
        self.id = job_id

    def __repr__(self):
        return "<Job %s %s %s>" % (self.id, self.data.get("type"), self.status)

    @property
    def status(self):
        return self.data.get("status")

    @property
    def name(self):
        return self.data.get("name")

    @property
    def type(self):
        return self.data.get("type")

    @property
    def error(self):
        return self.data.get("error")

    @property
    def done(self):
        return self.status in TERMINAL_STATES

    @property
    def progress(self):
        units = self.data.get("progress_units")
        if isinstance(units, dict) and units.get("units"):
            # automatically sized job: pieces are cut as it goes, count the work
            if self.data.get("status") == "done":
                return 1.0
            return float(units.get("done") or 0) / float(units["units"])
        total = self.data.get("total_tasks") or 0
        finished = (self.data.get("done_tasks") or 0) + (self.data.get("failed_tasks") or 0)
        return (float(finished) / total) if total else 0.0

    def refresh(self):
        data = self.cluster.get("/api/jobs/%s" % self.id)
        if isinstance(data, dict) and data:
            if "job" in data and isinstance(data["job"], dict):
                data = data["job"]
            self.data = data
        return self

    def wait(self, poll=1.0, timeout=None):
        """Block until the job finishes. Raises :class:`JobFailed` on failure."""
        deadline = None if timeout is None else time.time() + timeout
        while True:
            self.refresh()
            if self.status in ("failed", "cancelled"):
                raise JobFailed(self, self.error)
            if self.status == "done":
                return self
            if deadline is not None and time.time() >= deadline:
                raise TimeoutError(
                    "job %s still %s after %ss" % (self.id, self.status, timeout))
            time.sleep(poll)

    def result(self):
        return _unwrap_result(self.cluster.get("/api/jobs/%s/result" % self.id))

    def tasks(self):
        return _as_list(self.cluster.get("/api/jobs/%s/tasks" % self.id), "tasks")

    def logs(self, task_idx=None):
        """Log text for one task index, or every task with ``[task N]`` headers."""
        tasks = self.tasks()
        if task_idx is not None:
            for task in tasks:
                if task.get("idx") == task_idx:
                    return self.cluster.task_log(task.get("id"))
            raise ClusterError(0, "job %s has no task with idx %s"
                               % (self.id, task_idx))
        chunks = []
        for task in sorted(tasks, key=lambda t: t.get("idx") or 0):
            text = self.cluster.task_log(task.get("id"))
            if text:
                chunks.append("[task %s] %s\n%s" % (task.get("idx"), task.get("id"), text))
        return "\n".join(chunks)

    def cancel(self):
        self.cluster.post("/api/jobs/%s/cancel" % self.id)
        return self.refresh()

    def retry(self):
        self.cluster.post("/api/jobs/%s/retry" % self.id)
        return self.refresh()

    def rerun(self, fresh=False, wait=False):
        """The same job again (a new job); ``fresh`` computes everything anew."""
        return self.cluster.rerun(self.id, fresh=fresh, wait=wait)

    @property
    def cached(self):
        """Answered by an identical earlier job instead of running (SPEC 9.8)."""
        return bool(self.data.get("cached"))

    def delete(self):
        return self.cluster.delete("/api/jobs/%s" % self.id)

    def export(self, path, format="zip"):
        """Download the job export (``zip`` | ``json`` | ``csv``) to ``path``."""
        if os.path.isdir(path):
            path = os.path.join(path, "%s.%s" % (self.id, format))
        return self.cluster.download("/api/jobs/%s/export" % self.id, path,
                                     {"format": format})

    def iter_results(self, wait=False, poll=1.0, timeout=None):
        """Yield the job's items one by one from ``GET /api/jobs/{id}/results``
        (JSON lines, SPEC 6.6) without loading them all into memory.

        With ``wait=True`` the job is awaited first so every item is there;
        otherwise a running streamed job yields what has been written so far.
        """
        if wait:
            self.wait(poll=poll, timeout=timeout)
        response = self.cluster.open("GET", "/api/jobs/%s/results" % self.id)
        try:
            if response.status == 204:
                return
            buf = b""
            while True:
                block = response.read(65536)
                if not block:
                    break
                buf += block
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        yield json.loads(line.decode("utf-8"))
            if buf.strip():
                yield json.loads(buf.decode("utf-8"))
        finally:
            response.close()

    def download_results(self, path, wait=True):
        """Save the JSONL results stream to ``path``; returns the path."""
        if wait:
            self.wait()
        return self.cluster.download("/api/jobs/%s/results" % self.id, path)

    def outputs(self):
        """Every output file produced by this job's tasks."""
        found = []
        for task in sorted(self.tasks(), key=lambda t: t.get("idx") or 0):
            for output in (task.get("outputs") or []):
                if not isinstance(output, dict):
                    continue
                found.append({
                    "file_id": output.get("file_id") or output.get("id"),
                    "name": output.get("name"),
                    "size": output.get("size"),
                    "task_idx": task.get("idx"),
                })
        return found

    def download_outputs(self, dest_dir):
        """Download every output into ``dest_dir``; returns the written paths."""
        if not os.path.isdir(dest_dir):
            os.makedirs(dest_dir)
        written = []
        used = set()
        for output in self.outputs():
            name = _safe_name(output.get("name"), output["file_id"])
            if name in used:
                stem, ext = os.path.splitext(name)
                name = "%s_%s%s" % (stem, output.get("task_idx"), ext)
            used.add(name)
            written.append(self.cluster.files.download(
                output["file_id"], os.path.join(dest_dir, name)))
        return written


class Pipeline(object):
    """The result of :meth:`Cluster.pipeline`: ``.jobs`` maps key -> Job."""

    def __init__(self, cluster, data, specs=None):
        self.cluster = cluster
        self.data = dict(data or {})
        self.id = self.data.get("pipeline_id") or self.data.get("id")
        self.specs = specs or []
        self.jobs = {}
        mapping = self.data.get("jobs") or {}
        if isinstance(mapping, dict):
            for key, job_id in mapping.items():
                self.jobs[key] = Job(cluster, {"id": job_id})
        elif isinstance(mapping, list):
            for index, item in enumerate(mapping):
                if isinstance(item, dict):
                    key = item.get("key") or "job%d" % index
                    self.jobs[key] = Job(cluster, item)

    def __repr__(self):
        return "<Pipeline %s jobs=%s>" % (self.id, sorted(self.jobs))

    def __getitem__(self, key):
        return self.jobs[key]

    def wait(self, poll=1.0, timeout=None):
        deadline = None if timeout is None else time.time() + timeout
        for key in self._order():
            remaining = None if deadline is None else max(0.0, deadline - time.time())
            self.jobs[key].wait(poll=poll, timeout=remaining)
        return self

    def results(self):
        return dict((key, job.result()) for key, job in self.jobs.items())

    def statuses(self):
        return dict((key, job.refresh().status) for key, job in self.jobs.items())

    def _order(self):
        keys = [s.get("key") for s in self.specs if s.get("key") in self.jobs]
        for key in sorted(self.jobs):
            if key not in keys:
                keys.append(key)
        return keys


# --------------------------------------------------------------------------
# namespaces
# --------------------------------------------------------------------------


class Files(object):
    """``c.files`` — the content-addressed file store."""

    def __init__(self, cluster):
        self.cluster = cluster

    def list(self, limit=None, offset=None):
        return _as_list(self.cluster.get("/api/files",
                                         {"limit": limit, "offset": offset}), "files")

    def upload(self, path, name=None):
        """Raw ``PUT /api/files?name=``, streamed in a single request."""
        name = name or os.path.basename(path)
        body = _FileBody(path)
        try:
            response = self.cluster.open(
                "PUT", "/api/files", data=body, params={"name": name},
                content_type="application/octet-stream",
                content_length=body.content_length)
            try:
                raw = response.read()
            finally:
                response.close()
        finally:
            body.close()
        return self._file_id(raw)

    def upload_bytes(self, data, name):
        if isinstance(data, str):
            data = data.encode("utf-8")
        response = self.cluster.open(
            "PUT", "/api/files", data=data, params={"name": name},
            content_type="application/octet-stream", content_length=len(data))
        try:
            raw = response.read()
        finally:
            response.close()
        return self._file_id(raw)

    @staticmethod
    def _file_id(raw):
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ClusterError(0, "upload: unexpected response %r" % raw[:200])
        file_id = data.get("file_id") or data.get("id")
        if not file_id:
            raise ClusterError(0, "upload: no file_id in %r" % (data,))
        return file_id

    def meta(self, file_id):
        return self.cluster.get("/api/files/%s" % file_id)

    def download(self, file_id, dest):
        return self.cluster.download("/api/files/%s/download" % file_id, dest)

    def delete(self, file_id):
        return self.cluster.delete("/api/files/%s" % file_id)


class Storage(object):
    """``c.storage`` — blobs pooled across the phones."""

    def __init__(self, cluster):
        self.cluster = cluster

    def list(self, limit=None, offset=None):
        return _as_list(self.cluster.get("/api/storage",
                                         {"limit": limit, "offset": offset}), "blobs")

    def put(self, path, replication=None, name=None, scheme=None):
        """Multipart ``POST /api/storage`` -> blob id.

        ``scheme``: ``auto`` (erasure coding from three storage phones on,
        else copies), ``copies`` or ``ec``.  ``replication`` alone asks for
        that many copies."""
        name = name or os.path.basename(path)
        fields = {}
        if replication is not None:
            fields["replication"] = str(replication)
        if scheme:
            fields["scheme"] = str(scheme)
        fields["name"] = name
        body = _MultipartBody(fields, [("file", name, "application/octet-stream", path)])
        try:
            response = self.cluster.open(
                "POST", "/api/storage", data=body,
                content_type=body.content_type,
                content_length=body.content_length)
            try:
                raw = response.read()
            finally:
                response.close()
        finally:
            body.close()
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ClusterError(0, "storage.put: unexpected response %r" % raw[:200])
        blob_id = data.get("blob_id") or data.get("id")
        if not blob_id:
            raise ClusterError(0, "storage.put: no blob id in %r" % (data,))
        return blob_id

    def put_file_id(self, file_id, replication=None, scheme=None):
        """Store an already-uploaded file id into the pool."""
        body = {"file_id": file_id}
        if replication is not None:
            body["replication"] = replication
        if scheme:
            body["scheme"] = scheme
        data = self.cluster.post("/api/storage", body)
        return data.get("blob_id") or data.get("id")

    def get(self, blob_id, dest):
        return self.cluster.download("/api/storage/%s" % blob_id, dest)

    def status(self, blob_id):
        return self.cluster.get("/api/storage/%s/status" % blob_id)

    def delete(self, blob_id):
        return self.cluster.delete("/api/storage/%s" % blob_id)


class Schedules(object):
    """``c.schedules`` — cron-driven job submission."""

    def __init__(self, cluster):
        self.cluster = cluster

    def list(self):
        return _as_list(self.cluster.get("/api/schedules"), "schedules")

    def create(self, name, cron, job_spec, enabled=True):
        return self.cluster.post("/api/schedules", {
            "name": name, "cron": cron, "job_spec": job_spec,
            "enabled": bool(enabled)})

    def update(self, schedule_id, **fields):
        return self.cluster.patch("/api/schedules/%s" % schedule_id, fields)

    def delete(self, schedule_id):
        return self.cluster.delete("/api/schedules/%s" % schedule_id)

    def run(self, schedule_id):
        return self.cluster.post("/api/schedules/%s/run" % schedule_id)


class Templates(object):
    """``c.templates`` — saved job specs."""

    def __init__(self, cluster):
        self.cluster = cluster

    def list(self):
        return _as_list(self.cluster.get("/api/templates"), "templates")

    def create(self, name, job_spec):
        return self.cluster.post("/api/templates", {"name": name, "job_spec": job_spec})

    def delete(self, template_id):
        return self.cluster.delete("/api/templates/%s" % template_id)


class LLM(object):
    """``c.llm`` — run one GGUF model across chosen devices (SPEC 6.8).

    Devices may be given by id or by name everywhere.
    """

    ACTIVE = ("starting", "loading", "ready", "degraded")

    def __init__(self, cluster):
        self.cluster = cluster

    # -- models ----------------------------------------------------------
    def models(self):
        return _as_list(self.cluster.get("/api/llm/models"), "models")

    def upload_model(self, path, name=None):
        """Stream a local ``.gguf`` to the coordinator's models folder."""
        name = name or os.path.basename(path)
        body = _FileBody(path)
        try:
            response = self.cluster.open(
                "PUT", "/api/llm/models", data=body, params={"name": name},
                content_type="application/octet-stream", content_length=body.content_length,
                timeout=3600)
            try:
                raw = response.read()
            finally:
                response.close()
        finally:
            body.close()
        return json.loads(raw.decode("utf-8"))

    def fetch_model(self, url, name=None):
        """Have the coordinator download a model from ``url`` (runs in the background)."""
        return self.cluster.post("/api/llm/models/fetch", {"url": url, "name": name})

    def delete_model(self, name):
        return self.cluster.delete("/api/llm/models/%s" % urllib.parse.quote(name))

    def runtime(self):
        return self.cluster.get("/api/llm/runtime")

    # -- devices and plans ---------------------------------------------
    def devices(self):
        return _as_list(self.cluster.get("/api/llm/devices"), "devices")

    def resolve(self, refs):
        """Device ids for a list of ids or names."""
        devices = self.devices()
        by_name = dict((d.get("name"), d["id"]) for d in devices)
        ids = set(d["id"] for d in devices)
        out = []
        for ref in refs or []:
            ref = str(ref)
            if ref in ids:
                out.append(ref)
            elif ref in by_name:
                out.append(by_name[ref])
            else:
                raise PhoneClusterError("no device called %r (see c.llm.devices())" % ref)
        return out

    def _weights(self, weights):
        if not weights:
            return {}
        keys = list(weights.keys())
        return dict(zip(self.resolve(keys), [float(weights[k]) for k in keys]))

    def plan(self, model, devices=None, host=None, weights=None, ctx=2048, mode=None):
        """Preview where ``model`` would run.  ``mode="auto"`` (the default
        without ``host``/``weights``) picks among ``devices`` (None: any);
        ``mode="manual"`` uses exactly ``devices``."""
        body = {"model": model, "devices": self.resolve(devices),
                "host": self.resolve([host])[0] if host else None,
                "weights": self._weights(weights), "ctx": int(ctx), "mode": mode}
        return self.cluster.post("/api/llm/plan", body)

    # -- deployments -----------------------------------------------------
    def keep(self, key, keep=True):
        """Keep a model loaded, or let it unload after ``llm.idle_minutes`` unused."""
        return self.cluster.post("/api/llm/deployments/%s/keep" % urllib.parse.quote(key), {"keep": bool(keep)})

    def default(self):
        """``{model, name}`` that the name ``chat`` reaches."""
        return self.cluster.get("/api/llm/default")

    def set_default(self, model):
        return self.cluster.request("PUT", "/api/llm/default", body={"model": model})

    def deploy(self, name, model, devices=None, host=None, weights=None, ctx=2048, threads=0,
               force=False, wait=False, timeout=None, mode=None, idle_minutes=None):
        """Run ``model``.  Automatic placement (no ``host``/``weights``): alone
        on the fastest device that holds it (the PC's GPU first), split over
        the fewest fast devices when it is too big; ``devices`` narrows the
        choice.  Manual (``host`` and/or ``weights``, or ``mode="manual"``):
        layers spread over exactly ``devices``, ``host`` keeps the model file
        and serves chats.  With ``wait=True`` blocks until the model is ready."""
        body = {"name": name, "model": model, "devices": self.resolve(devices),
                "host": self.resolve([host])[0] if host else None,
                "weights": self._weights(weights), "ctx": int(ctx), "threads": int(threads),
                "force": bool(force), "mode": mode, "idle_minutes": idle_minutes}
        dep = self.cluster.post("/api/llm/deployments", body)
        return self.wait_ready(dep["id"], timeout=timeout) if wait else dep

    def deployments(self):
        return _as_list(self.cluster.get("/api/llm/deployments"), "deployments")

    def deployment(self, key):
        return self.cluster.get("/api/llm/deployments/%s" % urllib.parse.quote(key))

    def wait_ready(self, key, timeout=None, poll=2.0):
        """Block until the deployment is ready; raises on failure or timeout."""
        deadline = None if timeout is None else time.time() + timeout
        while True:
            dep = self.deployment(key)
            if dep["status"] == "ready":
                return dep
            if dep["status"] in ("failed", "stopped"):
                raise PhoneClusterError("deployment %s %s: %s" % (dep["name"], dep["status"], dep.get("error")))
            if deadline is not None and time.time() > deadline:
                raise TimeoutError("deployment %s still %s" % (dep["name"], dep["status"]))
            time.sleep(poll)

    def stop(self, key):
        return self.cluster.post("/api/llm/deployments/%s/stop" % urllib.parse.quote(key), {})

    def restart(self, key):
        return self.cluster.post("/api/llm/deployments/%s/restart" % urllib.parse.quote(key), {})

    def remove(self, key):
        return self.cluster.delete("/api/llm/deployments/%s" % urllib.parse.quote(key))

    # -- chatting ----------------------------------------------------------
    def chat(self, name, prompt=None, messages=None, system=None, max_tokens=512,
             temperature=None, on_token=None, timeout=None, **options):
        """Ask a running model. Give a ``prompt`` or a list of ``messages``
        (``[{"role": "user", "content": ...}]``). ``on_token(text)`` receives
        the answer as it streams. Returns the result dict (``text``,
        ``tokens``, ``tokens_per_second``, ...)."""
        body = dict(options)
        if messages:
            body["messages"] = list(messages)
        elif prompt is not None:
            body["prompt"] = str(prompt)
        else:
            raise PhoneClusterError("chat() needs a prompt or messages")
        if system:
            body["system"] = system
        body["max_tokens"] = int(max_tokens)
        if temperature is not None:
            body["temperature"] = float(temperature)
        callback = None
        if on_token is not None:
            def callback(partial):
                if isinstance(partial, dict) and partial.get("token"):
                    on_token(partial["token"])
        result = self.cluster.call(name, json_body=body, on_partial=callback, timeout=timeout)
        if result.get("status") != "done":
            raise ClusterError(502, result.get("error") or "the model did not answer")
        return result.get("result") or {}
