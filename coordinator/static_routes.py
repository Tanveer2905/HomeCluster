"""Static routes: dashboard, browser worker, worker.py, the installers, /join, /qr.svg.

SPEC §11: ``GET /`` -> ``dashboard/index.html``; ``/static/*`` -> ``dashboard/``;
``GET /w`` -> ``worker/browser/index.html``; ``/w/*`` -> ``worker/browser/``;
``GET /worker.py``; ``GET /install.sh?code=``; ``GET /install.ps1?code=``;
``GET /python-win.zip?code=``; ``GET /join``; ``GET /qr.svg?text=``; ``GET /ca.pem``.
``/``, ``/static``, ``/join`` and ``/qr.svg`` open only on the PC itself
(``coordinator/security.py``).
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import threading
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from .qr import qr_svg
from .security import normalize_code

log = logging.getLogger("coordinator.static")

router = APIRouter(tags=["static"])

INSTALL_SH = """#!/bin/sh
# PhoneCluster worker installer for Termux (no root), Linux and macOS.  Made for one
# join code, which works once and only for a few minutes.
set -e
PY=$(command -v python3 || command -v python || true)
if [ -z "$PY" ]; then
  case "$PREFIX" in
    *com.termux*) pkg install -y python; PY=python ;;
    *) echo "PhoneCluster needs Python 3.8 or newer: install python3, then run this again" >&2; exit 1 ;;
  esac
fi
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 8))' || {
  echo "PhoneCluster needs Python 3.8 or newer ($PY is older)" >&2; exit 1; }
mkdir -p ~/phonecluster && cd ~/phonecluster
# the cluster's own certificate authority: the worker trusts only this
cat > cluster-ca.pem <<'PHONECLUSTER_CA'
{CA}PHONECLUSTER_CA
curl -fsSk --pinnedpubkey "{SPKI}" "{URL}/worker.py" -o worker.py
exec "$PY" worker.py --url "{URL}" --ca cluster-ca.pem --join "{CODE}"
"""

# Run by the Join page's Windows one-liner: curl.exe (part of Windows 10 and 11) fetches it
# with the coordinator's key pinned and PowerShell runs it.  It only uses PowerShell's own
# built-in commands and .NET (no script modules), so it works under Windows' default
# "Restricted" script policy without touching it.  ASCII only: PowerShell reads curl's
# output in the console's code page.
INSTALL_PS1 = r"""# PhoneCluster worker installer for a Windows PC or laptop (no admin rights needed).  Made
# for one join code, which works once and only for a few minutes.  Everything goes into one
# folder, %USERPROFILE%\phonecluster: nothing is installed, no setting changes, and deleting
# the folder removes it all.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$url = '{URL}'
$code = '{CODE}'
$pin = '{SPKI}'
$pythonSha = '{PYZIP}'
$dir = Join-Path $env:USERPROFILE 'phonecluster'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
Set-Location $dir

# every download checks that it really comes from the cluster's PC (its key is pinned)
function Get-FromPC([string]$path, [string]$out) {
    & curl.exe -fsSk --pinnedpubkey $pin -o "$out.part" "$url$path"
    if ($LASTEXITCODE -ne 0) { throw "could not download $path from the PC (curl exit code $LASTEXITCODE)" }
    Move-Item -Force "$out.part" $out
}

# a Python 3.8+ this computer already has (python.org, the Microsoft Store), as a full path
function Find-Python {
    foreach ($name in 'python', 'python3', 'py') {
        try {
            if (-not (Get-Command $name -CommandType Application -ErrorAction SilentlyContinue)) { continue }
            $found = @(& $name -c "import sys; print(sys.executable if sys.version_info >= (3, 8) else '')" 2>$null)
            $exe = "$($found[-1])".Trim()
            if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path -LiteralPath $exe)) { return $exe }
        } catch { }
    }
    return $null
}

# the cluster's own certificate authority: the worker trusts only this
$ca = @'
{CA}'@
[IO.File]::WriteAllText((Join-Path $dir 'cluster-ca.pem'), $ca + "`n")

# Python: the copy in this folder; else the PC's own portable Python (just files, unpacked
# here); else a Python 3.8+ this computer already has
$own = Join-Path $dir 'python\python.exe'
$python = $null
if (Test-Path $own) {
    $python = $own
} elseif ($pythonSha -and [Environment]::Is64BitOperatingSystem) {
    Write-Host 'copying Python from the PC ...'
    $zip = Join-Path $dir 'python.zip'
    Get-FromPC "/python-win.zip?code=$code" $zip
    $sha = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($zip)
    try { $got = -join ($sha.ComputeHash($stream) | ForEach-Object { $_.ToString('x2') }) }
    finally { $stream.Dispose() }
    if ($got -ne $pythonSha) {
        Remove-Item -Force $zip
        throw 'the copy of Python from the PC arrived damaged: run the command again'
    }
    $unpack = Join-Path $dir 'python.unpack'
    foreach ($old in $unpack, (Join-Path $dir 'python')) {
        if (Test-Path $old) { Remove-Item -Recurse -Force $old }
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($zip, $unpack)
    Remove-Item -Force $zip
    Rename-Item $unpack 'python'
    $python = $own
} else {
    $python = Find-Python
}
if (-not $python) {
    throw ('this computer needs Python 3.8 or newer (python.org), or the cluster''s PC its ' +
           'portable Python (scripts\get-python.ps1 there): then make a new join code and run this again')
}

Write-Host 'downloading worker.py ...'
Get-FromPC '/worker.py' (Join-Path $dir 'worker.py')

# start-worker.cmd starts the worker again later: it remembers the cluster in state\
$run = if ($python -eq $own) { 'python\python.exe' } else { $python }
$launcher = @('@echo off',
    'rem PhoneCluster worker: keep this window open; Ctrl+C or closing it stops the worker.',
    'cd /d "%~dp0"',
    ('"{0}" -u -X utf8 worker.py --state-dir state %*' -f $run)) -join "`r`n"
[IO.File]::WriteAllText((Join-Path $dir 'start-worker.cmd'), $launcher + "`r`n")

Write-Host ''
Write-Host "joining the cluster at $url (keep this window open) ..."
Write-Host "Later: $dir\start-worker.cmd starts the worker again. To remove it, delete $dir."
& $python -u -X utf8 worker.py --url $url --ca cluster-ca.pem --join $code --state-dir state
"""

BROWSER_PLACEHOLDER = """<!doctype html><meta charset="utf-8">
<title>PhoneCluster browser worker</title>
<body style="font-family:system-ui;background:#0d1117;color:#c9d1d9;padding:2rem">
<h1>Browser worker not installed</h1>
<p>Drop <code>index.html</code>, <code>worker.js</code>, <code>runner.js</code> and
<code>style.css</code> into <code>worker/browser/</code> and reload this page.</p>
<p>The Termux worker is available at <a style="color:#58a6ff" href="/worker.py">/worker.py</a>.</p>
</body>
"""

JOIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Join PhoneCluster</title>
<style>
 body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#0d1117;color:#c9d1d9;
      margin:0;padding:1.5rem;line-height:1.5}}
 .wrap{{max-width:760px;margin:0 auto}}
 h1{{font-size:1.5rem}} code{{background:#161b22;padding:.15rem .35rem;border-radius:4px}}
 .pin{{font-size:2.4rem;letter-spacing:.3em;font-weight:700;color:#58a6ff}}
 .card{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:1rem;margin:1rem 0}}
 pre{{background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:.75rem;
      overflow-x:auto;white-space:pre-wrap;word-break:break-all}}
 .qr{{background:#fff;padding:12px;border-radius:10px;display:inline-block;max-width:260px}}
 .qr svg{{width:100%;height:auto;display:block}}
 .qr-wide{{max-width:340px}}
 a{{color:#58a6ff}}
</style></head><body><div class="wrap">
<h1>Add a device to this PhoneCluster</h1>
<div class="card"><div>One-time join code (works once, for {minutes} minutes)</div>
<div class="pin">{code}</div><p>Reload this page for a new code.</p></div>
<div class="card"><div class="qr">{qr}</div>
<p>Scan with the phone to open the browser worker. The browser warns once that the
connection is "not private": the cluster uses its own certificate. Continue to the page.</p></div>
<div class="card"><h2>Termux (Android, no root)</h2>
<div class="qr qr-wide">{install_qr}</div>
<p>Scan this with the phone's camera or QR scanner, tap <b>Copy</b>, then paste it into Termux.
(Scanning only copies the text; nothing runs until you paste it.)</p>
<pre>{install_cmd}</pre>
<p>The same command adds a Linux or Mac computer: paste it into a terminal there (it needs
Python 3.8 or newer).</p></div>
<div class="card"><h2>Windows PC or laptop</h2>
<p>Paste into PowerShell or Command Prompt on it. Everything goes into one folder,
<code>phonecluster</code> in the user's folder: no admin rights, nothing installed.</p>
<pre>{windows_cmd}</pre></div>
<div class="card"><h2>Browser worker</h2><pre>{browser_url}</pre></div>
<div class="card"><h2>By hand</h2><pre>{manual_cmd}</pre>
<p>Certificate fingerprint: <code>{fingerprint}</code></p>
<p><a href="/">Open the dashboard</a> &middot; <a href="/worker.py">worker.py</a></p></div>
</div></body></html>
"""


def _state(request: Request) -> Any:
    return request.app.state.cluster


def _root(request: Request) -> Path:
    return Path(request.app.state.cluster.root)


def _base_url(request: Request) -> str:
    """Prefer the host the client actually used, so QR/install links work.

    When the page is opened on the coordinator itself via ``localhost`` /
    ``127.0.0.1`` that host is useless on a phone, so fall back to the best
    LAN address instead.
    """
    host = request.headers.get("host")
    if host:
        name = host.rsplit(":", 1)[0].strip("[]").lower()
        loopback = name in ("localhost", "::1") or name.startswith("127.")
        if not loopback:
            return "%s://%s" % (request.url.scheme, host)
    return _state(request).primary_url()


@router.get("/", include_in_schema=False)
async def dashboard(request: Request) -> Response:
    index = _root(request) / "dashboard" / "index.html"
    if not index.is_file():  # pragma: no cover - dashboard is part of the repo
        raise HTTPException(status_code=404, detail="dashboard is not installed")
    return FileResponse(index, media_type="text/html")


@router.get("/w", include_in_schema=False)
async def browser_worker(request: Request) -> Response:
    index = _root(request) / "worker" / "browser" / "index.html"
    if not index.is_file():
        log.warning("worker/browser/index.html is missing; serving a placeholder")
        return HTMLResponse(BROWSER_PLACEHOLDER)
    return FileResponse(index, media_type="text/html")


@router.get("/worker.py", include_in_schema=False)
async def worker_py(request: Request) -> Response:
    path = _root(request) / "worker" / "worker.py"
    if not path.is_file():  # pragma: no cover
        raise HTTPException(status_code=404, detail="worker.py is not installed")
    return FileResponse(
        path,
        media_type="text/x-python",
        headers={"Content-Disposition": 'attachment; filename="worker.py"'},
    )


def _refuse(request: Request, code: str) -> Optional[Response]:
    """The installers exist only for a valid, unused join code (wrong ones count
    towards the lockout like any other guess)."""
    security = _state(request).security
    host = getattr(request.client, "host", "")
    local = security.is_local(host)
    wait = security.guard.blocked(host, local)
    if wait:
        return JSONResponse({"error": "too many wrong attempts; try again later"}, status_code=429)
    if not security.codes.valid(code):
        security.guard.fail(host, local)
        return JSONResponse({"error": "join code unknown, used or expired: make a new one on the "
                                      "PC's Join page"}, status_code=403)
    return None


def _installer(template: str, request: Request, code: str, **extra: str) -> str:
    security = _state(request).security
    security.ensure_tls()
    script = (template.replace("{URL}", _base_url(request)).replace("{CODE}", normalize_display(code))
              .replace("{SPKI}", security.tls.server_pin()))
    for key, value in extra.items():
        script = script.replace("{%s}" % key, value)
    return script.replace("{CA}", security.tls.ca_text())


@router.get("/install.sh", include_in_schema=False)
async def install_sh(request: Request, code: str = "") -> Response:
    """The Termux / Linux / Mac installer for one join code (SPEC 3)."""
    refused = _refuse(request, code)
    if refused is not None:
        return refused
    return PlainTextResponse(_installer(INSTALL_SH, request, code), media_type="text/x-shellscript")


@router.get("/install.ps1", include_in_schema=False)
async def install_ps1(request: Request, code: str = "") -> Response:
    """The Windows installer for one join code (SPEC 3)."""
    refused = _refuse(request, code)
    if refused is not None:
        return refused
    state = _state(request)
    packed = await asyncio.to_thread(portable_python, _root(request), Path(state.data))
    script = _installer(INSTALL_PS1, request, code, PYZIP=packed[1] if packed else "")
    return PlainTextResponse(script, media_type="text/plain")


@router.get("/python-win.zip", include_in_schema=False)
async def python_win_zip(request: Request, code: str = "") -> Response:
    """The PC's portable Python for a Windows computer joining with ``code``."""
    refused = _refuse(request, code)
    if refused is not None:
        return refused
    state = _state(request)
    packed = await asyncio.to_thread(portable_python, _root(request), Path(state.data))
    if packed is None:
        raise HTTPException(status_code=404, detail="this PC has no portable Python: run "
                                                    "scripts\\get-python.ps1 on it")
    return FileResponse(packed[0], media_type="application/zip")


@router.get("/ca.pem", include_in_schema=False)
async def ca_pem(request: Request) -> Response:
    """The cluster's CA certificate: public, a joining device checks it
    against the fingerprint shown on the PC."""
    security = _state(request).security
    security.ensure_tls()
    return Response(security.tls.ca_text(), media_type="application/x-pem-file")


@router.get("/qr.svg", include_in_schema=False)
async def qr(request: Request, text: str = "") -> Response:
    """A QR of ``text`` (the dashboard uses it; this PC only)."""
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    return Response(content=qr_svg(text), media_type="image/svg+xml")


@router.get("/join", include_in_schema=False)
async def join(request: Request) -> Response:
    """A fresh one-time join code with its QR and commands (this PC only)."""
    state = _state(request)
    state.security.ensure_tls()
    payload = state.security.join_info(_base_url(request), state.security.codes.create("join page"))
    accept = (request.headers.get("accept") or "").lower()
    if "application/json" in accept:
        return JSONResponse(payload)
    page = JOIN_PAGE.format(
        code=html.escape(str(payload["code"])),
        minutes=int(payload["minutes"]),
        qr=payload["qr_svg"],
        install_qr=payload["install_qr_svg"],
        install_cmd=html.escape(payload["install_cmd"]),
        windows_cmd=html.escape(payload["windows_cmd"]),
        browser_url=html.escape(payload["browser_url"]),
        manual_cmd=html.escape(payload["manual_cmd"]),
        fingerprint=html.escape(payload["ca_fingerprint"]),
    )
    return HTMLResponse(page)


def normalize_display(code: str) -> str:
    raw = normalize_code(code)
    return "%s-%s" % (raw[:4], raw[4:]) if len(raw) == 8 else raw


_packing = threading.Lock()
_packed_sha: Dict[str, str] = {}


def portable_python(root: Path, data: Path) -> Optional[Tuple[Path, str]]:
    """``tools/python`` packed for a Windows computer's worker: ``(zip, sha256)``, or None
    when this PC has no portable Python.

    Only the interpreter and the standard library go in (the files at the top of
    ``tools/python``; the PC's libraries and pip in ``Lib`` and ``Scripts`` stay here), with
    a ._pth of its own that keeps it isolated from anything on that computer.  Packed once
    into ``data/tmp`` and again when ``tools/python`` changes.
    """
    source = Path(root) / "tools" / "python"
    if not (source / "python.exe").is_file():
        return None
    files = sorted((p for p in source.iterdir() if p.is_file() and not p.name.endswith("._pth")),
                   key=lambda p: p.name)
    stdlib = next((p.name for p in files if p.name.startswith("python3") and p.suffix == ".zip"), None)
    pth = next(iter(sorted(source.glob("python3*._pth"))), None)
    if stdlib is None or pth is None:
        return None
    stamp = hashlib.sha256(repr([(p.name, p.stat().st_size, p.stat().st_mtime_ns)
                                 for p in files]).encode("utf-8")).hexdigest()[:12]
    tmp = Path(data) / "tmp"
    target = tmp / ("python-win-%s.zip" % stamp)
    with _packing:
        if not target.is_file():
            tmp.mkdir(parents=True, exist_ok=True)
            for old in tmp.glob("python-win-*.zip"):
                try:
                    old.unlink()
                except OSError:  # pragma: no cover - still being sent to someone
                    pass
            part = target.with_suffix(".part")
            with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as pack:
                for path in files:
                    pack.write(path, path.name)
                pack.writestr(pth.name, "%s\n.\n" % stdlib)  # the standard library, nothing else
            part.replace(target)
            _packed_sha.pop(str(target), None)
            log.info("packed tools/python for Windows workers: %s (%.1f MB)",
                     target.name, target.stat().st_size / 1e6)
        key = str(target)
        if key not in _packed_sha:
            digest = hashlib.sha256()
            with open(target, "rb") as handle:
                for block in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(block)
            _packed_sha[key] = digest.hexdigest()
        return target, _packed_sha[key]


def pyodide_dir(root: Path) -> Optional[Path]:
    """``tools/pyodide`` when a usable Pyodide build is installed (SPEC §15.1)."""
    candidate = Path(root) / "tools" / "pyodide"
    if (candidate / "pyodide.js").is_file() and (candidate / "pyodide.asm.wasm").is_file():
        return candidate
    return None


def pyodide_version(root: Path) -> Optional[str]:
    directory = pyodide_dir(root)
    if directory is None:
        return None
    try:
        import json as _json

        lock = _json.loads((directory / "pyodide-lock.json").read_text(encoding="utf-8"))
        return str((lock.get("info") or {}).get("version") or "unknown")
    except Exception:
        return "unknown"


def install_static(app: FastAPI, root: Path) -> None:
    """Mount ``/static``, ``/w`` and (if present) ``/w/pyodide``."""
    import mimetypes

    # Windows has no registry entry for .wasm; browsers need the right type
    # for WebAssembly.instantiateStreaming.
    mimetypes.add_type("application/wasm", ".wasm")
    mimetypes.add_type("application/javascript", ".mjs")
    dashboard_dir = Path(root) / "dashboard"
    if dashboard_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(dashboard_dir)), name="static")
    else:  # pragma: no cover
        log.warning("no dashboard directory at %s", dashboard_dir)
    pyodide = pyodide_dir(root)
    if pyodide is not None:
        # mounted before /w so it takes precedence for /w/pyodide/*
        app.mount("/w/pyodide", StaticFiles(directory=str(pyodide)), name="pyodide")
        log.info("browser Python (Pyodide %s) served from %s", pyodide_version(root), pyodide)
    browser_dir = Path(root) / "worker" / "browser"
    if browser_dir.is_dir():
        app.mount("/w", StaticFiles(directory=str(browser_dir), html=True), name="browser")
