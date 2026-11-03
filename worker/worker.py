#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PhoneCluster worker - Termux / Linux / Windows.

Single file, standard library only, Python 3.8+.  Implements the worker side of
docs/SPEC.md (sections 6, 7 and 8): register, long-poll, run python/shell tasks,
pooled-storage chunks, remote terminal, notifications and the benchmark.

Usage:
    python worker.py --url https://192.168.1.5:8765 --ca cluster-ca.pem --join ABCD-EFGH
    python worker.py                      # reuse <state>/device.json
"""

import argparse
import binascii
import errno
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback

try:
    import http.client as httplib
    import urllib.parse as urlparse
except ImportError:  # pragma: no cover - python 2 never supported, kept explicit
    raise SystemExit("PhoneCluster worker requires Python 3.8+")

VERSION = "1.0"
IS_WINDOWS = os.name == "nt"
DEFAULT_STATE_DIR = os.path.join(os.path.expanduser("~"), ".phonecluster")
SENT_RESULT = "__PC_RESULT__"
SENT_WORK = "__PC_WORK__"
SENT_ERROR = "__PC_ERROR__"
SENT_PROGRESS = "__PC_PROGRESS__"
SENT_CHECKPOINT = "__PC_CHECKPOINT__"
CHECKPOINT_MIN_INTERVAL = 2.0     # seconds between checkpoint posts per task
CHECKPOINT_MAX_BYTES = 256 * 1024
MAX_LOG_LINES = 10000
STDOUT_CAP = 1024 * 1024
STDERR_CAP = 256 * 1024
STORAGE_CAP_MB = 200

_VERBOSE = False


def log(msg):
    sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    try:
        sys.stderr.flush()
    except Exception:
        pass


def dbg(msg):
    if _VERBOSE:
        log("[dbg] " + msg)


def warn(msg):
    log("[warn] " + msg)


def mkdirs(path):
    try:
        os.makedirs(path)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise
    return path


def touch(path):
    try:
        os.utime(path, None)
    except Exception:
        pass


def remove(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def replace_file(src, dst):
    """os.replace, with a copy fallback for filesystems that refuse it."""
    remove(dst)
    try:
        os.rename(src, dst)
    except Exception:
        shutil.copyfile(src, dst)
        remove(src)


def run_capture(args, timeout=10):
    """Run a command, return stripped stdout or None."""
    kwargs = {}
    if IS_WINDOWS:
        # keep helper probes (powershell, taskkill) from flashing a console
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, **kwargs)
    except Exception:
        return None
    try:
        out, _err = proc.communicate(timeout=timeout)
    except Exception:
        try:
            proc.kill()
            proc.communicate(timeout=2)
        except Exception:
            pass
        return None
    if proc.returncode != 0:
        return None
    try:
        return out.decode("utf-8", "replace").strip()
    except Exception:
        return None


DISCOVERY_PORT = 8766
DISCOVERY_MAGIC = b"PHONECLUSTER_DISCOVER v1"
REDISCOVER_AFTER = 3   # consecutive connection failures before asking the LAN again


def discover(timeout=2.0, port=DISCOVERY_PORT, hosts=None):
    """Find a coordinator on the LAN with a UDP broadcast (SPEC 2.1).

    Returns the coordinator's base URL or None.  ``hosts`` overrides the
    broadcast targets (tests use 127.0.0.1)."""
    targets = list(hosts) if hosts else ["255.255.255.255"]
    if not hosts:
        # also try the subnet broadcast of the default-route interface (some
        # Android builds drop 255.255.255.255 on Wi-Fi)
        probe = None
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("10.255.255.255", 1))
            mine = probe.getsockname()[0]
            parts = mine.split(".")
            if len(parts) == 4:
                targets.append(".".join(parts[:3] + ["255"]))
        except OSError:
            pass
        finally:
            if probe is not None:
                probe.close()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(timeout)
        for host in targets:
            try:
                sock.sendto(DISCOVERY_MAGIC, (host, int(port)))
            except OSError as exc:
                dbg("discovery send to %s failed: %s" % (host, exc))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                break
            except OSError:
                break
            try:
                reply = json.loads(data.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(reply, dict) and reply.get("phonecluster") and reply.get("url"):
                dbg("discovered %s at %s (%s)" % (reply.get("name"), reply["url"], addr[0]))
                return str(reply["url"])
    finally:
        sock.close()
    return None


def discover_forever(interval=5.0, port=DISCOVERY_PORT, hosts=None, stop=None):
    """Keep probing until a coordinator answers (or ``stop()`` is true)."""
    attempt = 0
    while not (stop and stop()):
        url = discover(port=port, hosts=hosts)
        if url:
            return url
        attempt += 1
        if attempt == 1 or attempt % 12 == 0:
            log("no coordinator found on the LAN yet, still looking ...")
        time.sleep(interval)
    return None


def input_key(entry):
    """Stable key for an ``inputs`` entry (file id, or a byte range dict)."""
    if isinstance(entry, dict):
        return "%s:%s+%s" % (entry.get("file_id"), entry.get("offset", 0),
                             entry.get("length", 0))
    return str(entry)


# --------------------------------------------------------------------------
# python impl harness (SPEC 6.1) - executed as `sys.executable -c HARNESS`
# --------------------------------------------------------------------------

HARNESS = r'''
import sys, os, json, traceback

R = '__PC_RESULT__'
E = '__PC_ERROR__'
P = '__PC_PROGRESS__'
C = '__PC_CHECKPOINT__'


class Ctx(object):
    def __init__(self, spec):
        self.task_id = spec.get('task_id')
        self.device_name = spec.get('device_name')
        self.workdir = spec.get('workdir')
        self._map = spec.get('inputs') or {}
        order = spec.get('input_order') or list(self._map.keys())
        self.inputs = [self._map[k] for k in order if k in self._map]
        self._outputs = []
        #: progress saved by an earlier attempt of this task, or None
        self.checkpoint_state = spec.get('checkpoint')

    def resume(self, default=None):
        """The last checkpoint() of a previous attempt, or ``default``."""
        return default if self.checkpoint_state is None else self.checkpoint_state

    def checkpoint(self, state):
        """Save resumable progress (JSON-serialisable, <= 256 KB). If this
        device dies, the next attempt finds it in ``ctx.resume()``."""
        try:
            text = json.dumps(state)
        except (TypeError, ValueError) as exc:
            raise TypeError('checkpoint state must be JSON-serialisable: %s' % exc)
        sys.stdout.write('\n' + C + text + '\n')
        sys.stdout.flush()

    def log(self, *parts):
        msg = ' '.join([str(p) for p in parts])
        for line in msg.split('\n'):
            sys.stdout.write(line.rstrip('\r') + '\n')
        sys.stdout.flush()

    def input(self, file_id):
        if file_id in self._map:
            return self._map[file_id]
        try:
            idx = int(file_id)
        except (TypeError, ValueError):
            raise KeyError(file_id)
        return self.inputs[idx]

    def output(self, path, name=None):
        p = os.path.abspath(path)
        self._outputs.append({'path': p, 'name': name or os.path.basename(p)})

    def progress(self, fraction):
        try:
            f = float(fraction)
        except (TypeError, ValueError):
            return
        sys.stdout.write(P + ' ' + ('%g' % f) + '\n')
        sys.stdout.flush()


def main():
    spec = json.loads(sys.stdin.read())
    ctx = Ctx(spec)
    wd = ctx.workdir
    if wd:
        try:
            os.makedirs(wd)
        except OSError:
            pass
        try:
            os.chdir(wd)
        except OSError:
            pass
    ns = {'__name__': 'pc_task'}
    exec(compile(spec.get('code') or '', '<pc_task>', 'exec'), ns)
    entry = spec.get('entry') or 'run'
    fn = ns.get(entry)
    if not callable(fn):
        raise NameError('entry point ' + repr(entry) + ' not found in task code')
    # the work starts now: interpreter start-up is not the task's speed
    sys.stdout.write('\n__PC_WORK__\n')
    sys.stdout.flush()
    result = fn(spec.get('args') or {}, ctx)
    if wd:
        try:
            fh = open(os.path.join(wd, '__pc_outputs.json'), 'w')
            fh.write(json.dumps(ctx._outputs))
            fh.close()
        except Exception:
            pass
    sys.stdout.write('\n' + R + json.dumps(result, default=repr) + '\n')
    sys.stdout.flush()


try:
    main()
except BaseException:
    et, ev, _tb = sys.exc_info()
    body = {'error': (str(ev) or et.__name__), 'traceback': traceback.format_exc()}
    try:
        sys.stdout.write('\n' + E + json.dumps(body) + '\n')
        sys.stdout.flush()
    except Exception:
        pass
    sys.exit(1)
'''


# --------------------------------------------------------------------------
# service harness (SPEC 6.7) - a long-lived model host, executed as
# `sys.executable -c SERVICE_HARNESS`.  The parent (ServiceRun) feeds request
# bytes over stdin in frames, so a model can consume a stream without the
# data ever being written to the phone's storage.
#
#   parent -> child (stdin, binary):
#       <spec json>\n                       once, at start
#       REQ <json>\n  DAT <n>\n<n bytes> ... END\n   per request
#       QUIT\n
#   child -> parent (stdout, text lines):
#       __PC_READY__<json>   after load() finished
#       __PC_PARTIAL__<json> for every ctx.emit()
#       __PC_DONE__<json>    {ok, result, outputs, error, traceback, ms}
#       anything else        log lines (print() included)
# --------------------------------------------------------------------------

SERVICE_HARNESS = r'''
import sys, os, json, time, traceback, shutil

READY = '__PC_READY__'
PARTIAL = '__PC_PARTIAL__'
DONE = '__PC_DONE__'
P = '__PC_PROGRESS__'
IN = sys.stdin.buffer
OUT = sys.stdout


def emit(prefix, obj):
    OUT.write('\n' + prefix + json.dumps(obj, default=repr) + '\n')
    OUT.flush()


def read_line():
    line = IN.readline()
    if not line:
        raise EOFError('parent closed stdin')
    return line.decode('utf-8', 'replace').rstrip('\r\n')


class Request(object):
    """One call to the service. Read it as a stream, or all at once."""

    def __init__(self, meta):
        self.id = meta.get('id')
        self.params = meta.get('params') or {}
        self.content_type = meta.get('content_type') or 'application/octet-stream'
        self.size = meta.get('size')
        self.name = meta.get('name')
        self._eof = False
        self._buffered = None

    def stream(self, chunk_size=None):
        """Yield the request body chunk by chunk as it arrives (no disk)."""
        if self._buffered is not None:
            yield self._buffered
            return
        while not self._eof:
            line = read_line()
            if line == 'END':
                self._eof = True
                return
            if not line.startswith('DAT '):
                raise RuntimeError('bad frame from parent: ' + line[:40])
            n = int(line[4:])
            data = b''
            while len(data) < n:
                more = IN.read(n - len(data))
                if not more:
                    raise EOFError('parent closed stdin mid-frame')
                data += more
            if n:
                yield data

    def read(self):
        if self._buffered is None:
            self._buffered = b''.join(self.stream())
        return self._buffered

    def text(self, encoding='utf-8'):
        return self.read().decode(encoding, 'replace')

    def json(self):
        return json.loads(self.text())

    def save(self, path):
        """Only if the model truly needs a file on disk."""
        with open(path, 'wb') as fh:
            for chunk in self.stream():
                fh.write(chunk)
        return path

    def drain(self):
        for _chunk in self.stream():
            pass

    @property
    def eof(self):
        """True once the whole body has been consumed."""
        return self._eof or self._buffered is not None


class Ctx(object):
    def __init__(self, spec):
        self.task_id = spec.get('task_id')
        self.device_name = spec.get('device_name')
        self.service_name = spec.get('service_name')
        self.service_dir = spec.get('service_dir')
        self.workdir = None
        self._map = spec.get('inputs') or {}
        order = spec.get('input_order') or list(self._map.keys())
        self.inputs = [self._map[k] for k in order if k in self._map]
        self.params = spec.get('service_params') or {}
        self.request = None
        self._outputs = []
        #: llama.cpp binaries / local model file, when the service asked for them
        self.runtime_dir = spec.get('runtime_dir')
        self.model_path = spec.get('model_path')
        self.coordinator_host = spec.get('coordinator_host')
        self._announce = {}

    def announce(self, facts):
        """Publish facts (e.g. an address) with the ready signal; they show
        up in the service registry for the coordinator to use."""
        self._announce.update(dict(facts or {}))

    def spawn(self, args, **kwargs):
        """Start a helper process (e.g. llama-server) that must never outlive
        this service, however the service ends: on Windows it joins a job
        object that the system kills when this process goes away, on Linux /
        Android it gets a parent-death signal.  Nothing system-wide changes."""
        import subprocess
        if os.name != 'nt' and sys.platform.startswith('linux'):
            def _die_with_parent():
                try:
                    import ctypes, signal
                    ctypes.CDLL(None).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
                except Exception:
                    pass
            kwargs.setdefault('preexec_fn', _die_with_parent)
        proc = subprocess.Popen(args, **kwargs)
        if os.name == 'nt':
            _bind_to_this_process(proc)
        return proc

    def lan_ip(self):
        """This device's address on the network the coordinator is on."""
        import socket as _socket
        for target in (self.coordinator_host, '10.255.255.255'):
            if not target:
                continue
            sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
            try:
                sock.connect((target, 1))
                ip = sock.getsockname()[0]
            except OSError:
                continue
            finally:
                sock.close()
            if ip and not ip.startswith('127.') and not ip.startswith('0.'):
                return ip
        return '127.0.0.1'

    def log(self, *parts):
        msg = ' '.join([str(p) for p in parts])
        for line in msg.split('\n'):
            OUT.write(line.rstrip('\r') + '\n')
        OUT.flush()

    def input(self, file_id):
        if file_id in self._map:
            return self._map[file_id]
        return self.inputs[int(file_id)]

    def progress(self, fraction):
        try:
            OUT.write(P + ' ' + ('%g' % float(fraction)) + '\n')
            OUT.flush()
        except (TypeError, ValueError):
            pass

    def emit(self, partial):
        """Send an intermediate result to the caller right now."""
        emit(PARTIAL, {'data': partial})

    def output(self, path, name=None):
        p = os.path.abspath(path)
        self._outputs.append({'path': p, 'name': name or os.path.basename(p)})

    def output_bytes(self, data, name):
        """Return binary results (audio, images) as a downloadable file."""
        p = os.path.join(self.workdir, name)
        with open(p, 'wb') as fh:
            fh.write(data)
        self._outputs.append({'path': p, 'name': name})


_JOB = []


def _bind_to_this_process(proc):
    """Windows: put ``proc`` in a kill-on-close job object held only by this
    process, so it dies with us even if we are killed outright."""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        if not _JOB:
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
            k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                    wintypes.DWORD]
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

            class Basic(ctypes.Structure):
                _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64),
                            ('PerJobUserTimeLimit', ctypes.c_int64),
                            ('LimitFlags', wintypes.DWORD),
                            ('MinimumWorkingSetSize', ctypes.c_size_t),
                            ('MaximumWorkingSetSize', ctypes.c_size_t),
                            ('ActiveProcessLimit', wintypes.DWORD),
                            ('Affinity', ctypes.c_size_t),
                            ('PriorityClass', wintypes.DWORD),
                            ('SchedulingClass', wintypes.DWORD)]

            class Extended(ctypes.Structure):
                _fields_ = [('BasicLimitInformation', Basic),
                            ('IoInfo', ctypes.c_uint64 * 6),
                            ('ProcessMemoryLimit', ctypes.c_size_t),
                            ('JobMemoryLimit', ctypes.c_size_t),
                            ('PeakProcessMemoryUsed', ctypes.c_size_t),
                            ('PeakJobMemoryUsed', ctypes.c_size_t)]

            job = k32.CreateJobObjectW(None, None)
            if not job:
                return False
            info = Extended()
            info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                return False
            _JOB.append(job)
        return bool(k32.AssignProcessToJobObject(_JOB[0], int(proc._handle)))
    except Exception:
        return False


def main():
    spec = json.loads(read_line())
    ctx = Ctx(spec)
    for d in (ctx.service_dir,):
        if d:
            try:
                os.makedirs(d)
            except OSError:
                pass
    ns = {'__name__': 'pc_service'}
    exec(compile(spec.get('code') or '', '<pc_service>', 'exec'), ns)
    serve = ns.get('serve')
    if not callable(serve):
        raise NameError('service code must define serve(request, ctx)')
    load = ns.get('load')
    model = load(ctx) if callable(load) else None
    user_unload = ns.get('unload')
    done = []

    def unload_once():
        if done or not callable(user_unload):
            return
        done.append(True)
        try:
            user_unload(model, ctx)
        except Exception:
            pass

    # also clean up when the parent vanishes (EOF on stdin) or on sys.exit
    import atexit
    atexit.register(unload_once)
    emit(READY, {'ok': True, 'announce': ctx._announce})
    base = spec.get('workdir') or os.getcwd()
    while True:
        line = read_line()
        if line == 'QUIT':
            unload_once()
            return
        if not line.startswith('REQ '):
            continue
        meta = json.loads(line[4:])
        req = Request(meta)
        ctx.request = req
        ctx.workdir = os.path.join(base, str(req.id))
        ctx._outputs = []
        try:
            os.makedirs(ctx.workdir)
        except OSError:
            pass
        started = time.time()
        try:
            if 'model' in serve.__code__.co_varnames[:serve.__code__.co_argcount]:
                result = serve(req, ctx, model)
            else:
                result = serve(req, ctx)
            try:
                req.drain()
            except Exception:
                pass
            outputs = [o for o in ctx._outputs if os.path.isfile(o['path'])]
            emit(DONE, {'ok': True, 'result': result, 'outputs': outputs,
                        'ms': int((time.time() - started) * 1000)})
        except BaseException:
            et, ev, _tb = sys.exc_info()
            try:
                req.drain()
            except Exception:
                pass
            emit(DONE, {'ok': False, 'error': (str(ev) or et.__name__),
                        'traceback': traceback.format_exc(),
                        'ms': int((time.time() - started) * 1000)})
            if isinstance(ev, (KeyboardInterrupt, SystemExit)):
                return
        finally:
            ctx.request = None


try:
    main()
except BaseException:
    et, ev, _tb = sys.exc_info()
    if not isinstance(ev, EOFError):
        try:
            emit(DONE, {'ok': False, 'fatal': True, 'error': (str(ev) or et.__name__),
                        'traceback': traceback.format_exc()})
        except Exception:
            pass
    sys.exit(1)
'''


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, status, body):
        Exception.__init__(self, "HTTP %s: %s" % (status, body[:200]))
        self.status = status
        self.body = body


class Unauthorized(ApiError):
    pass


class Removed(ApiError):
    pass


def is_loopback_host(host):
    host = str(host or "").strip("[]").lower()
    return host in ("localhost", "::1") or host.startswith("127.")


def tls_context(ca_file):
    """Trust exactly the cluster's own CA (SPEC 3).  Host names are not
    checked: the PC's address changes, and only the cluster can sign."""
    import ssl

    if not ca_file or not os.path.isfile(ca_file):
        raise ApiError(0, "no cluster certificate: join again with the command from the PC's Join page")
    ctx = ssl.create_default_context(cafile=ca_file)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


class HttpClient(object):
    """Tiny http.client wrapper with a keep-alive connection pool.

    HTTPS is checked against the cluster CA; plain HTTP is only allowed to
    this machine itself (the PC worker on the PC-only door)."""

    POOL_MAX = 6

    def __init__(self, url, ca_file=None):
        parts = urlparse.urlsplit(url if "://" in url else "https://" + url)
        self.scheme = parts.scheme or "https"
        self.host = parts.hostname or "127.0.0.1"
        self.port = parts.port or (443 if self.scheme == "https" else 80)
        self.prefix = (parts.path or "").rstrip("/")
        if self.scheme == "http" and not is_loopback_host(self.host):
            raise ValueError("plain http is only used on the PC itself; phones use the https join command")
        self.ca_file = ca_file
        self.token = None
        self._idle = []
        self._lock = threading.Lock()

    @property
    def base_url(self):
        default = 443 if self.scheme == "https" else 80
        host = self.host if self.port == default else "%s:%d" % (self.host, self.port)
        return "%s://%s%s" % (self.scheme, host, self.prefix)

    def _new_conn(self, timeout):
        if self.scheme == "https":
            return httplib.HTTPSConnection(self.host, self.port, timeout=timeout,
                                           context=tls_context(self.ca_file))
        return httplib.HTTPConnection(self.host, self.port, timeout=timeout)

    #: uvicorn drops idle keep-alive connections after 5 s; never reuse one
    #: that has been idle longer than this
    IDLE_MAX = 4.0

    def _acquire(self, timeout, fresh=False):
        if not fresh:
            with self._lock:
                while self._idle:
                    conn, since = self._idle.pop()
                    if time.time() - since > self.IDLE_MAX:
                        try:
                            conn.close()
                        except Exception:
                            pass
                        continue
                    conn.timeout = timeout
                    if getattr(conn, "sock", None) is not None:
                        try:
                            conn.sock.settimeout(timeout)
                        except Exception:
                            pass
                    return conn, True
        return self._new_conn(timeout), False

    def _release(self, conn):
        with self._lock:
            if len(self._idle) < self.POOL_MAX:
                self._idle.append((conn, time.time()))
                return
        try:
            conn.close()
        except Exception:
            pass

    def close(self):
        with self._lock:
            conns = self._idle
            self._idle = []
        for conn, _since in conns:
            try:
                conn.close()
            except Exception:
                pass

    def request(self, method, path, body=None, headers=None, timeout=30.0,
                content_type=None):
        """Return (status, bytes).  Raises on transport errors."""
        hdrs = {"Connection": "keep-alive", "Accept": "application/json",
                "User-Agent": "phonecluster-worker/" + VERSION}
        if self.token:
            hdrs["X-Device-Token"] = self.token
        if content_type:
            hdrs["Content-Type"] = content_type
        if headers:
            hdrs.update(headers)
        full = self.prefix + path
        last = None
        for attempt in (0, 1):
            # the retry after a dead pooled socket must use a brand-new one
            conn, reused = self._acquire(timeout, fresh=(attempt == 1))
            try:
                conn.request(method, full, body=body, headers=hdrs)
                resp = conn.getresponse()
                data = resp.read()
                status = resp.status
                if resp.will_close:
                    try:
                        conn.close()
                    except Exception:
                        pass
                else:
                    self._release(conn)
                return status, data
            except Exception as exc:
                last = exc
                try:
                    conn.close()
                except Exception:
                    pass
                # A stale pooled connection dies on the first write: retry once.
                if reused and attempt == 0 and not hasattr(body, "read"):
                    continue
                raise
        raise last

    def open_stream(self, method, path, body=None, timeout=300.0, headers=None):
        """Send a request and return ``(status, response)`` **without** reading
        the body, so the caller can consume it incrementally.  The connection
        is dedicated to this response; ``response.close()`` releases it."""
        hdrs = {"Connection": "close", "Accept": "*/*",
                "User-Agent": "phonecluster-worker/" + VERSION}
        if self.token:
            hdrs["X-Device-Token"] = self.token
        if headers:
            hdrs.update(headers)
        conn = self._new_conn(timeout)
        try:
            conn.request(method, self.prefix + path, body=body, headers=hdrs)
            resp = conn.getresponse()
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            raise
        original_close = resp.close

        def close_all():
            try:
                original_close()
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        resp.close = close_all
        return resp.status, resp

    def call(self, method, path, obj=None, timeout=30.0):
        """JSON in / JSON out.  Raises Unauthorized / Removed / ApiError."""
        body = None
        ctype = None
        if obj is not None:
            body = json.dumps(obj).encode("utf-8")
            ctype = "application/json"
        status, data = self.request(method, path, body=body, timeout=timeout,
                                    content_type=ctype)
        text = data.decode("utf-8", "replace")
        if status == 401:
            raise Unauthorized(status, text)
        if status == 410:
            raise Removed(status, text)
        if status < 200 or status >= 300:
            raise ApiError(status, text)
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except ValueError:
            return {"raw": text}


# --------------------------------------------------------------------------
# platform / capability detection (SPEC 8)
# --------------------------------------------------------------------------

def is_termux():
    if "com.termux" in (os.environ.get("PREFIX") or ""):
        return True
    if os.environ.get("TERMUX_VERSION"):
        return True
    return os.path.isdir("/data/data/com.termux/files/usr")


def is_android():
    if is_termux():
        return True
    return bool(os.environ.get("ANDROID_ROOT")) and os.path.isdir("/system/bin")


def platform_tag():
    if IS_WINDOWS:
        return "windows"
    if is_termux():
        return "termux"
    return "linux"


def project_root():
    env = os.environ.get("PC_ROOT")
    if env and os.path.isdir(env):
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(here)


def find_ffmpeg():
    """Return the ffmpeg path, or None.  PATH first, then <PC_ROOT>/tools/ffmpeg."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    name = "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"
    cand = os.path.join(project_root(), "tools", "ffmpeg", name)
    if os.path.isfile(cand):
        return cand
    if not IS_WINDOWS:
        cand = os.path.join(project_root(), "tools", "ffmpeg", "ffmpeg.exe")
        if os.path.isfile(cand):
            return cand
    return None


def has_termux_api():
    return shutil.which("termux-battery-status") is not None


def find_wasmtime():
    """The WebAssembly runtime: <PC_ROOT>/tools/wasmtime first (the PC, put
    there by scripts/get-wasmtime.ps1), then PATH (Termux: pkg install wasmtime)."""
    name = "wasmtime.exe" if IS_WINDOWS else "wasmtime"
    cand = os.path.join(project_root(), "tools", "wasmtime", name)
    if os.path.isfile(cand):
        return cand
    return shutil.which("wasmtime")


def can_install_wasmtime():
    """Termux installs wasmtime from its own repository on the first WASM task."""
    return is_termux() and shutil.which("pkg") is not None


def free_mb(path):
    try:
        usage = shutil.disk_usage(path)
        return int(usage.free // (1024 * 1024))
    except Exception:
        return None


def detect_caps(state_dir):
    # "service": can host long-lived model services (SPEC 6.7); browsers cannot
    # "peer": can send cached inputs to other devices (SPEC 8.3)
    caps = ["python", "shell", "service", "peer", platform_tag()]
    if find_ffmpeg():
        caps.append("ffmpeg")
    if has_termux_api():
        caps.append("termux_api")
    if find_wasmtime() or can_install_wasmtime():
        caps.append("wasm")
    free = free_mb(state_dir)
    if free is not None and free >= STORAGE_CAP_MB:
        caps.append("storage")
    out = []
    for cap in caps:
        if cap not in out:
            out.append(cap)
    return out


def _windows_memory():
    """(total MB, available MB) from GlobalMemoryStatusEx, or (None, None)."""
    try:
        import ctypes

        class MemStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = MemStatus()
        stat.dwLength = ctypes.sizeof(MemStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return (int(stat.ullTotalPhys // (1024 * 1024)),
                    int(stat.ullAvailPhys // (1024 * 1024)))
    except Exception:
        pass
    return None, None


def read_mem_mb():
    if IS_WINDOWS:
        return _windows_memory()[0]
    return parse_meminfo(read_text("/proc/meminfo"))


def read_mem_free_mb():
    """Memory the OS could hand to a new process right now (MB), or None."""
    if IS_WINDOWS:
        return _windows_memory()[1]
    return parse_meminfo(read_text("/proc/meminfo"), key="MemAvailable:")


def parse_meminfo(text, key="MemTotal:"):
    """A field (kB) from /proc/meminfo content -> MB."""
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith(key):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    return int(int(parts[1]) // 1024)
                except ValueError:
                    return None
    return None


def android_api():
    """Android SDK level (int) or None off Android."""
    if not is_android():
        return None
    out = run_capture(["getprop", "ro.build.version.sdk"], timeout=5)
    try:
        return int(out) if out else None
    except ValueError:
        return None


def cpu_abi():
    """arm64-v8a / armeabi-v7a / x86_64 ... (Android ABI names on Android)."""
    if is_android():
        out = run_capture(["getprop", "ro.product.cpu.abi"], timeout=5)
        if out:
            return out
    machine = (platform.machine() or "").lower()
    return {"amd64": "x86_64", "x86_64": "x86_64", "aarch64": "arm64-v8a",
            "arm64": "arm64-v8a"}.get(machine, machine or "unknown")


def read_text(path):
    """File contents, or None for anything that goes wrong (sysfs is flaky)."""
    try:
        with open(path, "r") as handle:
            return handle.read()
    except Exception:
        return None


def default_shell():
    if IS_WINDOWS:
        return os.environ.get("COMSPEC") or "cmd.exe"
    for name in ("bash", "sh"):
        found = shutil.which(name)
        if found:
            return found
    return os.environ.get("SHELL") or "/bin/sh"


def device_model():
    if is_android():
        out = run_capture(["getprop", "ro.product.model"], timeout=5)
        if out:
            return out
    return platform.node() or "device"


def collect_info():
    return {
        "cpu_count": os.cpu_count() or 1,
        "mem_mb": read_mem_mb(),
        "os": "%s %s" % (platform.system(), platform.release()),
        "python": platform.python_version(),
        "model": device_model(),
        "shell": default_shell(),
        "abi": cpu_abi(),
        "android_api": android_api(),
        # the PC worker started by the coordinator reads models from the project folder
        "coordinator_local": bool(os.environ.get("PC_ROOT")),
    }


# --------------------------------------------------------------------------
# battery / thermal (SPEC 8)
# --------------------------------------------------------------------------

def parse_temp_value(raw):
    """sysfs temperature -> degrees C (handles tenths and millidegrees)."""
    if raw is None:
        return None
    try:
        val = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    neg = val < 0
    val = abs(val)
    guard = 0
    while val > 100.0 and guard < 4:
        val = val / 10.0
        guard += 1
    if neg:
        val = -val
    if val < -50.0 or val > 150.0:
        return None
    return round(val, 1)


def parse_charging(status):
    """sysfs / termux battery status string -> True / False / None."""
    text = (status or "").strip().lower().replace("_", " ")
    if not text:
        return None
    if text in ("charging", "full"):
        return True
    if text in ("discharging", "not charging"):
        return False
    return None


def parse_capacity(raw):
    try:
        val = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return None
    if val < 0 or val > 100:
        return None
    return val


def parse_termux_battery(text):
    """termux-battery-status JSON -> (battery, charging, temp)."""
    if not text:
        return (None, None, None)
    try:
        obj = json.loads(text)
    except ValueError:
        return (None, None, None)
    if not isinstance(obj, dict):
        return (None, None, None)
    battery = parse_capacity(obj.get("percentage"))
    charging = parse_charging(obj.get("status"))
    if charging is None:
        plugged = (obj.get("plugged") or "").strip().upper()
        if plugged and plugged != "UNPLUGGED":
            charging = True
    temp = None
    if obj.get("temperature") is not None:
        try:
            temp = round(float(obj.get("temperature")), 1)
        except (TypeError, ValueError):
            temp = None
        if temp is not None and (temp < -50.0 or temp > 150.0):
            temp = None
    return (battery, charging, temp)


def parse_win_battery(text):
    """'85,2' from the PowerShell probe -> (battery, charging)."""
    if not text:
        return (None, None)
    line = text.strip().splitlines()[0].strip() if text.strip() else ""
    if not line or "," not in line:
        return (None, None)
    left, right = line.split(",", 1)
    battery = parse_capacity(left)
    charging = None
    try:
        code = int(float(right.strip()))
        charging = code in (2, 3, 6, 7, 8, 9)
    except (TypeError, ValueError):
        charging = None
    return (battery, charging)


POWER_SUPPLY = "/sys/class/power_supply"
SYSFS_BATTERY = POWER_SUPPLY + "/battery"
THERMAL = "/sys/class/thermal"


def find_battery_supply(base=None):
    """The device's own battery under /sys/class/power_supply, or None (a desktop).
    Android kernels call it "battery" (also under a Linux like the J2's), laptops
    BAT0 / BAT1; batteries of mice and headsets (scope "Device") don't count."""
    base = base or POWER_SUPPLY
    preferred = os.path.join(base, "battery")
    if parse_capacity(read_text(os.path.join(preferred, "capacity"))) is not None:
        return preferred
    try:
        names = sorted(os.listdir(base))
    except Exception:
        return None
    for name in names:
        path = os.path.join(base, name)
        if (read_text(os.path.join(path, "type")) or "").strip().lower() != "battery":
            continue
        if (read_text(os.path.join(path, "scope")) or "").strip().lower() == "device":
            continue
        if parse_capacity(read_text(os.path.join(path, "capacity"))) is not None:
            return path
    return None


class BatteryMonitor(object):
    """battery %, charging flag and temperature, cached per source."""

    def __init__(self, use_termux_api=False):
        self.use_termux_api = use_termux_api
        self._lock = threading.Lock()
        self._cached = (None, None, None)
        self._next_api = 0.0
        self._next_win = 0.0

    def read(self):
        with self._lock:
            try:
                if IS_WINDOWS:
                    return self._read_windows()
                if is_android():
                    return self._read_android()
                if sys.platform.startswith("linux"):
                    return self._read_linux()
            except Exception as exc:
                dbg("battery read failed: %s" % exc)
            return (None, None, None)

    # -- android ---------------------------------------------------------
    def _read_android(self):
        battery = parse_capacity(read_text(os.path.join(SYSFS_BATTERY, "capacity")))
        charging = parse_charging(read_text(os.path.join(SYSFS_BATTERY, "status")))
        temp = parse_temp_value(read_text(os.path.join(SYSFS_BATTERY, "temp")))
        if temp is None:
            temp = self._read_thermal_zone()
        if self.use_termux_api and (battery is None or temp is None):
            now = time.time()
            if now >= self._next_api:
                self._next_api = now + 30.0
                out = run_capture(["termux-battery-status"], timeout=15)
                self._cached = parse_termux_battery(out)
            api_b, api_c, api_t = self._cached
            if battery is None:
                battery = api_b
            if charging is None:
                charging = api_c
            if temp is None:
                temp = api_t
        return (battery, charging, temp)

    # -- linux (a phone running Linux, like the J2, or a laptop) -----------
    def _read_linux(self, power_supply=None, thermal=None):
        supply = find_battery_supply(power_supply)
        if supply is None:
            return (None, None, None)  # no battery: a desktop, nothing to protect
        battery = parse_capacity(read_text(os.path.join(supply, "capacity")))
        charging = parse_charging(read_text(os.path.join(supply, "status")))
        temp = parse_temp_value(read_text(os.path.join(supply, "temp")))
        if temp is None and os.path.basename(supply) == "battery":
            # a phone's kernel: the battery or chip sensor is the next best thing.
            # (Not on laptops: CPU sensors there run far above the phone limits.)
            temp = self._read_thermal_zone(thermal, any_zone=True)
        return (battery, charging, temp)

    def _read_thermal_zone(self, base=None, any_zone=False):
        base = base or THERMAL
        try:
            names = sorted(os.listdir(base))
        except Exception:
            return None
        best = first = None
        for name in names:
            if not name.startswith("thermal_zone"):
                continue
            zone = os.path.join(base, name)
            kind = (read_text(os.path.join(zone, "type")) or "").strip().lower()
            val = parse_temp_value(read_text(os.path.join(zone, "temp")))
            if val is None:
                continue
            if "battery" in kind:
                return val
            if best is None and ("cpu" in kind or "soc" in kind):
                best = val
            if first is None:
                first = val
        if best is None and any_zone:
            return first
        return best

    # -- windows ---------------------------------------------------------
    WIN_PS = (
        "$b = Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue | "
        "Select-Object -First 1; if ($b) { "
        "[string]$b.EstimatedChargeRemaining + ',' + [string]$b.BatteryStatus }"
    )

    def _read_windows(self):
        now = time.time()
        if now >= self._next_win:
            self._next_win = now + 60.0
            out = run_capture(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", self.WIN_PS],
                timeout=25)
            battery, charging = parse_win_battery(out)
            self._cached = (battery, charging, None)
        return self._cached


# --------------------------------------------------------------------------
# graphics card (SPEC 6.8.1): read-only; nothing is configured on the system
# --------------------------------------------------------------------------

GPU_QUERY = "index,name,memory.total,memory.free,temperature.gpu,utilization.gpu"
VULKAN_LIST = re.compile(r"Vulkan(\d+):\s*(.+?)\s*\((\d+) MiB, (\d+) MiB free\)")


def find_nvidia_smi():
    found = shutil.which("nvidia-smi")
    if found:
        return found
    if IS_WINDOWS:
        cand = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32", "nvidia-smi.exe")
        if os.path.isfile(cand):
            return cand
    return None


def parse_nvidia_smi(text):
    """``nvidia-smi --query-gpu=GPU_QUERY --format=csv,noheader,nounits`` -> list of dicts."""
    out = []
    for line in (text or "").splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) < 6:
            continue

        def num(value):
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return None

        out.append({"index": num(cells[0]), "name": cells[1], "vram_total_mb": num(cells[2]),
                    "vram_free_mb": num(cells[3]), "temp": num(cells[4]), "util": num(cells[5])})
    return out


def parse_vulkan_devices(text):
    """``llama-server --list-devices`` output -> [{vk_index, name, total_mb, free_mb}]."""
    out = []
    for match in VULKAN_LIST.finditer(text or ""):
        out.append({"vk_index": int(match.group(1)), "name": match.group(2).strip(),
                    "total_mb": int(match.group(3)), "free_mb": int(match.group(4))})
    return out


def gpu_build_dir():
    """The project's GPU build of llama.cpp (PC worker only), or None."""
    root = os.environ.get("PC_ROOT")
    if not root or not IS_WINDOWS:
        return None
    path = os.path.join(root, "tools", "llama.cpp", "win-vulkan-x64")
    return path if find_binary(path, ("llama-server",)) else None


def list_vulkan_devices(bin_dir, timeout=30):
    """Ask the GPU build which cards it sees.  The AMD switchable-graphics
    layer is switched off for this one process only (it makes the listing hang
    on hybrid laptops); nothing system-wide changes."""
    exe = find_binary(bin_dir, ("llama-server",))
    if not exe:
        return []
    env = dict(os.environ)
    env["DISABLE_LAYER_AMD_SWITCHABLE_GRAPHICS_1"] = "1"
    kwargs = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if IS_WINDOWS else {}
    try:
        proc = subprocess.Popen([exe, "--list-devices"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, cwd=bin_dir, env=env, **kwargs)
        out, _ = proc.communicate(timeout=timeout)
    except Exception as exc:
        dbg("vulkan device listing failed: %s" % exc)
        try:
            proc.kill()
        except Exception:
            pass
        return []
    return parse_vulkan_devices(out.decode("utf-8", "replace"))


def match_gpus(nvidia, vulkan):
    """Pair nvidia-smi cards (temperature readable) with the Vulkan index the
    GPU build uses for them.  Cards we cannot watch are left out."""
    out = []
    for card in nvidia:
        name = (card.get("name") or "").lower()
        for vk in vulkan:
            vk_name = vk["name"].lower()
            if name and (name in vk_name or vk_name in name):
                out.append({"name": card["name"], "vram_mb": card.get("vram_total_mb"),
                            "vk_index": vk["vk_index"], "nvidia_index": card.get("index")})
                break
    return out


class GpuMonitor(object):
    """Live figures for the cards the GPU build may use, refreshed in the
    background (nvidia-smi takes a moment) so polls never wait for it."""

    def __init__(self, cards, smi):
        self.cards = cards or []
        self.smi = smi
        self.latest = None
        self._lock = threading.Lock()
        self._next = 0.0
        self._busy = False

    def read(self):
        if not self.cards or not self.smi:
            return None
        now = time.time()
        with self._lock:
            start = now >= self._next and not self._busy
            if start:
                self._busy = True
                self._next = now + 10.0
        if start:
            thread = threading.Thread(target=self._refresh, name="gpu-monitor")
            thread.daemon = True
            thread.start()
        return self.latest

    def _refresh(self):
        try:
            text = run_capture([self.smi, "--query-gpu=" + GPU_QUERY, "--format=csv,noheader,nounits"],
                               timeout=15)
            wanted = self.cards[0].get("nvidia_index")
            for card in parse_nvidia_smi(text):
                if wanted is None or card.get("index") == wanted:
                    self.latest = {"name": card["name"], "vram_total_mb": card["vram_total_mb"],
                                   "vram_free_mb": card["vram_free_mb"], "temp": card["temp"],
                                   "util": card["util"], "ts": time.time()}
                    break
        except Exception as exc:
            dbg("gpu read failed: %s" % exc)
        finally:
            self._busy = False


def probe_gpus():
    """(cards, nvidia-smi path) for the PC worker; ([], None) everywhere else."""
    build = gpu_build_dir()
    smi = find_nvidia_smi() if build else None
    if not build or not smi:
        return [], None
    nvidia = parse_nvidia_smi(run_capture([smi, "--query-gpu=" + GPU_QUERY, "--format=csv,noheader,nounits"],
                                          timeout=15))
    if not nvidia:
        return [], None
    return match_gpus(nvidia, list_vulkan_devices(build)), smi


# --------------------------------------------------------------------------
# input cache (LRU)
# --------------------------------------------------------------------------

class FileCache(object):
    """LRU cache of downloaded input files under <state>/cache."""

    def __init__(self, directory, cap_mb=500):
        self.dir = mkdirs(directory)
        self.cap = int(float(cap_mb) * 1024 * 1024)
        self.lock = threading.Lock()

    def path(self, file_id):
        return os.path.join(self.dir, str(file_id))

    def get(self, file_id):
        path = self.path(file_id)
        if not os.path.isfile(path):
            return None
        touch(path)  # mark as most recently used
        return path

    def put_bytes(self, file_id, data):
        path = self.path(file_id)
        # unique per call: tasks running side by side may store the same file
        tmp = path + ".part-%d-%d-%s" % (os.getpid(), threading.current_thread().ident or 0,
                                         binascii.hexlify(os.urandom(3)).decode("ascii"))
        with open(tmp, "wb") as handle:
            handle.write(data)
        replace_file(tmp, path)
        touch(path)  # freshly fetched == most recently used
        self.enforce()
        return path

    def put_file(self, file_id, src):
        """Move a finished download into the cache."""
        path = self.path(file_id)
        replace_file(src, path)
        touch(path)
        self.enforce()
        return path

    def entries(self):
        """[(mtime, size, path)] for every complete file in the cache."""
        out = []
        try:
            names = os.listdir(self.dir)
        except Exception:
            return out
        for name in names:
            if ".part-" in name:
                continue
            path = os.path.join(self.dir, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            if os.path.isfile(path):
                out.append((stat.st_mtime, stat.st_size, path))
        return out

    def enforce(self):
        """Evict oldest files until the cache fits.  Returns removed paths."""
        removed = []
        with self.lock:
            items = self.entries()
            total = sum([it[1] for it in items])
            if total <= self.cap:
                return removed
            items.sort(key=lambda it: it[0])
            for _mtime, size, path in items:
                if total <= self.cap:
                    break
                try:
                    os.remove(path)
                    removed.append(path)
                    total -= size
                except OSError:
                    continue
        return removed


# --------------------------------------------------------------------------
# phone-to-phone transfers (SPEC 8.3)
# --------------------------------------------------------------------------

PEER_TOKEN_HEADER = "X-Peer-Token"


def lan_ip_for(host):
    """This device's address on the network that reaches ``host``."""
    for target in (host, "10.255.255.255"):
        if not target:
            continue
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((target, 1))
            ip = sock.getsockname()[0]
        except (OSError, socket.error):
            continue
        finally:
            sock.close()
        if ip and not ip.startswith("0."):
            return ip
    return "127.0.0.1"


def parse_range_header(header, size):
    """``bytes=a-b`` -> inclusive (start, end) within ``size``, or None."""
    if not header or not header.strip().lower().startswith("bytes="):
        return None
    spec = header.strip()[6:].split(",")[0].strip()
    if "-" not in spec:
        return None
    first, last = spec.split("-", 1)
    try:
        if first == "":
            return (max(0, size - int(last)), size - 1)
        return (int(first), min(int(last) if last else size - 1, size - 1))
    except ValueError:
        return None


class PeerServer(object):
    """Sends cached input files to other devices, and only while asked to.

    The coordinator sends ``peer_serve`` with a one-time token.  A server
    starts on a random port of this device's LAN address, serves only the
    offered file and only to requests that carry the token, and shuts down
    as soon as no offer is left: the coordinator closes an offer when the
    fetcher is done, and an offer nobody used for its TTL expires.
    """

    def __init__(self, worker):
        self.worker = worker
        self.offers = {}
        self.lock = threading.Lock()
        self.server = None
        self.address = None
        self.reaper = None
        self.sent_bytes = 0

    def offer(self, cmd):
        offer_id = str(cmd.get("offer_id") or "")
        file_id = str(cmd.get("file_id") or "")
        body = {"offer_id": offer_id, "ok": False}
        path = self.worker.cache.get(file_id) if file_id else None
        if not offer_id or path is None:
            body["error"] = "not here"
        elif not self.worker.has_device_cert():
            body["error"] = "no certificate (join again)"
        else:
            try:
                ttl = max(5.0, min(600.0, float(cmd.get("ttl") or 60)))
                with self.lock:
                    self.offers[offer_id] = {"file_id": file_id, "token": str(cmd.get("token") or ""),
                                             "ttl": ttl, "until": time.time() + ttl}
                    host, port = self._ensure_server()
                body.update({"ok": True, "host": host, "port": port})
            except Exception as exc:
                self.close(offer_id)
                body["error"] = "could not open a port: %s" % exc
        try:
            self.worker.http.call("POST", "/api/w/peer/ready", body, timeout=10.0)
        except Exception as exc:
            dbg("peer ready post failed: %s" % exc)

    def close(self, offer_id):
        with self.lock:
            self.offers.pop(str(offer_id), None)
            idle = not self.offers
        if idle:
            self._stop()

    def match(self, file_id, token):
        """The cache path when ``token`` opens an offer for ``file_id``."""
        if not token:
            return None
        stamp = time.time()
        with self.lock:
            for offer in self.offers.values():
                if offer["file_id"] == file_id and offer["token"] == token and offer["until"] > stamp:
                    offer["until"] = stamp + offer["ttl"]  # still in use
                    return self.worker.cache.get(file_id)
        return None

    def _ensure_server(self):
        if self.server is not None:
            return self.address
        try:
            from http.server import BaseHTTPRequestHandler, HTTPServer
            from socketserver import ThreadingMixIn
        except ImportError:  # pragma: no cover
            raise RuntimeError("http.server is not available")
        owner = self

        class Server(ThreadingMixIn, HTTPServer):
            daemon_threads = True
            allow_reuse_address = False

            def handle_error(self, request, client_address):
                dbg("peer request from %s failed" % (client_address,))  # e.g. a failed handshake

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                # TLS handshake here, in the request thread, with a deadline
                self.request.settimeout(20)
                try:
                    self.request.do_handshake()
                except Exception:
                    self.request.close()
                    raise
                BaseHTTPRequestHandler.setup(self)

            def log_message(self, *_args):
                pass

            def do_GET(self):
                parts = self.path.split("?", 1)[0].strip("/").split("/")
                file_id = parts[1] if len(parts) == 2 and parts[0] == "f" else ""
                path = owner.match(file_id, self.headers.get(PEER_TOKEN_HEADER))
                if path is None:
                    self.send_error(403)
                    return
                size = os.path.getsize(path)
                rng = parse_range_header(self.headers.get("Range"), size)
                start, end = rng if rng else (0, size - 1)
                if start >= size or end < start:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                length = end - start + 1
                self.send_response(206 if rng else 200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(length))
                if rng:
                    self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
                self.end_headers()
                with open(path, "rb") as handle:
                    handle.seek(start)
                    left = length
                    while left > 0:
                        block = handle.read(min(65536, left))
                        if not block:
                            break
                        self.wfile.write(block)
                        left -= len(block)
                        owner.sent_bytes += len(block)

        import ssl

        host = lan_ip_for(self.worker.http.host)
        server = Server((host, 0), Handler)
        # HTTPS with this device's own certificate, issued by the cluster CA
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.worker.cert_file, self.worker.key_file)
        server.socket = context.wrap_socket(server.socket, server_side=True,
                                            do_handshake_on_connect=False)
        thread = threading.Thread(target=server.serve_forever, name="peer-server")
        thread.daemon = True
        thread.start()
        self.server = server
        self.address = (host, server.server_address[1])
        log("sending a file to another device on %s:%d (closes when done)" % self.address)
        if self.reaper is None or not self.reaper.is_alive():
            self.reaper = threading.Thread(target=self._reap, name="peer-reaper")
            self.reaper.daemon = True
            self.reaper.start()
        return self.address

    def _reap(self):
        while not self.worker.stopping:
            time.sleep(2.0)
            stamp = time.time()
            with self.lock:
                for offer_id in [o for o, offer in self.offers.items() if offer["until"] < stamp]:
                    self.offers.pop(offer_id, None)
                idle = not self.offers
            if idle:
                self._stop()
                return

    def _stop(self):
        with self.lock:
            server, self.server, self.address = self.server, None, None
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
            log("sending port closed")


def http_get_range(host, port, path, start, end, headers=None, timeout=30.0, context=None):
    """GET bytes [start, end] from a peer over HTTPS (its certificate must come
    from the cluster CA, ``context``); returns the body or raises."""
    if context is None:
        raise ApiError(0, "no cluster certificate to check the other device with")
    conn = httplib.HTTPSConnection(host, port, timeout=timeout, context=context)
    try:
        hdrs = {"Range": "bytes=%d-%d" % (start, end), "Connection": "close"}
        hdrs.update(headers or {})
        conn.request("GET", path, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        if resp.status not in (200, 206):
            raise ApiError(resp.status, "peer answered %d" % resp.status)
        if resp.status == 200:
            data = data[start:end + 1]
        return data
    finally:
        conn.close()


class PieceDownload(object):
    """Fetch one file from several sources at once, piece by piece, checking
    every piece against the SHA-256 the coordinator computed (SPEC 8.3)."""

    def __init__(self, worker, file_id, plan, dest, logger=None):
        self.worker = worker
        self.file_id = file_id
        self.size = int(plan.get("size") or 0)
        self.piece = int(plan.get("piece_bytes") or 4 * 1024 * 1024)
        self.hashes = list(plan.get("pieces") or [])
        self.sources = list(plan.get("sources") or [])
        self.dest = dest
        self.logger = logger
        self.lock = threading.Lock()
        self.todo = list(range(len(self.hashes)))
        self.done = set()
        self.stats = {}
        self.failed = None

    def _span(self, idx):
        start = idx * self.piece
        return start, min(self.size, start + self.piece) - 1

    def _take(self):
        with self.lock:
            return self.todo.pop(0) if self.todo else None

    def _store(self, idx, data, handle):
        with self.lock:
            handle.seek(idx * self.piece)
            handle.write(data)
            self.done.add(idx)

    def _from_peer(self, source, handle):
        parsed = urlparse.urlsplit(source["url"])
        stat = {"bytes": 0, "seconds": 0.0, "error": None}
        self.stats[source["offer_id"]] = stat
        began = time.time()
        while True:
            idx = self._take()
            if idx is None:
                break
            start, end = self._span(idx)
            try:
                data = http_get_range(parsed.hostname, parsed.port, parsed.path, start, end,
                                      {PEER_TOKEN_HEADER: source["token"]},
                                      context=self.worker.peer_context())
                if hashlib.sha256(data).hexdigest() != self.hashes[idx]:
                    raise ValueError("piece %d failed its hash check" % idx)
            except Exception as exc:
                with self.lock:
                    self.todo.append(idx)  # someone else gets this piece
                stat["error"] = "hash mismatch" if isinstance(exc, ValueError) else str(exc)
                break
            self._store(idx, data, handle)
            stat["bytes"] += len(data)
        stat["seconds"] = round(time.time() - began, 3)

    def _from_coordinator(self, handle):
        tries = 0
        while True:
            idx = self._take()
            if idx is None:
                return
            start, end = self._span(idx)
            try:
                status, data = self.worker.http.request(
                    "GET", "/api/w/files/%s" % urlparse.quote(self.file_id),
                    headers={"Range": "bytes=%d-%d" % (start, end)}, timeout=120.0)
                if status not in (200, 206):
                    raise ApiError(status, "download failed")
                if status == 200:
                    data = data[start:end + 1]
                if hashlib.sha256(data).hexdigest() != self.hashes[idx]:
                    raise ValueError("piece %d failed its hash check" % idx)
            except Exception as exc:
                with self.lock:
                    self.todo.append(idx)
                tries += 1
                if tries > 3:
                    self.failed = str(exc)
                    return
                time.sleep(0.5)
                continue
            self._store(idx, data, handle)

    def run(self):
        with open(self.dest, "wb") as handle:
            handle.truncate(self.size)
            threads = []
            for source in self.sources:
                thread = threading.Thread(target=self._from_peer, args=(source, handle), name="peer-get")
                thread.daemon = True
                thread.start()
                threads.append(thread)
            self._from_coordinator(handle)
            for thread in threads:
                thread.join(timeout=300)
            if self.failed is None and len(self.done) < len(self.hashes):
                self._from_coordinator(handle)  # pieces a dropped peer gave back late
        ok = self.failed is None and len(self.done) == len(self.hashes)
        for source in self.sources:
            stat = self.stats.get(source["offer_id"]) or {"bytes": 0, "seconds": 0.0, "error": "not used"}
            body = {"offer_id": source["offer_id"], "ok": stat["error"] is None, "bytes": stat["bytes"],
                    "seconds": stat["seconds"]}
            if stat["error"]:
                body["error"] = stat["error"]
            try:
                self.worker.http.call("POST", "/api/w/peer/result", body, timeout=10.0)
            except Exception as exc:
                dbg("peer result post failed: %s" % exc)
        if self.logger and ok:
            peer = sum(s["bytes"] for s in self.stats.values())
            self.logger("[worker] got %s: %.1f of %.1f MB from other phones"
                        % (self.file_id[:12], peer / 1048576.0, self.size / 1048576.0))
        return ok


# --------------------------------------------------------------------------
# log streaming
# --------------------------------------------------------------------------

class LogPump(object):
    """Batches task log lines and posts them every log_batch_ms."""

    def __init__(self, worker):
        self.worker = worker
        self.lock = threading.Lock()
        self.buffers = {}
        self.stopping = False
        self.thread = threading.Thread(target=self._loop, name="logpump")
        self.thread.daemon = True

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopping = True

    def open(self, task_id):
        with self.lock:
            self.buffers[task_id] = {"lines": [], "count": 0, "trunc": False}

    def count(self, task_id):
        with self.lock:
            state = self.buffers.get(task_id)
            return state["count"] if state else 0

    def add(self, task_id, line):
        with self.lock:
            state = self.buffers.get(task_id)
            if state is None:
                state = {"lines": [], "count": 0, "trunc": False}
                self.buffers[task_id] = state
            if state["trunc"]:
                return
            if state["count"] >= MAX_LOG_LINES:
                state["trunc"] = True
                state["lines"].append("... truncated (log line cap reached)")
                return
            state["count"] += 1
            state["lines"].append(line)

    def _drain(self, task_id):
        with self.lock:
            state = self.buffers.get(task_id)
            if not state or not state["lines"]:
                return []
            lines = state["lines"]
            state["lines"] = []
            return lines

    def flush(self, task_id):
        lines = self._drain(task_id)
        while lines:
            if not self._post(task_id, lines):
                return  # coordinator unreachable: lines stay buffered
            lines = self._drain(task_id)

    def close(self, task_id):
        self.flush(task_id)
        with self.lock:
            self.buffers.pop(task_id, None)

    def _post(self, task_id, lines):
        try:
            self.worker.http.call(
                "POST", "/api/w/task/%s/log" % task_id,
                {"lines": lines, "ts": time.time()}, timeout=20.0)
            dbg("log: posted %d line(s) for %s" % (len(lines), task_id))
            return True
        except Removed:
            raise
        except ApiError as exc:
            dbg("log post rejected for %s: %s" % (task_id, exc))
            return True  # the coordinator answered; do not retry
        except Exception as exc:
            dbg("log post failed for %s: %s" % (task_id, exc))
            # keep the lines for the next round instead of losing them
            with self.lock:
                state = self.buffers.get(task_id)
                if state is not None and len(state["lines"]) < MAX_LOG_LINES:
                    state["lines"] = lines + state["lines"]
            return False

    def _loop(self):
        while not self.stopping:
            try:
                interval = max(0.05, float(self.worker.log_batch_ms) / 1000.0)
                time.sleep(interval)
                with self.lock:
                    ids = list(self.buffers.keys())
                for task_id in ids:
                    lines = self._drain(task_id)
                    if lines:
                        self._post(task_id, lines)
            except Exception as exc:
                # never let the pump thread die
                dbg("log pump error: %s" % exc)
                dbg(traceback.format_exc())
                time.sleep(0.5)


# --------------------------------------------------------------------------
# process helpers
# --------------------------------------------------------------------------

def popen_kwargs():
    kwargs = {}
    if not IS_WINDOWS:
        kwargs["start_new_session"] = True
    return kwargs


def kill_tree(proc):
    if proc is None or proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.call(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
    except Exception as exc:
        dbg("kill_tree failed: %s" % exc)
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def batch_popen_kwargs():
    """Normal (batch) tasks run below normal priority, so a model answering a
    request on the same device always gets the CPU first (SPEC 9.7)."""
    kwargs = popen_kwargs()
    if IS_WINDOWS:
        kwargs["creationflags"] = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x4000)
    return kwargs


def lower_priority(proc):
    """POSIX: nice the task's whole process group (the child is a session leader)."""
    if IS_WINDOWS or proc is None:
        return
    try:
        os.setpriority(os.PRIO_PGRP, proc.pid, 10)
    except Exception as exc:
        dbg("could not lower priority: %s" % exc)


def _windows_tree(pid):
    """``pid`` and every process it started, from a Toolhelp snapshot."""
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_char * 260)]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.Process32First.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    k32.Process32Next.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return [pid]
    children = {}
    entry = Entry()
    entry.dwSize = ctypes.sizeof(Entry)
    try:
        ok = k32.Process32First(snap, ctypes.byref(entry))
        while ok:
            children.setdefault(int(entry.th32ParentProcessID), []).append(int(entry.th32ProcessID))
            ok = k32.Process32Next(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    out, stack = [], [int(pid)]
    while stack:
        current = stack.pop()
        if current in out:
            continue
        out.append(current)
        stack.extend(children.get(current, []))
    return out


def _windows_suspend(pid, resume):
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    ntdll = ctypes.windll.ntdll
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    call = ntdll.NtResumeProcess if resume else ntdll.NtSuspendProcess
    call.argtypes = [wintypes.HANDLE]
    for each in _windows_tree(pid):
        handle = k32.OpenProcess(0x0800, False, each)  # PROCESS_SUSPEND_RESUME
        if handle:
            call(handle)
            k32.CloseHandle(handle)


def pause_tree(proc, resume=False):
    """Freeze (or thaw) a task's process tree; it keeps its memory and state."""
    if proc is None or proc.poll() is not None:
        return False
    try:
        if IS_WINDOWS:
            _windows_suspend(proc.pid, resume)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGCONT if resume else signal.SIGSTOP)
        return True
    except Exception as exc:
        dbg("could not %s task: %s" % ("resume" if resume else "pause", exc))
        return False


#: a WASI program's stdout is its result; bigger results belong in /out
WASM_STDOUT_CAP = 16 * 1024 * 1024
#: wasmtime warns that --invoke with arguments is "experimental" on every call
WASMTIME_NOISE = re.compile(r"^warning: using `--invoke`")


def _number(text):
    text = text.strip()
    try:
        return int(text)
    except ValueError:
        pass
    try:
        value = float(text)
    except ValueError:
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return text  # NaN / infinity: keep the text, JSON has no such numbers
    return value


def parse_invoke_output(text):
    """wasmtime --invoke prints each returned value on its own line."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    values = [_number(line) for line in lines]
    if all(v is not None for v in values):
        return values[0] if len(values) == 1 else values
    last = _number(lines[-1])  # the function printed something before returning
    return last if last is not None else text


def _no_constants(name):
    raise ValueError("not JSON: %s" % name)  # NaN / Infinity, which browsers reject too


def parse_program_output(text):
    """A WASI program's stdout: JSON when it is JSON, else the text (the
    browser runner, runner.js parseProgramOutput, decides the same way)."""
    stripped = text.strip()
    if stripped[:1] in ("{", "[", '"') or stripped in ("true", "false", "null") or _number(stripped or "x") is not None:
        try:
            return json.loads(stripped, parse_constant=_no_constants)
        except ValueError:
            pass
    return text


class Tail(object):
    """Keeps the last `cap` bytes of a stream."""

    def __init__(self, cap):
        self.cap = cap
        self.buf = bytearray()

    def add(self, chunk):
        self.buf.extend(chunk)
        if len(self.buf) > self.cap:
            del self.buf[:len(self.buf) - self.cap]

    def text(self):
        return bytes(self.buf).decode("utf-8", "replace")


# --------------------------------------------------------------------------
# benchmark (SPEC 7)
# --------------------------------------------------------------------------

def run_benchmark():
    x = 42
    iters = 0
    batch = 20000
    start = time.time()
    end = start + 2.0
    while True:
        for _ in range(batch):
            x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        iters += batch
        left = end - time.time()
        if left <= 0:
            break
        rate = iters / max(1e-6, time.time() - start)
        batch = max(10000, min(2000000, int(rate * min(left, 0.25)) or 10000))
    buf = b"\xa5" * (1024 * 1024)
    rounds = 0
    hstart = time.time()
    hend = hstart + 1.0
    while time.time() < hend:
        hashlib.sha256(buf).digest()
        rounds += 1
    helapsed = max(1e-6, time.time() - hstart)
    return {
        "score": iters / 1e6,
        "loop_iters": iters,
        "hash_mb_s": round(rounds / helapsed, 3),
        "kind": "python",
    }


# --------------------------------------------------------------------------
# notifications
# --------------------------------------------------------------------------

WIN_TOAST_PS = (
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
    " ContentType=WindowsRuntime] > $null;"
    "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
    "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
    "$n = $t.GetElementsByTagName('text');"
    "$n.Item(0).AppendChild($t.CreateTextNode('%(title)s')) > $null;"
    "$n.Item(1).AppendChild($t.CreateTextNode('%(body)s')) > $null;"
    "$toast = [Windows.UI.Notifications.ToastNotification]::new($t);"
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
    "'PhoneCluster').Show($toast)"
)


def send_notification(title, body):
    title = str(title or "PhoneCluster")
    body = str(body or "")
    if shutil.which("termux-notification"):
        out = run_capture(
            ["termux-notification", "--title", title, "--content", body], timeout=15)
        if out is not None:
            return "termux"
    if IS_WINDOWS:
        script = WIN_TOAST_PS % {
            "title": title.replace("'", "''"),
            "body": body.replace("'", "''"),
        }
        out = run_capture(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            timeout=25)
        if out is not None:
            return "toast"
    log("[notify] %s: %s" % (title, body))
    return "print"


# --------------------------------------------------------------------------
# task execution (SPEC 6)
# --------------------------------------------------------------------------

class TaskRun(object):
    #: keep in step with coordinator/verify.py WORKER_ORDER
    IMPL_ORDER = ("python", "shell", "js", "wasm")

    def __init__(self, worker, task):
        self.worker = worker
        self.task = task or {}
        self.id = str(self.task.get("id"))
        self.payload = self.task.get("payload") or {}
        try:
            self.timeout = float(self.payload.get("timeout") or 600)
        except (TypeError, ValueError):
            self.timeout = 600.0
        self.cancelled = False
        self.proc = None
        self.workdir = None
        self.started = time.time()
        #: paused while this device answers a service call (SPEC 9.7); paused
        #: time counts neither against the timeout nor in the duration
        self.paused_at = None
        self.paused_total = 0.0
        self.progress = 0.0
        #: outputs whose upload hit a transport error; kept for the outbox
        self.pending_outputs = []
        self._pending_checkpoint = None
        self._checkpoint_timer = None
        self._checkpoint_sent_at = 0.0
        self._lock = threading.Lock()
        self.thread = threading.Thread(target=self._guarded, name="task-" + self.id)
        self.thread.daemon = True

    # -- lifecycle -------------------------------------------------------
    def start(self):
        self.thread.start()

    def cancel(self):
        self.cancelled = True
        with self._lock:
            proc = self.proc
        if proc is not None:
            kill_tree(proc)

    def pause(self):
        with self._lock:
            if self.paused_at is not None or self.proc is None:
                return
            if pause_tree(self.proc):
                self.paused_at = time.time()
        if self.paused_at is not None:
            self.log_line("[worker] paused: this device is answering a request")

    def unpause(self):
        with self._lock:
            if self.paused_at is None:
                return
            pause_tree(self.proc, resume=True)
            self.paused_total += time.time() - self.paused_at
            self.paused_at = None
        self.log_line("[worker] resumed")

    def paused_seconds(self):
        extra = (time.time() - self.paused_at) if self.paused_at is not None else 0.0
        return self.paused_total + extra

    def elapsed(self):
        """Working time so far (pauses excluded)."""
        return round(max(0.0, time.time() - self.started - self.paused_seconds()), 3)

    def _spawned(self, proc):
        """Bookkeeping right after a task process started."""
        lower_priority(proc)
        with self._lock:
            self.proc = proc
        if self.worker.held():
            self.pause()

    def _guarded(self):
        result = None
        try:
            result = self._execute()
        except Exception as exc:
            result = {
                "ok": False,
                "error": "%s: %s" % (exc.__class__.__name__, exc),
                "traceback": traceback.format_exc(),
                "duration": self.elapsed(),
            }
        log_lines = self.worker.log_pump.count(self.id)
        try:
            self.worker.log_pump.close(self.id)
        except Exception:
            pass
        if result is not None:
            result.setdefault("duration", self.elapsed())
            result["log_lines"] = log_lines
            if self.task.get("attempt") is not None:
                result["attempt"] = self.task.get("attempt")
            if not result.get("ok"):
                # let the next attempt resume from the latest progress
                self._flush_checkpoint_now()
            try:
                self.worker.deliver_result(self.id, result, self.pending_outputs)
            except Removed:
                raise
            except Exception as exc:
                warn("could not deliver result of %s: %s" % (self.id, exc))
        self._cleanup()
        self.worker.task_finished(self.id)

    def _cleanup(self):
        if self.workdir and os.path.isdir(self.workdir):
            shutil.rmtree(self.workdir, ignore_errors=True)

    # -- main body -------------------------------------------------------
    def _execute(self):
        self.worker.acquire_slot(lambda: self.cancelled)
        try:
            if self.cancelled:
                return {"ok": False, "error": "cancelled"}
            self.worker.log_pump.open(self.id)
            try:
                self.worker.http.call("POST", "/api/w/task/%s/start" % self.id, {},
                                      timeout=20.0)
            except Removed:
                raise
            except Exception as exc:
                dbg("task start post failed: %s" % exc)
            impls = self.payload.get("impls") or {}
            kind = None
            for candidate in self.IMPL_ORDER:
                if candidate in impls and candidate in self.worker.caps:
                    kind = candidate
                    break
            if kind is None:
                return {"ok": False,
                        "error": "no supported impl (have %s, offered %s)"
                                 % (",".join(self.worker.caps), ",".join(sorted(impls)))}
            self.workdir = mkdirs(os.path.join(self.worker.work_dir, self.id))
            inputs = self.payload.get("inputs") or []
            paths = self.worker.fetch_inputs(inputs, self.log_line, self.task.get("peer_inputs"))
            self.started = time.time()
            if kind == "python":
                result = self._run_python(impls["python"], inputs, paths)
            elif kind == "wasm":
                result = self._run_wasm(impls["wasm"], inputs, paths)
            else:
                result = self._run_shell(impls["shell"], inputs, paths)
            result["impl"] = kind  # checks compare like with like (SPEC 9.9)
            return result
        finally:
            self.worker.release_slot()

    def log_line(self, line):
        self.worker.log_pump.add(self.id, line)

    # -- checkpoints (SPEC 6.5) ------------------------------------------
    def save_checkpoint(self, text):
        """Queue a checkpoint (JSON text) for the coordinator; the latest one
        wins and posts are throttled to one per CHECKPOINT_MIN_INTERVAL."""
        if len(text) > CHECKPOINT_MAX_BYTES:
            self.log_line("[worker] checkpoint ignored: %d bytes > %d limit"
                          % (len(text), CHECKPOINT_MAX_BYTES))
            return
        try:
            state = json.loads(text)
        except ValueError:
            self.log_line("[worker] checkpoint ignored: not JSON")
            return
        with self._lock:
            self._pending_checkpoint = state
            due = self._checkpoint_sent_at + CHECKPOINT_MIN_INTERVAL
            if self._checkpoint_timer is not None:
                return  # a flush is already scheduled
            delay = max(0.0, due - time.time())
            self._checkpoint_timer = threading.Timer(delay, self._flush_checkpoint)
            self._checkpoint_timer.daemon = True
            self._checkpoint_timer.start()

    def _flush_checkpoint_now(self):
        with self._lock:
            timer = self._checkpoint_timer
            self._checkpoint_timer = None
        if timer is not None:
            timer.cancel()
        self._flush_checkpoint(force=True)

    def _flush_checkpoint(self, force=False):
        with self._lock:
            self._checkpoint_timer = None
            state = self._pending_checkpoint
            self._pending_checkpoint = None
            if state is None or (self.cancelled and not force):
                return
            self._checkpoint_sent_at = time.time()
        try:
            body = {"state": state}
            if self.task.get("attempt") is not None:
                body["attempt"] = self.task.get("attempt")
            self.worker.http.call("POST", "/api/w/task/%s/checkpoint" % self.id,
                                  body, timeout=30.0)
        except Removed:
            raise
        except ApiError as exc:
            dbg("checkpoint rejected for %s: %s" % (self.id, exc))
        except Exception as exc:
            dbg("checkpoint post failed for %s: %s" % (self.id, exc))

    def _watch_checkpoint_file(self, path, stop):
        """Shell tasks checkpoint by writing JSON to $PC_CHECKPOINT_FILE."""
        last = None
        while not stop.is_set():
            stop.wait(1.0)
            try:
                stat = os.stat(path)
                key = (stat.st_mtime, stat.st_size)
            except OSError:
                continue
            if key == last:
                continue
            last = key
            text = read_text(path)
            if text and text.strip():
                self.save_checkpoint(text.strip())

    # -- python impl -----------------------------------------------------
    def _run_python(self, impl, input_ids, paths):
        keys = [input_key(i) for i in input_ids]
        spec = {
            "code": impl.get("code") or "",
            "entry": impl.get("entry") or "run",
            "args": impl.get("args") or {},
            "inputs": dict(zip(keys, paths)),
            "input_order": keys,
            "workdir": self.workdir,
            "task_id": self.id,
            "device_name": self.worker.name,
            "checkpoint": self.task.get("checkpoint"),
        }
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        # -u / -X utf8 as flags: a Python started with a ._pth file (the PC's own
        # copy in tools\python) ignores PYTHONUNBUFFERED / PYTHONIOENCODING
        cmd = [sys.executable, "-u", "-X", "utf8", "-c", HARNESS]
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, cwd=self.workdir, env=env, **batch_popen_kwargs())
        self._spawned(proc)
        holder = {"result": None, "error": None, "have_result": False,
                  "work_started": None, "paused_before": 0.0}

        def reader():
            try:
                for raw in iter(proc.stdout.readline, b""):
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if line == SENT_WORK:
                        holder["work_started"] = time.time()
                        holder["paused_before"] = self.paused_seconds()
                        continue
                    if line.startswith(SENT_RESULT):
                        try:
                            holder["result"] = json.loads(line[len(SENT_RESULT):])
                            holder["have_result"] = True
                        except ValueError as exc:
                            holder["error"] = {"error": "bad result json: %s" % exc,
                                               "traceback": line[:2000]}
                        continue
                    if line.startswith(SENT_ERROR):
                        try:
                            holder["error"] = json.loads(line[len(SENT_ERROR):])
                        except ValueError:
                            holder["error"] = {"error": line[len(SENT_ERROR):][:500],
                                               "traceback": ""}
                        continue
                    if line.startswith(SENT_PROGRESS):
                        try:
                            self.progress = float(line[len(SENT_PROGRESS):].strip())
                        except ValueError:
                            pass
                        self.log_line(line)
                        continue
                    if line.startswith(SENT_CHECKPOINT):
                        self.save_checkpoint(line[len(SENT_CHECKPOINT):])
                        continue
                    if line:
                        self.log_line(line)
            except Exception as exc:
                dbg("python reader stopped: %s" % exc)
            finally:
                try:
                    proc.stdout.close()
                except Exception:
                    pass

        rthread = threading.Thread(target=reader, name="rd-" + self.id)
        rthread.daemon = True
        rthread.start()
        try:
            proc.stdin.write(json.dumps(spec).encode("utf-8"))
            proc.stdin.close()
        except Exception as exc:
            dbg("stdin write failed: %s" % exc)
        state = self._wait(proc)
        rthread.join(timeout=10)
        duration = self.elapsed()
        if state != "ok":
            return {"ok": False, "error": state, "traceback": "", "duration": duration}
        if holder["error"]:
            err = holder["error"]
            return {"ok": False, "error": str(err.get("error") or "task failed"),
                    "traceback": str(err.get("traceback") or ""), "duration": duration}
        if not holder["have_result"]:
            return {"ok": False,
                    "error": "task produced no result (exit code %s)" % proc.returncode,
                    "traceback": "", "duration": duration}
        result = {"ok": True, "result": holder["result"], "outputs": [], "duration": duration}
        if holder.get("work_started"):
            # time spent in the task's own code (no start-up, no pauses): its speed
            result["work_duration"] = round(max(0.0, time.time() - holder["work_started"]
                                                - (self.paused_seconds() - holder["paused_before"])), 4)
        result["outputs"] = self._upload_registered_outputs()
        return result

    def _upload_registered_outputs(self):
        manifest = os.path.join(self.workdir or ".", "__pc_outputs.json")
        items = []
        text = read_text(manifest)
        if text:
            try:
                items = json.loads(text) or []
            except ValueError:
                items = []
        outputs = []
        for item in items:
            path = item.get("path")
            name = item.get("name") or (os.path.basename(path) if path else None)
            if not path or not os.path.isfile(path):
                self.log_line("[worker] missing output file: %s" % path)
                continue
            uploaded = self._upload_output(path, name)
            if uploaded is not None:
                outputs.append(uploaded)
        return outputs

    def _upload_output(self, path, name):
        """Upload one output; on a transport error keep it for the outbox."""
        try:
            return self.worker.upload_file(path, name)
        except Removed:
            raise
        except ApiError as exc:
            self.log_line("[worker] output upload rejected (%s): %s" % (name, exc))
            return None
        except Exception as exc:
            dbg("output upload of %s deferred to the outbox: %s" % (name, exc))
            self.pending_outputs.append({"path": path, "name": name})
            return None

    # -- shell impl ------------------------------------------------------
    def _run_shell(self, impl, input_ids, paths):
        cmd = impl.get("cmd") or ""
        if IS_WINDOWS and impl.get("cmd_windows"):
            cmd = impl.get("cmd_windows")
        out_dir = mkdirs(os.path.join(self.workdir, "out"))
        env = dict(os.environ)
        extra = impl.get("env") or {}
        for key in extra:
            env[str(key)] = str(extra[key])
        env["PC_TASK_ID"] = self.id
        env["PC_WORKDIR"] = self.workdir
        env["PC_OUTPUT_DIR"] = out_dir
        env["PC_DEVICE_NAME"] = str(self.worker.name)
        for idx, path in enumerate(paths):
            env["PC_INPUT_%d" % idx] = path
        env["PC_INPUTS"] = os.pathsep.join(paths)
        # Checkpoints (SPEC 6.5): previous state in, new state via a file.
        ckpt_file = os.path.join(self.workdir, "checkpoint.json")
        env["PC_CHECKPOINT_FILE"] = ckpt_file
        if self.task.get("checkpoint") is not None:
            env["PC_CHECKPOINT"] = json.dumps(self.task.get("checkpoint"))
            try:
                with open(ckpt_file, "w") as handle:
                    handle.write(env["PC_CHECKPOINT"])
            except Exception:
                pass
        ffmpeg = self.worker.ffmpeg_path
        if ffmpeg:
            env["PC_FFMPEG"] = ffmpeg
            bindir = os.path.dirname(ffmpeg)
            if bindir and bindir not in (env.get("PATH") or ""):
                env["PATH"] = bindir + os.pathsep + (env.get("PATH") or "")
        cwd = impl.get("cwd") or self.workdir
        proc = subprocess.Popen(
            cmd, shell=True, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **batch_popen_kwargs())
        self._spawned(proc)
        stop_watch = threading.Event()
        watcher = threading.Thread(target=self._watch_checkpoint_file,
                                   args=(ckpt_file, stop_watch), name="ckpt-" + self.id)
        watcher.daemon = True
        watcher.start()
        out_tail = Tail(STDOUT_CAP)
        err_tail = Tail(STDERR_CAP)
        threads = [
            self._pipe_reader(proc.stdout, out_tail, ""),
            self._pipe_reader(proc.stderr, err_tail, "[stderr] "),
        ]
        state = self._wait(proc)
        stop_watch.set()
        for thread in threads:
            thread.join(timeout=10)
        # pick up a checkpoint written just before exit (the watcher polls at 1 s)
        final = read_text(ckpt_file)
        if final and final.strip() and state != "ok":
            self.save_checkpoint(final.strip())
        duration = self.elapsed()
        if state != "ok":
            return {"ok": False, "error": state, "traceback": err_tail.text()[-4000:],
                    "duration": duration}
        code = proc.returncode
        result = {"exit_code": code, "stdout": out_tail.text(), "stderr": err_tail.text()}
        outputs = self._upload_dir(out_dir)
        if code == 0:
            return {"ok": True, "result": result, "outputs": outputs,
                    "duration": duration}
        return {"ok": False, "error": "command exited with code %s" % code,
                "traceback": err_tail.text()[-4000:], "result": result,
                "outputs": outputs, "duration": duration}

    # -- WebAssembly impl (SPEC 6.9) -------------------------------------
    def _run_wasm(self, impl, input_ids, paths):
        """Run a module with wasmtime: call an exported function with numbers,
        or run it as a WASI program with /in, /out, stdin and arguments.  The
        module sees nothing else: no other files, no network."""
        runtime = self.worker.wasm_runtime(self.log_line)
        module = paths[int(impl.get("module") or 0)]
        call = impl.get("call")
        limit = max(16, int(impl.get("memory_mb") or 256)) * 1024 * 1024
        cmd = [runtime, "run", "-W", "max-memory-size=%d" % limit]
        out_dir = mkdirs(os.path.join(self.workdir, "out"))
        stdin_path = None
        stdin_text = None
        if call:
            cmd += ["--invoke", str(call), module] + [str(a) for a in impl.get("args") or []]
        else:
            in_dir = mkdirs(os.path.join(self.workdir, "in"))
            names = impl.get("names") or {}
            for idx in impl.get("files") or []:
                name = os.path.basename(str(names.get(str(idx)) or "input-%s" % idx)) or "input"
                shutil.copyfile(paths[int(idx)], os.path.join(in_dir, name))
            cmd += ["--dir", in_dir + "::/in", "--dir", out_dir + "::/out"]
            env = impl.get("env") or {}
            for key in sorted(env):
                cmd += ["--env", "%s=%s" % (key, env[key])]
            cmd += [module] + [str(a) for a in impl.get("args") or []]
            if impl.get("stdin_input") is not None:
                stdin_path = paths[int(impl["stdin_input"])]
            elif impl.get("stdin"):
                stdin_text = str(impl["stdin"]).encode("utf-8")
        stdin_handle = open(stdin_path, "rb") if stdin_path else None
        try:
            proc = subprocess.Popen(
                cmd, cwd=self.workdir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=stdin_handle or (subprocess.PIPE if stdin_text else subprocess.DEVNULL),
                **batch_popen_kwargs())
        finally:
            if stdin_handle is not None:
                stdin_handle.close()
        self._spawned(proc)
        if stdin_text:
            def feed():
                try:
                    proc.stdin.write(stdin_text)
                    proc.stdin.close()
                except Exception:
                    pass
            threading.Thread(target=feed, name="wasm-stdin", daemon=True).start()
        out = {"data": bytearray(), "over": False}

        def collect():
            try:
                for block in iter(lambda: proc.stdout.read(65536), b""):
                    if len(out["data"]) + len(block) > WASM_STDOUT_CAP:
                        out["over"] = True
                        continue
                    out["data"].extend(block)
            except Exception as exc:
                dbg("wasm stdout reader stopped: %s" % exc)

        err_tail = Tail(STDERR_CAP)
        threads = [threading.Thread(target=collect, name="wasm-out-" + self.id),
                   self._pipe_reader(proc.stderr, err_tail, "[stderr] ", skip=WASMTIME_NOISE)]
        threads[0].daemon = True
        threads[0].start()
        state = self._wait(proc)
        for thread in threads:
            thread.join(timeout=10)
        duration = self.elapsed()
        if state != "ok":
            return {"ok": False, "error": state, "traceback": err_tail.text()[-4000:], "duration": duration}
        if proc.returncode != 0:
            return {"ok": False, "error": "WebAssembly program exited with code %s" % proc.returncode,
                    "traceback": err_tail.text()[-4000:], "duration": duration}
        if out["over"]:
            return {"ok": False, "error": "output larger than %d MB: write big results to /out"
                    % (WASM_STDOUT_CAP // 1048576), "traceback": "", "duration": duration}
        text = bytes(out["data"]).decode("utf-8", "replace")
        result = parse_invoke_output(text) if call else parse_program_output(text)
        return {"ok": True, "result": result, "outputs": self._upload_dir(out_dir), "duration": duration}

    def _pipe_reader(self, pipe, tail, prefix, skip=None):
        def reader():
            try:
                for raw in iter(pipe.readline, b""):
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if skip is not None and skip.search(line):
                        continue  # the runtime's own chatter, not the program's
                    tail.add(raw)
                    if line:
                        self.log_line(prefix + line)
            except Exception as exc:
                dbg("pipe reader stopped: %s" % exc)
            finally:
                try:
                    pipe.close()
                except Exception:
                    pass

        thread = threading.Thread(target=reader, name="pipe-" + self.id)
        thread.daemon = True
        thread.start()
        return thread

    def _upload_dir(self, directory):
        outputs = []
        if not os.path.isdir(directory):
            return outputs
        for root, _dirs, files in os.walk(directory):
            for fname in sorted(files):
                path = os.path.join(root, fname)
                rel = os.path.relpath(path, directory).replace(os.sep, "/")
                uploaded = self._upload_output(path, rel)
                if uploaded is not None:
                    outputs.append(uploaded)
        return outputs

    # -- shared ----------------------------------------------------------
    def _wait(self, proc):
        """Returns 'ok', 'cancelled' or a timeout message.  ``timeout <= 0``
        means no deadline (services, SPEC 6.7).  Paused time does not count."""
        while True:
            try:
                proc.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                pass
            except Exception:
                break
            if self.cancelled:
                kill_tree(proc)
                return "cancelled"
            if self.timeout > 0 and self.elapsed() > self.timeout:
                kill_tree(proc)
                return "timeout after %.0fs" % self.timeout
            if self.worker.stopping:
                kill_tree(proc)
                return "worker shutting down"
        if self.cancelled:
            return "cancelled"
        return "ok"


# --------------------------------------------------------------------------
# terminal (SPEC 8 / 10)
# --------------------------------------------------------------------------

class TerminalSession(object):
    def __init__(self, worker, session_id, shell=None):
        self.worker = worker
        self.id = str(session_id)
        self.shell = shell or default_shell()
        self.proc = None
        self.closed = False
        self.buf = bytearray()
        self.lock = threading.Lock()

    def start(self):
        cmd = [self.shell]
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0, cwd=os.path.expanduser("~"),
            **popen_kwargs())
        for target, name in ((self._read_loop, "term-rd"), (self._flush_loop, "term-fl")):
            thread = threading.Thread(target=target, name=name + self.id)
            thread.daemon = True
            thread.start()

    def write(self, data):
        if self.proc is None or self.proc.poll() is not None:
            return
        try:
            text = data if isinstance(data, bytes) else str(data).encode("utf-8")
            self.proc.stdin.write(text)
            self.proc.stdin.flush()
        except Exception as exc:
            dbg("term write failed: %s" % exc)

    def close(self):
        self.closed = True
        kill_tree(self.proc)

    def _read_loop(self):
        fd = None
        try:
            fd = self.proc.stdout.fileno()
        except Exception:
            return
        while True:
            try:
                chunk = os.read(fd, 4096)
            except Exception:
                break
            if not chunk:
                break
            with self.lock:
                self.buf.extend(chunk)
        self._finish()

    def _flush_loop(self):
        while not self.closed:
            time.sleep(0.05)
            self._flush()
            if self.proc is not None and self.proc.poll() is not None:
                time.sleep(0.1)
                self._flush()
                return

    def _flush(self):
        with self.lock:
            if not self.buf:
                return
            data = bytes(self.buf)
            self.buf = bytearray()
        text = data.decode("utf-8", "replace")
        try:
            self.worker.http.call("POST", "/api/w/term/%s/output" % self.id,
                                  {"data": text}, timeout=20.0)
        except Exception as exc:
            dbg("term output post failed: %s" % exc)

    def _finish(self):
        self._flush()
        if self.closed and self.proc is None:
            return
        code = None
        try:
            code = self.proc.poll()
            if code is None:
                code = self.proc.wait(timeout=5)
        except Exception:
            code = -1
        self.closed = True
        try:
            self.worker.http.call("POST", "/api/w/term/%s/closed" % self.id,
                                  {"exit_code": code}, timeout=20.0)
        except Exception as exc:
            dbg("term closed post failed: %s" % exc)
        self.worker.terminal_gone(self.id)


# --------------------------------------------------------------------------
# provisioning (SPEC 6.8): llama.cpp runtime and model files for services
# --------------------------------------------------------------------------

LLAMA_REPO = "https://github.com/ggml-org/llama.cpp"
LLAMA_BINARIES = ("llama-server", "ggml-rpc-server", "rpc-server")


def llama_platform(info=None):
    """Which prebuilt llama.cpp this device can use, or 'source' to build it."""
    info = info or {}
    abi = info.get("abi") or cpu_abi()
    if IS_WINDOWS:
        return "win-x64" if abi == "x86_64" else "source"
    api = info.get("android_api")
    if api is None and is_android():
        api = android_api()
    if api is not None:
        # the official Android build targets API 28 (Android 9) and arm64
        return "android-arm64" if abi == "arm64-v8a" and int(api) >= 28 else "source"
    return "source"


def _safe_extract_tar(tar, dest):
    base = os.path.realpath(dest)
    for member in tar.getmembers():
        target = os.path.realpath(os.path.join(dest, member.name))
        if not (target == base or target.startswith(base + os.sep)):
            raise ValueError("unsafe path in archive: %s" % member.name)
        if member.islnk() or member.issym():
            link = os.path.realpath(os.path.join(os.path.dirname(target), member.linkname))
            if not link.startswith(base + os.sep):
                raise ValueError("unsafe link in archive: %s" % member.name)
    tar.extractall(dest)


def _flatten_single_dir(path):
    """If ``path`` holds exactly one directory, move its contents up."""
    entries = os.listdir(path)
    if len(entries) != 1:
        return
    inner = os.path.join(path, entries[0])
    if not os.path.isdir(inner):
        return
    for name in os.listdir(inner):
        shutil.move(os.path.join(inner, name), os.path.join(path, name))
    os.rmdir(inner)


def find_binary(bin_dir, names):
    """First existing executable among ``names`` (``.exe`` on Windows)."""
    for name in names:
        for candidate in (name + ".exe", name) if IS_WINDOWS else (name,):
            path = os.path.join(bin_dir, candidate)
            if os.path.isfile(path):
                return path
    return None


class Provisioner(object):
    """Fetches what a service needs before its code runs: a pinned llama.cpp
    build and GGUF model files.  Everything lands under the worker's state
    dir (``runtime/`` and ``models/``) and is reused by later services."""

    def __init__(self, worker, log_line):
        self.worker = worker
        self.log_line = log_line
        self.runtime_root = mkdirs(os.path.join(worker.state_dir, "runtime"))
        self.models_root = mkdirs(os.path.join(worker.state_dir, "models"))

    # -- downloads -------------------------------------------------------
    def download(self, path, dest, size=None, label=None):
        """Stream GET ``path`` to ``dest`` (resuming a ``.part`` file)."""
        part = dest + ".part"
        have = os.path.getsize(part) if os.path.isfile(part) else 0
        headers = {"Range": "bytes=%d-" % have} if have else None
        status, resp = self.worker.http.open_stream("GET", path, timeout=120.0, headers=headers)
        try:
            if status == 410:
                raise Removed(status, "removed")
            if status not in (200, 206):
                raise ApiError(status, resp.read().decode("utf-8", "replace"))
            mode = "ab" if status == 206 and have else "wb"
            if mode == "wb":
                have = 0
            total = size or (have + int(resp.getheader("Content-Length") or 0))
            step = max(total // 10, 1) if total else 0
            next_mark = have + step if step else None
            with open(part, mode) as out:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    have += len(chunk)
                    if next_mark and have >= next_mark:
                        self.log_line("[provision] %s: %d%% (%.0f MB)"
                                      % (label or os.path.basename(dest),
                                         100 * have // max(total, 1), have / 1048576.0))
                        next_mark += step
        finally:
            try:
                resp.close()
            except Exception:
                pass
        if size and have != size:
            raise IOError("download of %s incomplete: %d of %d bytes" % (dest, have, size))
        replace_file(part, dest)
        return dest

    # -- llama.cpp -------------------------------------------------------
    def llama(self, tag, variant=None):
        """Directory holding llama-server / ggml-rpc-server for ``tag``.

        ``variant="gpu"``: the project's Vulkan build, which only the PC worker
        has (``tools/llama.cpp/win-vulkan-x64``)."""
        # the PC worker can use the coordinator's own copy directly
        root = os.environ.get("PC_ROOT")
        if variant == "gpu":
            build = gpu_build_dir()
            local = os.path.join(root or "", "tools", "llama.cpp")
            version = (read_text(os.path.join(local, "VERSION")) or "").strip()
            if not build:
                raise RuntimeError("no GPU build of llama.cpp on this device (scripts\\get-llama.ps1 -Vulkan)")
            if version != tag:
                raise RuntimeError("GPU build is llama.cpp %s, the service wants %s" % (version, tag))
            return build
        if root and IS_WINDOWS:
            local = os.path.join(root, "tools", "llama.cpp")
            version = (read_text(os.path.join(local, "VERSION")) or "").strip()
            win = os.path.join(local, "win-x64")
            if version == tag and find_binary(win, ("llama-server",)):
                return win
        dest = os.path.join(self.runtime_root, "llama-%s" % tag)
        if os.path.isfile(os.path.join(dest, ".ok")):
            return dest
        platform_key = llama_platform(self.worker.info)
        self.log_line("[provision] llama.cpp %s for %s (%s)" % (tag, self.worker.name, platform_key))
        if platform_key == "source":
            self._build_llama(tag, dest)
        else:
            self._fetch_llama(tag, platform_key, dest)
        if not find_binary(dest, ("llama-server",)) or not find_binary(dest, ("ggml-rpc-server", "rpc-server")):
            raise RuntimeError("llama.cpp %s at %s lacks llama-server or the rpc server" % (tag, dest))
        with open(os.path.join(dest, ".ok"), "w") as handle:
            handle.write(tag)
        return dest

    def _fetch_llama(self, tag, platform_key, dest):
        manifest = self.worker.http.call("GET", "/api/w/runtime/llama", timeout=30.0)
        if manifest.get("tag") != tag:
            raise RuntimeError("coordinator has llama.cpp %s, the service wants %s"
                               % (manifest.get("tag"), tag))
        asset = (manifest.get("assets") or {}).get(platform_key)
        if not asset:
            raise RuntimeError("coordinator has no llama.cpp build for %s (run scripts\\get-llama.ps1)"
                               % platform_key)
        archive = os.path.join(self.runtime_root, asset["file"])
        if not (os.path.isfile(archive) and os.path.getsize(archive) == int(asset.get("size") or -1)):
            self.download("/api/w/runtime/llama/%s" % urlparse.quote(asset["file"]), archive,
                          size=int(asset.get("size") or 0) or None, label=asset["file"])
        staging = dest + ".tmp"
        shutil.rmtree(staging, ignore_errors=True)
        mkdirs(staging)
        if archive.endswith(".zip"):
            import zipfile
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(staging)
        else:
            import tarfile
            with tarfile.open(archive, "r:*") as tar:
                _safe_extract_tar(tar, staging)
        _flatten_single_dir(staging)
        if not IS_WINDOWS:
            for name in os.listdir(staging):
                path = os.path.join(staging, name)
                if os.path.isfile(path) and "." not in name:
                    os.chmod(path, 0o755)
        shutil.rmtree(dest, ignore_errors=True)
        os.rename(staging, dest)
        remove(archive)  # extracted; keep the phone's storage free
        self.log_line("[provision] llama.cpp %s installed in %s" % (tag, dest))

    def _run(self, cmd, cwd=None, env=None):
        self.log_line("[build] $ %s" % cmd)
        proc = subprocess.Popen(cmd, shell=True, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        last = []
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                last = (last + [line])[-20:]
                # keep the task log readable: compiler progress lines only now and then
                if not line.startswith("[") or "%]" not in line or line.endswith("0%]"):
                    self.log_line("[build] " + line[:300])
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError("command failed (%s): %s\n%s" % (proc.returncode, cmd, "\n".join(last)))

    def _build_llama(self, tag, dest):
        """Compile llama.cpp ``tag`` on the device (older Android, 32-bit, Linux)."""
        self.log_line("[build] no prebuilt llama.cpp fits this device; compiling %s "
                      "(one time, 15-40 minutes on a phone)" % tag)
        if not shutil.which("cmake") or not shutil.which("git") or not (shutil.which("clang") or shutil.which("cc")):
            if shutil.which("pkg") and is_termux():
                self._run("pkg install -y git cmake clang make")
            else:
                raise RuntimeError("git, cmake and a C++ compiler are needed to build llama.cpp here")
        src = os.path.join(self.runtime_root, "src-%s" % tag)
        if not os.path.isdir(os.path.join(src, ".git")):
            shutil.rmtree(src, ignore_errors=True)
            self._run("git clone --depth 1 --branch %s %s %s" % (tag, LLAMA_REPO, src))
        build = os.path.join(src, "build")
        jobs = max(1, min(4, (os.cpu_count() or 2) // 2))
        self._run("cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DGGML_RPC=ON -DGGML_NATIVE=ON "
                  "-DLLAMA_BUILD_SERVER=ON -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF "
                  "-DLLAMA_CURL=OFF -DBUILD_SHARED_LIBS=OFF", cwd=src)
        try:
            self._run("cmake --build build --config Release -j %d --target llama-server ggml-rpc-server" % jobs, cwd=src)
        except RuntimeError:
            self._run("cmake --build build --config Release -j %d --target llama-server rpc-server" % jobs, cwd=src)
        bin_dir = os.path.join(build, "bin")
        mkdirs(dest)
        for name in os.listdir(bin_dir):
            shutil.copy2(os.path.join(bin_dir, name), os.path.join(dest, name))
        shutil.rmtree(build, ignore_errors=True)
        self.log_line("[build] llama.cpp %s built into %s" % (tag, dest))

    # -- models ----------------------------------------------------------
    def model(self, name, size):
        """Local path of GGUF model ``name`` (downloaded once, resumable)."""
        safe = os.path.basename(str(name))
        root = os.environ.get("PC_ROOT")
        if root:
            local = os.path.join(root, "data", "models", safe)
            if os.path.isfile(local) and (not size or os.path.getsize(local) == int(size)):
                return local
        dest = os.path.join(self.models_root, safe)
        if os.path.isfile(dest) and (not size or os.path.getsize(dest) == int(size)):
            return dest
        free = free_mb(self.models_root)
        if size and free is not None and free * 1048576 < int(size) * 1.05:
            raise RuntimeError("not enough storage for %s: need %.0f MB, %d MB free"
                               % (safe, int(size) / 1048576.0, free))
        self.log_line("[provision] downloading model %s (%.0f MB)" % (safe, (int(size) if size else 0) / 1048576.0))
        return self.download("/api/w/models/%s" % urlparse.quote(safe), dest, size=int(size) if size else None,
                             label=safe)


# --------------------------------------------------------------------------
# services (SPEC 6.7): a task that hosts a model and answers requests
# --------------------------------------------------------------------------

SENT_READY = "__PC_READY__"
SENT_PARTIAL = "__PC_PARTIAL__"
SENT_DONE = "__PC_DONE__"
SERVICE_CHUNK = 64 * 1024
SERVICE_RESTART_DELAY = 3.0


class ServiceRun(TaskRun):
    """Runs the service harness for as long as the task lives, restarting the
    child if it crashes, and relays requests handed over by the coordinator:
    input bytes are pulled from ``GET /api/w/requests/{id}/input`` and fed to
    the child as frames; partials and the final result are posted back."""

    def __init__(self, worker, task):
        TaskRun.__init__(self, worker, task)
        self.timeout = 0.0  # services have no deadline
        impl = (self.payload.get("impls") or {}).get("python") or {}
        self.service = self.payload.get("service") or {}
        self.name = str(self.service.get("name") or "service")
        self.code = impl.get("code") or ""
        self.request_timeout = float(self.service.get("request_timeout") or 300)
        self.queue = []                 # pending request metas
        self._queue_cond = threading.Condition()
        self._ready = threading.Event()
        self._done_event = threading.Event()
        self._done_payload = None
        self._fatal = None
        self.current = None             # request meta being served
        self.requests_done = 0
        self.child_lock = threading.Lock()
        self.runtime_dir = None         # llama.cpp binaries, when the service needs them
        self.model_path = None          # local GGUF path, when the service needs one
        self._announce = {}             # facts load() published with ctx.announce()
        self.ready_meta = None          # what we told the coordinator when ready

    # -- coordinator commands -------------------------------------------
    def enqueue(self, meta):
        with self._queue_cond:
            self.queue.append(meta)
            self._queue_cond.notify_all()

    def cancel_request(self, request_id):
        with self._queue_cond:
            before = len(self.queue)
            self.queue = [m for m in self.queue if str(m.get("id")) != str(request_id)]
            if len(self.queue) != before:
                return
        if self.current and str(self.current.get("id")) == str(request_id):
            # the only way to interrupt Python mid-request is to drop the child
            self.log_line("[service] request %s cancelled, restarting the model" % request_id)
            self._kill_child()

    # -- lifecycle -------------------------------------------------------
    def _execute(self):
        # services live outside the normal job slots: the coordinator decides
        # where models may run (the "AI models" role) and keeps normal tasks
        # beside a model to one (SPEC 9.3)
        try:
            if self.cancelled:
                return {"ok": False, "error": "cancelled"}
            self.worker.log_pump.open(self.id)
            try:
                self.worker.http.call("POST", "/api/w/task/%s/start" % self.id, {}, timeout=20.0)
            except Removed:
                raise
            except Exception as exc:
                dbg("task start post failed: %s" % exc)
            self.workdir = mkdirs(os.path.join(self.worker.work_dir, self.id))
            inputs = self.payload.get("inputs") or []
            paths = self.worker.fetch_inputs(inputs, self.log_line, self.task.get("peer_inputs"))
            # llama.cpp runtime / GGUF model the service code expects (SPEC 6.8)
            needs = self.service.get("needs") or {}
            provisioner = Provisioner(self.worker, self.log_line)
            if needs.get("llama"):
                self.runtime_dir = provisioner.llama(str(needs["llama"]), needs.get("llama_variant"))
            if needs.get("model"):
                model = needs["model"]
                self.model_path = provisioner.model(model.get("name"), model.get("size"))
            if self.cancelled:
                return {"ok": False, "error": "cancelled"}
            self.started = time.time()
            setup = self.service.get("setup_cmd")
            if setup:
                self.log_line("[service] running setup: %s" % setup)
                code = self._run_setup(setup)
                if code != 0:
                    return {"ok": False, "error": "setup_cmd exited with code %s" % code}
            restarts = 0
            while not self.cancelled and not self.worker.stopping:
                outcome = self._serve_once(inputs, paths)
                if outcome == "cancelled" or self.cancelled:
                    return {"ok": False, "error": "cancelled"}
                if outcome == "stopping":
                    return {"ok": False, "error": "worker shutting down"}
                restarts += 1
                if restarts > 5 and time.time() - self.started < 120:
                    return {"ok": False, "error": "service keeps crashing: %s" % (self._fatal or outcome)}
                self.log_line("[service] model process ended (%s); restarting in %.0fs"
                              % (self._fatal or outcome, SERVICE_RESTART_DELAY))
                time.sleep(SERVICE_RESTART_DELAY)
            return {"ok": False, "error": "cancelled"}
        finally:
            self.ready_meta = None  # never re-announce a service that ended

    def _run_setup(self, cmd):
        env = dict(os.environ)
        env["PC_SERVICE_DIR"] = self._service_dir()
        proc = subprocess.Popen(cmd, shell=True, cwd=self.workdir, env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, **popen_kwargs())
        with self._lock:
            self.proc = proc
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line:
                    self.log_line("[setup] " + line)
        finally:
            proc.wait()
            with self._lock:
                self.proc = None
        return proc.returncode

    def _service_dir(self):
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in self.name)
        return mkdirs(os.path.join(self.worker.state_dir, "services", safe))

    def _serve_once(self, inputs, paths):
        """Start the child, announce ready, serve requests until it dies."""
        keys = [input_key(i) for i in inputs]
        spec = {
            "code": self.code,
            "inputs": dict(zip(keys, paths)),
            "input_order": keys,
            "workdir": self.workdir,
            "service_dir": self._service_dir(),
            "service_name": self.name,
            "service_params": self.service.get("params") or {},
            "task_id": self.id,
            "device_name": self.worker.name,
            "runtime_dir": self.runtime_dir,
            "model_path": self.model_path,
            "coordinator_host": self.worker.http.host,
        }
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        self._ready.clear()
        self._fatal = None
        self._announce = {}
        self.ready_meta = None   # not ready until the new child says so
        proc = subprocess.Popen(
            [sys.executable, "-u", "-X", "utf8", "-c", SERVICE_HARNESS], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=self.workdir,
            env=env, **popen_kwargs())
        with self._lock:
            self.proc = proc
        reader = threading.Thread(target=self._read_child, args=(proc,), name="svc-rd-" + self.id)
        reader.daemon = True
        reader.start()
        try:
            proc.stdin.write((json.dumps(spec) + "\n").encode("utf-8"))
            proc.stdin.flush()
        except Exception as exc:
            dbg("service spec write failed: %s" % exc)
        # wait for load() to finish
        while proc.poll() is None and not self._ready.is_set():
            if self.cancelled:
                kill_tree(proc)
                return "cancelled"
            if self.worker.stopping:
                kill_tree(proc)
                return "stopping"
            self._ready.wait(0.2)
        if self._fatal is not None or proc.poll() is not None:
            kill_tree(proc)
            reader.join(timeout=5)
            return "load failed: %s" % (self._fatal or "process exited")
        self.log_line("[service] %s ready on %s" % (self.name, self.worker.name))
        meta = dict(self.service.get("meta") or {})
        meta.update(self._announce or {})
        self.ready_meta = meta
        self.announce_ready()
        # request loop
        while proc.poll() is None:
            if self.cancelled:
                self._send_quit(proc)
                kill_tree(proc)
                return "cancelled"
            if self.worker.stopping:
                self._send_quit(proc)
                kill_tree(proc)
                return "stopping"
            with self._queue_cond:
                if not self.queue:
                    self._queue_cond.wait(0.5)
                    continue
                meta = self.queue.pop(0)
            self._handle_request(proc, meta)
        reader.join(timeout=5)
        return "exited with code %s" % proc.returncode

    def announce_ready(self):
        """Tell the coordinator this service is up (again after it restarted)."""
        if self.ready_meta is None:
            return
        try:
            self.worker.http.call("POST", "/api/w/service/%s/ready" % self.id,
                                  {"name": self.name, "meta": self.ready_meta},
                                  timeout=20.0)
        except Removed:
            raise
        except Exception as exc:
            dbg("service ready post failed: %s" % exc)

    def _send_quit(self, proc):
        try:
            proc.stdin.write(b"QUIT\n")
            proc.stdin.flush()
        except Exception:
            pass

    def _kill_child(self):
        with self._lock:
            proc = self.proc
        if proc is not None:
            kill_tree(proc)

    # -- child output ----------------------------------------------------
    def _read_child(self, proc):
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith(SENT_READY):
                    try:
                        ready = json.loads(line[len(SENT_READY):]) or {}
                        self._announce = ready.get("announce") or {}
                    except ValueError:
                        self._announce = {}
                    self._ready.set()
                    continue
                if line.startswith(SENT_PARTIAL):
                    self._post_partial(line[len(SENT_PARTIAL):])
                    continue
                if line.startswith(SENT_DONE):
                    try:
                        payload = json.loads(line[len(SENT_DONE):])
                    except ValueError:
                        payload = {"ok": False, "error": "bad done json"}
                    if payload.get("fatal"):
                        self._fatal = payload.get("error")
                        self.log_line("[service] fatal: %s" % payload.get("error"))
                        if payload.get("traceback"):
                            for tb_line in str(payload["traceback"]).splitlines()[-6:]:
                                self.log_line("[service] " + tb_line)
                        self._ready.set()  # unblock the loader wait
                        continue
                    self._done_payload = payload
                    self._done_event.set()
                    continue
                if line.startswith(SENT_PROGRESS):
                    continue
                if line:
                    self.log_line(line)
        except Exception as exc:
            dbg("service reader stopped: %s" % exc)

    def _post_partial(self, text):
        if not self.current:
            return
        try:
            data = json.loads(text).get("data")
        except ValueError:
            return
        try:
            self.worker.http.call("POST", "/api/w/requests/%s/partial" % self.current.get("id"),
                                  {"data": data}, timeout=20.0)
        except Removed:
            raise
        except Exception as exc:
            dbg("partial post failed: %s" % exc)

    # -- one request -----------------------------------------------------
    def _handle_request(self, proc, meta):
        rid = str(meta.get("id"))
        self.current = meta
        self._done_event.clear()
        self._done_payload = None
        started = time.time()
        feeder_error = [None]

        def feed():
            try:
                self._feed_input(proc, rid, meta)
            except Exception as exc:
                feeder_error[0] = str(exc)
                try:
                    proc.stdin.write(b"END\n")
                    proc.stdin.flush()
                except Exception:
                    pass

        try:
            proc.stdin.write(("REQ " + json.dumps(meta) + "\n").encode("utf-8"))
            proc.stdin.flush()
        except Exception as exc:
            self._post_done(rid, {"ok": False, "error": "could not hand request to model: %s" % exc})
            self.current = None
            return
        feeder = threading.Thread(target=feed, name="svc-feed-" + rid)
        feeder.daemon = True
        feeder.start()
        deadline = started + self.request_timeout if self.request_timeout > 0 else None
        while not self._done_event.is_set():
            if proc.poll() is not None:
                break
            if self.cancelled or self.worker.stopping:
                break
            if deadline is not None and time.time() > deadline:
                self.log_line("[service] request %s timed out after %.0fs; restarting the model"
                              % (rid, self.request_timeout))
                kill_tree(proc)
                break
            self._done_event.wait(0.2)
        feeder.join(timeout=5)
        payload = self._done_payload
        if payload is None:
            error = feeder_error[0] or ("request timed out" if deadline and time.time() > deadline
                                        else "model process died")
            payload = {"ok": False, "error": error}
        payload.setdefault("ms", int((time.time() - started) * 1000))
        if payload.get("ok"):
            uploaded = []
            for item in payload.get("outputs") or []:
                path = item.get("path")
                if path and os.path.isfile(path):
                    meta_up = self._upload_output(path, item.get("name"))
                    if meta_up is not None:
                        uploaded.append(meta_up)
            payload["outputs"] = uploaded
            self.requests_done += 1
        self._post_done(rid, payload)
        shutil.rmtree(os.path.join(self.workdir, rid), ignore_errors=True)
        self.current = None

    def _feed_input(self, proc, rid, meta):
        """GET the request body from the coordinator and frame it to the child."""
        status, resp = self.worker.http.open_stream(
            "GET", "/api/w/requests/%s/input" % rid, timeout=self.request_timeout or 300.0)
        try:
            if status < 200 or status >= 300:
                raise ApiError(status, resp.read().decode("utf-8", "replace"))
            # read1() hands over whatever has arrived instead of waiting to
            # fill the buffer, so the model gets audio while it is still uploading
            read = getattr(resp, "read1", None) or resp.read
            while True:
                chunk = read(SERVICE_CHUNK)
                if not chunk:
                    break
                proc.stdin.write(("DAT %d\n" % len(chunk)).encode("ascii"))
                proc.stdin.write(chunk)
                proc.stdin.flush()
            proc.stdin.write(b"END\n")
            proc.stdin.flush()
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def _post_done(self, rid, payload):
        body = {"ok": bool(payload.get("ok")), "result": payload.get("result"),
                "error": payload.get("error"), "traceback": payload.get("traceback"),
                "outputs": payload.get("outputs") or [], "ms": payload.get("ms")}
        for attempt in (0, 1, 2):
            try:
                self.worker.http.call("POST", "/api/w/requests/%s/done" % rid, body, timeout=60.0)
                return
            except Removed:
                raise
            except ApiError as exc:
                dbg("request done rejected: %s" % exc)
                return
            except Exception as exc:
                if attempt == 2:
                    warn("request done post gave up for %s: %s" % (rid, exc))
                time.sleep(1.0 + attempt)


# --------------------------------------------------------------------------
# outbox: results that could not be delivered (SPEC 18)
# --------------------------------------------------------------------------

OUTBOX_MAX_AGE = 7 * 24 * 3600.0


class Outbox(object):
    """Holds finished task results while the coordinator is unreachable.

    Layout: ``<dir>/<task_id>/entry.json`` plus the output files that still
    have to be uploaded.  Replay uploads them, posts ``done`` and deletes the
    entry.  Only transport errors keep an entry; any HTTP answer (2xx, 409,
    4xx/5xx) means the coordinator has spoken and the entry is dropped.
    """

    def __init__(self, worker, directory):
        self.worker = worker
        self.dir = mkdirs(directory)
        self._lock = threading.Lock()
        self._replaying = False

    # -- storage ---------------------------------------------------------
    def count(self):
        try:
            return len([n for n in os.listdir(self.dir)
                        if os.path.isfile(os.path.join(self.dir, n, "entry.json"))])
        except OSError:
            return 0

    def add(self, task_id, result, pending_outputs):
        """Persist ``result`` and move ``pending_outputs`` (path, name) here."""
        entry_dir = mkdirs(os.path.join(self.dir, task_id))
        kept = []
        for index, item in enumerate(pending_outputs or []):
            src = item.get("path")
            if not src or not os.path.isfile(src):
                continue
            dst = os.path.join(entry_dir, "out%03d" % index)
            try:
                shutil.move(src, dst)
            except Exception:
                try:
                    shutil.copy2(src, dst)
                except Exception as exc:
                    warn("outbox: could not keep output %s: %s" % (src, exc))
                    continue
            kept.append({"file": os.path.basename(dst),
                         "name": item.get("name") or os.path.basename(src)})
        payload = {"task_id": task_id, "result": result, "pending": kept,
                   "created": time.time()}
        tmp = os.path.join(entry_dir, "entry.json.tmp")
        with open(tmp, "w") as handle:
            handle.write(json.dumps(payload))
        replace_file(tmp, os.path.join(entry_dir, "entry.json"))
        log("outbox: kept result of task %s (%d pending file%s)"
            % (task_id, len(kept), "" if len(kept) == 1 else "s"))

    def entries(self):
        out = []
        try:
            names = sorted(os.listdir(self.dir))
        except OSError:
            return out
        for name in names:
            path = os.path.join(self.dir, name, "entry.json")
            text = read_text(path)
            if not text:
                continue
            try:
                obj = json.loads(text)
            except ValueError:
                self.remove(name)
                continue
            if isinstance(obj, dict) and obj.get("task_id"):
                out.append(obj)
        return out

    def remove(self, task_id):
        shutil.rmtree(os.path.join(self.dir, str(task_id)), ignore_errors=True)

    # -- replay ----------------------------------------------------------
    def replay_async(self):
        """Replay in a background thread; no-op if one is already running."""
        with self._lock:
            if self._replaying:
                return
            self._replaying = True
        thread = threading.Thread(target=self._replay_guarded, name="outbox")
        thread.daemon = True
        thread.start()

    def _replay_guarded(self):
        try:
            self.replay()
        except Exception as exc:
            dbg("outbox replay failed: %s" % exc)
        finally:
            with self._lock:
                self._replaying = False

    def replay(self):
        """Deliver every entry; stops at the first transport error."""
        for entry in self.entries():
            task_id = str(entry["task_id"])
            if time.time() - float(entry.get("created") or 0) > OUTBOX_MAX_AGE:
                warn("outbox: dropping stale result of task %s" % task_id)
                self.remove(task_id)
                continue
            result = entry.get("result") or {}
            outputs = list(result.get("outputs") or [])
            pending = list(entry.get("pending") or [])
            delivered = 0
            stalled = False
            for item in pending:
                path = os.path.join(self.dir, task_id, item.get("file") or "")
                if os.path.isfile(path):
                    try:
                        outputs.append(self.worker.upload_file(path, item.get("name")))
                    except ApiError as exc:
                        warn("outbox: upload of %s rejected (%s), skipping it"
                             % (item.get("name"), exc))
                    except Exception as exc:
                        dbg("outbox: coordinator still unreachable (%s)" % exc)
                        stalled = True
                        break
                delivered += 1
            result["outputs"] = outputs
            entry["result"] = result
            if stalled:
                # Save progress so files already uploaded are not sent twice.
                entry["pending"] = pending[delivered:]
                self._rewrite(task_id, entry)
                return
            try:
                self.worker.http.call("POST", "/api/w/task/%s/done" % task_id, result,
                                      timeout=60.0)
                log("outbox: delivered result of task %s" % task_id)
            except Removed:
                raise
            except ApiError as exc:
                log("outbox: result of task %s no longer wanted (HTTP %s)"
                    % (task_id, exc.status))
            except Exception as exc:
                dbg("outbox: coordinator still unreachable (%s)" % exc)
                entry["pending"] = []
                self._rewrite(task_id, entry)
                return
            self.remove(task_id)

    def _rewrite(self, task_id, entry):
        path = os.path.join(self.dir, task_id, "entry.json")
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as handle:
                handle.write(json.dumps(entry))
            replace_file(tmp, path)
        except Exception as exc:
            dbg("outbox: could not rewrite %s: %s" % (task_id, exc))


# --------------------------------------------------------------------------
# worker
# --------------------------------------------------------------------------

class Worker(object):
    def __init__(self, url, pin=None, name=None, slots=None, state_dir=None, join_code=None):
        self.state_dir = mkdirs(os.path.abspath(state_dir or DEFAULT_STATE_DIR))
        self.device_file = os.path.join(self.state_dir, "device.json")
        self.saved = self._load_device()
        # the cluster's own CA, and this device's certificate for sending files (SPEC 3, 8.3)
        self.tls_dir = mkdirs(os.path.join(self.state_dir, "tls"))
        self.ca_file = os.path.join(self.tls_dir, "ca.pem")
        self.cert_file = os.path.join(self.tls_dir, "device.pem")
        self.key_file = os.path.join(self.tls_dir, "device.key")
        self.http = HttpClient(url, ca_file=self.ca_file)
        self.pin = pin
        self.join_code = join_code
        self.device_id = self.saved.get("device_id")
        self.token = self.saved.get("token")
        self.http.token = self.token
        self.name = name or self.saved.get("name") or device_model()
        # every core but one; the coordinator lowers it further to the CPU limit
        # set for this device (SPEC 9.3).  A count given with --slots or from
        # the dashboard sticks.
        default_slots = max(1, (os.cpu_count() or 1) - 1)
        if slots:
            self.slots, self.slots_set = int(slots), True
        elif self.saved.get("slots_set") and self.saved.get("slots"):
            self.slots, self.slots_set = int(self.saved["slots"]), True
        else:
            self.slots, self.slots_set = default_slots, False
        self.caps = detect_caps(self.state_dir)
        self.ffmpeg_path = find_ffmpeg()
        self.info = collect_info()
        cards, smi = probe_gpus()
        if cards:
            self.info["gpus"] = cards
            self.info["llama_gpu"] = "win-vulkan-x64"
        self.gpu = GpuMonitor(cards, smi)
        self.platform = platform_tag()
        self.poll_timeout = 25.0
        self.log_batch_ms = 500
        self.cache_mb = 500
        self.paused = False
        self.stopping = False
        self.bench_ran = False
        self.work_dir = os.path.join(self.state_dir, "work")
        shutil.rmtree(self.work_dir, ignore_errors=True)  # orphans from a crash
        mkdirs(self.work_dir)
        self.store_dir = mkdirs(os.path.join(self.state_dir, "store"))
        self.outbox = Outbox(self, os.path.join(self.state_dir, "outbox"))
        self.cache = FileCache(os.path.join(self.state_dir, "cache"), self.cache_mb)
        self.peers = PeerServer(self)
        self._fetch_guard = threading.Lock()
        self._fetch_locks = {}
        self._wasm_lock = threading.Lock()
        self._wasm_failed = None
        self.battery = BatteryMonitor("termux_api" in self.caps)
        self.log_pump = LogPump(self)
        self.tasks = {}
        self.terminals = {}
        #: request id -> until: this device is answering a service call, so
        #: normal tasks pause (SPEC 9.7)
        self.holds = {}
        self._hold_lock = threading.Lock()
        self._hold_watch = None
        self._task_lock = threading.Lock()
        self._slot_cond = threading.Condition()
        self._active = 0
        self._shutdown_lock = threading.Lock()
        self._shutdown_done = False
        self.exit_code = 0
        self._connect_failures = 0
        self.discovery_port = DISCOVERY_PORT
        self.discovery_hosts = None

    # -- discovery (SPEC 2.1) --------------------------------------------
    def _rediscover(self):
        """The saved URL stopped answering: ask the LAN where the coordinator
        is now (its IP may have changed).  Returns True if we switched."""
        self._connect_failures = 0
        url = discover(port=self.discovery_port, hosts=self.discovery_hosts)
        if not url or url.rstrip("/") == self.http.base_url.rstrip("/"):
            return False
        log("coordinator moved: %s -> %s" % (self.http.base_url, url))
        token = self.http.token
        try:
            moved = HttpClient(url, ca_file=self.ca_file)
        except ValueError as exc:
            warn("ignoring discovery reply %s: %s" % (url, exc))
            return False
        self.http.close()
        self.http = moved
        self.http.token = token
        try:
            self._save_device()
        except Exception:
            pass
        return True

    # -- persistence -----------------------------------------------------
    def _load_device(self):
        text = read_text(self.device_file)
        if not text:
            return {}
        try:
            obj = json.loads(text)
            return obj if isinstance(obj, dict) else {}
        except ValueError:
            return {}

    def _save_device(self):
        data = {"device_id": self.device_id, "token": self.token,
                "url": self.http.base_url, "name": self.name, "slots": self.slots,
                "slots_set": self.slots_set}
        tmp = self.device_file + ".tmp"
        with open(tmp, "w") as handle:
            handle.write(json.dumps(data, indent=2))
        replace_file(tmp, self.device_file)

    def _forget_device(self):
        self.device_id = None
        self.token = None
        self.http.token = None
        remove(self.device_file)

    # -- slots -----------------------------------------------------------
    def acquire_slot(self, cancelled):
        with self._slot_cond:
            while (self._active >= self.slots or self.held()) and not self.stopping and not cancelled():
                self._slot_cond.wait(0.5)
            self._active += 1

    # -- requests first (SPEC 9.7) ---------------------------------------
    def held(self):
        with self._hold_lock:
            stamp = time.time()
            for rid in [r for r, until in self.holds.items() if until < stamp]:
                self.holds.pop(rid, None)
            return bool(self.holds)

    def _batch_runs(self):
        with self._task_lock:
            return [r for r in self.tasks.values() if not isinstance(r, ServiceRun)]

    def cmd_hold(self, cmd):
        rid = str(cmd.get("request_id") or "")
        try:
            seconds = max(1.0, min(600.0, float(cmd.get("seconds") or 120)))
        except (TypeError, ValueError):
            seconds = 120.0
        with self._hold_lock:
            first = not self.holds
            self.holds[rid] = time.time() + seconds
            if self._hold_watch is None or not self._hold_watch.is_alive():
                self._hold_watch = threading.Thread(target=self._watch_holds, name="holds")
                self._hold_watch.daemon = True
                self._hold_watch.start()
        if first:
            for run in self._batch_runs():
                run.pause()

    def cmd_release(self, cmd):
        with self._hold_lock:
            self.holds.pop(str(cmd.get("request_id") or ""), None)
        if not self.held():
            self._resume_batch()

    def _resume_batch(self):
        for run in self._batch_runs():
            run.unpause()
        with self._slot_cond:
            self._slot_cond.notify_all()

    def _watch_holds(self):
        """Holds also end on their own (a lost release must not stall work)."""
        while not self.stopping:
            time.sleep(1.0)
            if not self.held():
                self._resume_batch()
                return

    def release_slot(self):
        with self._slot_cond:
            self._active = max(0, self._active - 1)
            self._slot_cond.notify_all()

    # -- registration ----------------------------------------------------
    def register(self):
        body = {
            "join_code": self.join_code,
            "pin": self.pin,
            "name": self.name,
            "platform": self.platform,
            "caps": self.caps,
            "info": self.info,
            "slots": self.slots,
        }
        if self.device_id and self.token:
            body["device_id"] = self.device_id
            body["token"] = self.token
        try:
            resp = self.http.call("POST", "/api/w/register", body, timeout=30.0)
        except Unauthorized:
            if not body.get("device_id"):
                raise
            log("saved credentials rejected, registering as a new device")
            self._forget_device()
            body.pop("device_id", None)
            body.pop("token", None)
            if not (self.join_code or self.pin):
                raise Unauthorized(401, "this device needs a new join code from the PC's Join page")
            resp = self.http.call("POST", "/api/w/register", body, timeout=30.0)
        self.join_code = None  # used up
        self._save_tls(resp.get("tls") or {})
        self.device_id = resp.get("device_id") or self.device_id
        token = resp.get("token")
        if token:
            self.token = token
        self.http.token = self.token
        config = resp.get("config") or {}
        self.poll_timeout = float(config.get("poll_timeout") or 25)
        self.log_batch_ms = int(config.get("log_batch_ms") or 500)
        self.cache_mb = float(config.get("cache_mb") or 500)
        self.cache.cap = int(self.cache_mb * 1024 * 1024)
        self._save_device()
        log("registered as %s (%s)" % (self.name, self.device_id))
        return resp

    def _save_tls(self, bundle):
        """Keep the cluster CA and this device's certificate (private to Termux)."""
        for name, path in (("ca_pem", self.ca_file), ("cert_pem", self.cert_file),
                           ("key_pem", self.key_file)):
            text = bundle.get(name)
            if not text:
                continue
            tmp = path + ".tmp"
            with open(tmp, "w") as handle:
                handle.write(text)
            replace_file(tmp, path)
            if name == "key_pem":
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass

    def has_device_cert(self):
        return os.path.isfile(self.cert_file) and os.path.isfile(self.key_file)

    def peer_context(self):
        """For fetching from another device: its certificate must come from the cluster CA."""
        return tls_context(self.ca_file)

    # -- status ----------------------------------------------------------
    def status_block(self):
        battery, charging, temp = self.battery.read()
        with self._task_lock:
            running = list(self.tasks.keys())
        free = 0 if self.paused else max(0, self.slots - len(running))
        return {
            "battery": battery,
            "charging": charging,
            "temp": temp,
            "free_slots": free,
            "running": running,
            "storage_free_mb": free_mb(self.state_dir),
            "mem_free_mb": read_mem_free_mb(),
            "gpu": self.gpu.read(),
        }

    # -- files -----------------------------------------------------------
    def fetch_inputs(self, file_ids, logger=None, peer=None):
        """Download inputs into the cache.  An entry is a file id, or a dict
        ``{"file_id", "offset", "length"}`` for a byte range of a big file
        (streaming inputs, SPEC 6.6) fetched with an HTTP Range request.
        Ids in ``peer`` are big files other devices have: they come from them
        and the coordinator at once (SPEC 8.3)."""
        paths = []
        peer = set(peer or [])
        for entry in file_ids or []:
            if isinstance(entry, dict):
                key = "%s_%d_%d" % (entry.get("file_id"), int(entry.get("offset") or 0),
                                    int(entry.get("length") or 0))
            else:
                key = str(entry)
            # tasks starting side by side often need the same file (a .wasm
            # module, a model): the first downloads it, the others wait for it
            with self._input_lock(key):
                path = self.cache.get(key)
                if path is None:
                    path = self._fetch_input(entry, key, logger, str(entry) in peer)
            paths.append(path)
        return paths

    def _input_lock(self, key):
        with self._fetch_guard:
            lock = self._fetch_locks.get(key)
            if lock is None:
                lock = self._fetch_locks[key] = threading.Lock()
            return lock

    def _fetch_input(self, entry, key, logger, from_peers):
        if from_peers and not isinstance(entry, dict):
            path = self.peer_fetch(str(entry), logger)
            if path is not None:
                return path
        if isinstance(entry, dict):
            fid = str(entry.get("file_id"))
            offset = int(entry.get("offset") or 0)
            length = int(entry.get("length") or 0)
            headers = {"Range": "bytes=%d-%d" % (offset, offset + max(0, length - 1))}
            label = "%s[%d:%d]" % (fid[:12], offset, offset + length)
        else:
            fid = str(entry)
            headers = None
            label = fid
        if logger:
            logger("[worker] downloading input %s" % label)
        status, data = self.http.request(
            "GET", "/api/w/files/%s" % urlparse.quote(fid), headers=headers,
            timeout=300.0)
        if status == 410:
            raise Removed(status, "removed")
        if status < 200 or status >= 300:
            raise ApiError(status, data.decode("utf-8", "replace"))
        if headers and status != 206:
            # coordinator ignored the range: slice locally
            data = data[offset:offset + length]
        return self.cache.put_bytes(key, data)

    def peer_fetch(self, file_id, logger=None):
        """Get a whole file from other phones and the coordinator in parallel.
        Returns the cache path, or None to fall back to a plain download."""
        try:
            plan = self.http.call("POST", "/api/w/peer/fetch", {"file_id": file_id}, timeout=30.0)
        except Removed:
            raise
        except Exception as exc:
            dbg("peer fetch plan failed: %s" % exc)
            return None
        if not plan.get("sources") or not plan.get("pieces"):
            return None
        if logger:
            logger("[worker] downloading input %s from %s and the coordinator" % (
                file_id[:12], ", ".join(str(s.get("name") or s.get("device_id")) for s in plan["sources"])))
        tmp = self.cache.path(file_id) + ".part-peer"
        try:
            ok = PieceDownload(self, file_id, plan, tmp, logger).run()
        except Exception as exc:
            warn("peer download of %s failed: %s" % (file_id[:12], exc))
            ok = False
        if not ok:
            remove(tmp)
            return None
        return self.cache.put_file(file_id, tmp)

    # -- WebAssembly runtime (SPEC 6.9) ------------------------------------
    def wasm_runtime(self, logger=None):
        """wasmtime's path; on Termux it is installed with pkg the first time."""
        with self._wasm_lock:
            found = find_wasmtime()
            if found:
                return found
            if self._wasm_failed:
                raise RuntimeError(self._wasm_failed)
            if not can_install_wasmtime():
                raise RuntimeError("no WebAssembly runtime here (run scripts\\get-wasmtime.ps1 on the PC)")
            say = logger or log
            say("[worker] installing the WebAssembly runtime: pkg install wasmtime (once)")
            for cmd in (["pkg", "install", "-y", "wasmtime"],
                        ["apt-get", "update"], ["apt-get", "install", "-y", "wasmtime"]):
                code = self._run_logged(cmd, say)
                found = find_wasmtime()
                if found:
                    say("[worker] wasmtime ready: %s" % found)
                    return found
                if cmd[0] == "pkg" and code == 0:
                    break
            self._wasm_failed = "wasmtime could not be installed on this device"
            # stop offering WebAssembly so these tasks go to other devices
            self.caps = [c for c in self.caps if c != "wasm"]
            threading.Thread(target=self._reregister, name="reregister", daemon=True).start()
            raise RuntimeError(self._wasm_failed)

    def _run_logged(self, cmd, say):
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL)
        except OSError as exc:
            say("[worker] %s: %s" % (cmd[0], exc))
            return -1
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                say("[%s] %s" % (cmd[0], line))
        try:
            return proc.wait(timeout=1200)
        except Exception:
            kill_tree(proc)
            return -1

    def _reregister(self):
        try:
            self.register()
        except Exception as exc:
            dbg("re-register failed: %s" % exc)

    def upload_file(self, path, name=None):
        name = name or os.path.basename(path)
        size = os.path.getsize(path)
        fh = open(path, "rb")
        try:
            status, data = self.http.request(
                "PUT", "/api/w/files?name=%s" % urlparse.quote(name),
                body=fh, headers={"Content-Length": str(size)},
                content_type="application/octet-stream", timeout=600.0)
        finally:
            fh.close()
        text = data.decode("utf-8", "replace")
        if status < 200 or status >= 300:
            raise ApiError(status, text)
        try:
            obj = json.loads(text)
        except ValueError:
            obj = {}
        return {"file_id": obj.get("file_id"), "name": obj.get("name") or name,
                "size": obj.get("size") if obj.get("size") is not None else size}

    def post_done(self, task_id, result):
        """Report a result.  Returns False only when the coordinator could not
        be reached at all (the caller then parks the result in the outbox)."""
        for attempt in (0, 1, 2):
            try:
                self.http.call("POST", "/api/w/task/%s/done" % task_id, result,
                               timeout=60.0)
                return True
            except Removed:
                raise
            except ApiError as exc:
                if exc.status == 409:
                    dbg("done rejected (stale attempt) for %s" % task_id)
                    return True
                warn("done post failed for %s: %s" % (task_id, exc))
                return True
            except Exception as exc:
                if attempt == 2:
                    warn("done post gave up for %s: %s" % (task_id, exc))
                    return False
                time.sleep(1.0 + attempt * 2)
        return False

    def deliver_result(self, task_id, result, pending_outputs):
        """Post ``done`` now, or keep it in the outbox for later (SPEC 18).

        Failed results are never kept: the coordinator requeues the task by
        itself once the device is seen offline, and a late failure would be
        ignored anyway.
        """
        if pending_outputs and result.get("ok"):
            self.outbox.add(task_id, result, pending_outputs)
            return
        if self.post_done(task_id, result):
            return
        if result.get("ok"):
            self.outbox.add(task_id, result, [])

    # -- commands --------------------------------------------------------
    def handle(self, cmd):
        kind = (cmd or {}).get("cmd")
        if kind == "task":
            self.cmd_task(cmd.get("task") or {})
        elif kind == "cancel":
            self.cmd_cancel(cmd.get("task_id"))
        elif kind == "service_request":
            self.cmd_service_request(cmd)
        elif kind == "service_cancel":
            self.cmd_service_cancel(cmd)
        elif kind == "store_chunk":
            self.cmd_store_chunk(cmd)
        elif kind == "send_chunk":
            self.cmd_send_chunk(cmd)
        elif kind == "del_chunk":
            self.cmd_del_chunk(cmd)
        elif kind == "scrub_chunks":
            self.cmd_scrub_chunks(cmd)
        elif kind == "peer_serve":
            threading.Thread(target=self.peers.offer, args=(cmd,), name="peer-offer",
                             daemon=True).start()
        elif kind == "peer_close":
            self.peers.close(cmd.get("offer_id"))
        elif kind == "term_open":
            self.cmd_term_open(cmd)
        elif kind == "term_input":
            self.cmd_term_input(cmd)
        elif kind == "term_close":
            self.cmd_term_close(cmd)
        elif kind == "notify":
            threading.Thread(
                target=send_notification,
                args=(cmd.get("title"), cmd.get("body")),
                daemon=True).start()
        elif kind == "benchmark":
            self.start_benchmark()
        elif kind == "config":
            self.cmd_config(cmd)
        elif kind == "hold":
            self.cmd_hold(cmd)
        elif kind == "release":
            self.cmd_release(cmd)
        elif kind == "removed":
            raise Removed(410, "removed by coordinator")
        elif kind == "shutdown":
            log("shutdown requested by coordinator")
            self.stopping = True
        else:
            warn("unknown command: %r" % (kind,))

    def cmd_task(self, task):
        task_id = str(task.get("id") or "")
        if not task_id:
            return
        with self._task_lock:
            if task_id in self.tasks:
                dbg("task %s already running" % task_id)
                return
            payload = task.get("payload") or {}
            if payload.get("service"):
                run = ServiceRun(self, task)
            else:
                run = TaskRun(self, task)
            self.tasks[task_id] = run
        if isinstance(run, ServiceRun):
            log("service %s (task %s) starting" % (run.name, task_id))
        else:
            log("task %s (job %s idx %s) accepted"
                % (task_id, task.get("job_id"), task.get("idx")))
        run.start()

    def cmd_cancel(self, task_id):
        with self._task_lock:
            run = self.tasks.get(str(task_id))
        if run:
            log("cancelling task %s" % task_id)
            run.cancel()

    def cmd_service_request(self, cmd):
        task_id = str(cmd.get("task_id") or "")
        meta = cmd.get("request") or {}
        with self._task_lock:
            run = self.tasks.get(task_id)
        if not isinstance(run, ServiceRun):
            warn("service request %s for unknown service task %s" % (meta.get("id"), task_id))
            try:
                self.http.call("POST", "/api/w/requests/%s/done" % meta.get("id"),
                               {"ok": False, "error": "service not running on this device"},
                               timeout=20.0)
            except Exception:
                pass
            return
        dbg("service %s: request %s queued" % (run.name, meta.get("id")))
        run.enqueue(meta)

    def cmd_service_cancel(self, cmd):
        rid = cmd.get("request_id")
        with self._task_lock:
            runs = [r for r in self.tasks.values() if isinstance(r, ServiceRun)]
        for run in runs:
            run.cancel_request(rid)

    def task_finished(self, task_id):
        with self._task_lock:
            self.tasks.pop(str(task_id), None)

    def cmd_store_chunk(self, cmd):
        chunk_id = str(cmd.get("chunk_id") or "")
        if not chunk_id:
            return

        def work():
            ok = True
            error = None
            try:
                status, data = self.http.request(
                    "GET", "/api/w/chunk/%s" % urlparse.quote(chunk_id), timeout=300.0)
                if status < 200 or status >= 300:
                    raise ApiError(status, data.decode("utf-8", "replace"))
                digest = hashlib.sha256(data).hexdigest()
                if digest != chunk_id:
                    raise ValueError("sha256 mismatch (%s != %s)" % (digest, chunk_id))
                path = os.path.join(self.store_dir, chunk_id)
                tmp = path + ".part"
                with open(tmp, "wb") as handle:
                    handle.write(data)
                replace_file(tmp, path)
            except Exception as exc:
                ok = False
                error = str(exc)
                warn("store_chunk %s failed: %s" % (chunk_id[:12], error))
            try:
                body = {"chunk_id": chunk_id, "ok": ok}
                if error:
                    body["error"] = error
                self.http.call("POST", "/api/w/chunk_ack", body, timeout=30.0)
            except Exception as exc:
                dbg("chunk_ack failed: %s" % exc)

        threading.Thread(target=work, name="chunk-store", daemon=True).start()

    def cmd_send_chunk(self, cmd):
        chunk_id = str(cmd.get("chunk_id") or "")
        req_id = str(cmd.get("req_id") or "")

        def work():
            path = os.path.join(self.store_dir, chunk_id)
            if not os.path.isfile(path):
                warn("send_chunk: %s not stored here" % chunk_id[:12])
                return
            try:
                size = os.path.getsize(path)
                fh = open(path, "rb")
                try:
                    self.http.request(
                        "PUT", "/api/w/chunk/%s?req_id=%s"
                        % (urlparse.quote(chunk_id), urlparse.quote(req_id)),
                        body=fh, headers={"Content-Length": str(size)},
                        content_type="application/octet-stream", timeout=300.0)
                finally:
                    fh.close()
            except Exception as exc:
                warn("send_chunk %s failed: %s" % (chunk_id[:12], exc))

        threading.Thread(target=work, name="chunk-send", daemon=True).start()

    def cmd_del_chunk(self, cmd):
        chunk_id = str(cmd.get("chunk_id") or "")
        if chunk_id:
            remove(os.path.join(self.store_dir, chunk_id))

    def cmd_scrub_chunks(self, cmd):
        """Re-hash stored pieces: a piece's id is its SHA-256, so damage shows
        without sending anything.  Damaged pieces are deleted; the coordinator
        rebuilds them from the other phones (SPEC 10)."""
        ids = [str(c) for c in (cmd.get("chunk_ids") or []) if c]

        def work():
            checked, bad, missing = [], [], []
            for chunk_id in ids:
                if not re.match(r"^[0-9a-f]{64}$", chunk_id):
                    continue
                path = os.path.join(self.store_dir, chunk_id)
                if not os.path.isfile(path):
                    missing.append(chunk_id)
                    continue
                digest = hashlib.sha256()
                try:
                    with open(path, "rb") as handle:
                        for block in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(block)
                except OSError:
                    missing.append(chunk_id)
                    continue
                if digest.hexdigest() == chunk_id:
                    checked.append(chunk_id)
                else:
                    bad.append(chunk_id)
                    remove(path)
            if bad:
                warn("storage check: %d damaged piece(s) removed" % len(bad))
            try:
                self.http.call("POST", "/api/w/storage/scrub",
                               {"checked": checked, "bad": bad, "missing": missing}, timeout=30.0)
            except Exception as exc:
                dbg("scrub report failed: %s" % exc)

        threading.Thread(target=work, name="chunk-scrub", daemon=True).start()

    def cmd_term_open(self, cmd):
        sid = str(cmd.get("session_id") or "")
        if not sid or sid in self.terminals:
            return
        session = TerminalSession(self, sid, cmd.get("shell"))
        self.terminals[sid] = session
        try:
            session.start()
            log("terminal %s opened (%s)" % (sid, session.shell))
        except Exception as exc:
            warn("term_open failed: %s" % exc)
            self.terminals.pop(sid, None)
            try:
                self.http.call("POST", "/api/w/term/%s/closed" % sid,
                               {"exit_code": -1}, timeout=20.0)
            except Exception:
                pass

    def cmd_term_input(self, cmd):
        session = self.terminals.get(str(cmd.get("session_id") or ""))
        if session:
            session.write(cmd.get("data") or "")

    def cmd_term_close(self, cmd):
        session = self.terminals.get(str(cmd.get("session_id") or ""))
        if session:
            session.close()

    def terminal_gone(self, sid):
        self.terminals.pop(str(sid), None)

    def cmd_config(self, cmd):
        if cmd.get("slots") is not None:
            try:
                self.slots = max(1, int(cmd.get("slots")))
                self.slots_set = True
                with self._slot_cond:
                    self._slot_cond.notify_all()
                log("slots set to %d" % self.slots)
            except (TypeError, ValueError):
                pass
        if cmd.get("paused") is not None:
            self.paused = bool(cmd.get("paused"))
            log("paused=%s" % self.paused)
        try:
            self._save_device()
        except Exception:
            pass

    def start_benchmark(self):
        def work():
            log("running benchmark ...")
            try:
                result = run_benchmark()
                self.http.call("POST", "/api/w/bench", result, timeout=30.0)
                log("benchmark score %.2f (%.1f MB/s sha256)"
                    % (result["score"], result["hash_mb_s"]))
            except Exception as exc:
                warn("benchmark failed: %s" % exc)

        threading.Thread(target=work, name="bench", daemon=True).start()

    # -- main loop -------------------------------------------------------
    def banner(self):
        print("")
        print("  PhoneCluster worker %s" % VERSION)
        print("  device : %s" % self.name)
        print("  url    : %s" % self.http.base_url)
        print("  caps   : %s" % ", ".join(self.caps))
        print("  slots  : %d" % self.slots)
        print("  state  : %s" % self.state_dir)
        pending = self.outbox.count()
        if pending:
            print("  outbox : %d undelivered result%s (will be sent on connect)"
                  % (pending, "" if pending == 1 else "s"))
        print("")
        try:
            sys.stdout.flush()
        except Exception:
            pass

    def run(self):
        """Run the poll loop in a background thread so Ctrl+C stays responsive
        (a blocking socket read is not interruptible on Windows)."""
        self.banner()
        self.log_pump.start()
        self.exit_code = 0
        thread = threading.Thread(target=self._loop, name="poll")
        thread.daemon = True
        thread.start()
        while thread.is_alive():
            thread.join(0.25)
        return self.exit_code

    def _loop(self):
        backoff = 1.0
        registered = False
        while not self.stopping:
            try:
                if not registered:
                    resp = self.register()
                    registered = True
                    backoff = 1.0
                    if not resp.get("bench_done") and not self.bench_ran:
                        self.bench_ran = True
                        self.start_benchmark()
                    # a restarted coordinator forgot our running services
                    with self._task_lock:
                        services = [r for r in self.tasks.values() if isinstance(r, ServiceRun)]
                    for run in services:
                        run.announce_ready()
                resp = self.http.call("POST", "/api/w/poll",
                                      {"status": self.status_block()},
                                      timeout=self.poll_timeout + 15.0)
                backoff = 1.0
                self._connect_failures = 0
                if self.outbox.count():
                    self.outbox.replay_async()
                for cmd in (resp.get("commands") or []):
                    if self.stopping:
                        break
                    try:
                        self.handle(cmd)
                    except Removed:
                        raise
                    except Exception as exc:
                        warn("command %r failed: %s" % ((cmd or {}).get("cmd"), exc))
                        dbg(traceback.format_exc())
            except Removed:
                log("this device was removed from the cluster")
                self._forget_device()
                self.exit_code = 0
                self.shutdown(send_leave=False)
                return
            except Unauthorized as exc:
                if not (self.device_id and self.token):
                    self.join_code = None  # a code that failed once never starts working
                if not (self.join_code or self.pin) and not (self.device_id and self.token):
                    log("not allowed to join: %s" % exc)
                    log("make a new join code on the PC's Join page and run the command shown there")
                    self.exit_code = 2
                    self.shutdown(send_leave=False)
                    return
                warn("unauthorized, re-registering")
                registered = False
                time.sleep(backoff)
                backoff = min(30.0, backoff * 2)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                warn("poll failed (%s), retrying in %.0fs" % (exc, backoff))
                dbg(traceback.format_exc())
                if not isinstance(exc, ApiError):
                    self._connect_failures += 1
                    if self._connect_failures >= REDISCOVER_AFTER and self._rediscover():
                        registered = False
                        backoff = 1.0
                        continue
                time.sleep(backoff)
                backoff = min(30.0, backoff * 2)
                if isinstance(exc, ApiError) and exc.status >= 500:
                    continue
                registered = registered and not isinstance(exc, ApiError)
        self.shutdown(send_leave=True)

    def shutdown(self, send_leave=True):
        with self._shutdown_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
        if send_leave:
            try:
                self.http.call("POST", "/api/w/leave", {}, timeout=10.0)
            except Exception as exc:
                dbg("leave failed: %s" % exc)
        self.stopping = True
        with self._task_lock:
            runs = list(self.tasks.values())
        for run in runs:
            try:
                run.cancel()
            except Exception:
                pass
        for session in list(self.terminals.values()):
            try:
                session.close()
            except Exception:
                pass
        self.peers._stop()  # never leave a sending port open
        with self._slot_cond:
            self._slot_cond.notify_all()
        for run in runs:
            run.thread.join(timeout=5)
        self.log_pump.stop()
        try:
            self.http.close()
        except Exception:
            pass
        log("worker stopped")


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def pem_to_der(text):
    body = "".join(line.strip() for line in text.strip().splitlines() if not line.startswith("-----"))
    import base64
    return base64.b64decode(body)


def install_ca(state_dir, ca_path=None, fingerprint=None, url=None):
    """Put the cluster's CA into <state>/tls/ca.pem (SPEC 3).

    ``ca_path``: a file the join command wrote.  ``fingerprint``: fetch
    ``/ca.pem`` and keep it only if its SHA-256 is that value -- a stranger
    on the Wi-Fi cannot make a certificate with the right fingerprint."""
    target = os.path.join(mkdirs(os.path.join(state_dir, "tls")), "ca.pem")
    if ca_path:
        text = read_text(ca_path)
        if not text or "BEGIN CERTIFICATE" not in text:
            raise ValueError("%s is not a certificate" % ca_path)
        if os.path.abspath(ca_path) != os.path.abspath(target):
            with open(target, "w") as handle:
                handle.write(text)
        return target
    if fingerprint and url and str(url).startswith("https://"):
        import ssl

        parts = urlparse.urlsplit(url)
        conn = httplib.HTTPSConnection(parts.hostname, parts.port or 443, timeout=20,
                                       context=ssl._create_unverified_context())
        try:
            conn.request("GET", (parts.path or "").rstrip("/") + "/ca.pem")
            resp = conn.getresponse()
            text = resp.read().decode("ascii", "replace")
        finally:
            conn.close()
        got = hashlib.sha256(pem_to_der(text)).hexdigest()
        if got != str(fingerprint).strip().lower().replace(":", ""):
            raise ValueError("the coordinator's certificate does not match the fingerprint shown "
                             "on the PC (got %s): not joining" % got)
        with open(target, "w") as handle:
            handle.write(text)
    return target


def build_parser():
    parser = argparse.ArgumentParser(
        prog="worker.py", description="PhoneCluster worker (stdlib only)")
    parser.add_argument("--url", default=None,
                        help="coordinator base URL, e.g. http://192.168.1.5:8765 "
                             "(omit or use 'auto' to find it by LAN broadcast)")
    parser.add_argument("--discover-host", action="append", default=None,
                        help="send discovery probes here instead of broadcasting (repeatable)")
    parser.add_argument("--discovery-port", type=int, default=DISCOVERY_PORT,
                        help="UDP port the coordinator answers probes on")
    parser.add_argument("--join", default=None, metavar="CODE",
                        help="one-time join code from the PC's Join page (first run only)")
    parser.add_argument("--ca", default=None, metavar="FILE",
                        help="the cluster's CA certificate (the join command writes it)")
    parser.add_argument("--fp", default=None, metavar="SHA256",
                        help="fingerprint of the cluster's CA: fetch it from the coordinator and check it")
    parser.add_argument("--pin", default=None, help=argparse.SUPPRESS)  # the PC's own worker only
    parser.add_argument("--name", default=None, help="device name shown in the dashboard")
    parser.add_argument("--slots", type=int, default=None,
                        help="how many tasks to run at once")
    parser.add_argument("--state-dir", default=None,
                        help="where device.json / cache / store live (default ~/.phonecluster)")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version="phonecluster-worker " + VERSION)
    return parser


def main(argv=None):
    global _VERBOSE
    args = build_parser().parse_args(argv)
    _VERBOSE = bool(args.verbose)
    state_dir = args.state_dir or os.environ.get("PC_STATE_DIR") or DEFAULT_STATE_DIR
    state_dir = os.path.abspath(state_dir)
    mkdirs(state_dir)
    saved = {}
    text = read_text(os.path.join(state_dir, "device.json"))
    if text:
        try:
            saved = json.loads(text) or {}
        except ValueError:
            saved = {}
    url = args.url or os.environ.get("PC_URL") or saved.get("url")
    pin = args.pin or os.environ.get("PC_PIN")
    join_code = args.join or os.environ.get("PC_JOIN_CODE")
    if not url or str(url).lower() == "auto":
        if not (join_code or pin or saved.get("token")):
            sys.stderr.write(
                "error: this device has not joined yet (no device.json in %s)\n"
                "       on the PC open Join, then run the command shown there\n" % state_dir)
            return 2
        log("looking for a coordinator on the LAN ...")
        url = discover_forever(port=args.discovery_port, hosts=args.discover_host)
        if not url:
            return 2
        log("found coordinator at %s" % url)
    try:
        ca = install_ca(state_dir, args.ca, args.fp, url)
        if str(url).startswith("https://") and not os.path.isfile(ca):
            raise ValueError("no cluster certificate yet: run the join command from the PC's Join page")
        worker = Worker(url=url, pin=pin, name=args.name, slots=args.slots,
                        state_dir=state_dir, join_code=join_code)
    except (ValueError, ApiError) as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    worker.discovery_port = args.discovery_port
    worker.discovery_hosts = args.discover_host

    def on_signal(_signum, _frame):
        # SIGINT keeps its default handler so Ctrl+C raises KeyboardInterrupt
        # in the main thread immediately (see Worker.run).
        if not worker.stopping:
            log("stopping ...")
            worker.stopping = True

    sig = getattr(signal, "SIGTERM", None)
    if sig is not None:
        try:
            signal.signal(sig, on_signal)
        except (ValueError, OSError):
            pass
    try:
        return worker.run()
    except KeyboardInterrupt:
        log("interrupted")
        worker.shutdown(send_leave=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
