"""Keeping outsiders out (SPEC §3).

* **Two doors.**  The LAN port (``port``, 8765) speaks only HTTPS, with
  certificates the cluster makes for itself in ``data/tls`` (its own CA; no
  system store is touched).  The local port (``local_port``, 8764) is plain
  HTTP bound to 127.0.0.1, for the dashboard, CLI, SDK and the PC's own
  worker -- traffic that never leaves the PC.
* **Only this PC manages the cluster.**  The dashboard, the PIN-protected API,
  the dashboard WebSocket and the Join page answer only to a client on the PC
  itself (loopback, or one of the PC's own addresses).  Phones reach only the
  worker protocol, the browser-worker page and service calls made with their
  own device token.
* **Phones join with a one-time code** shown on the PC: valid once, for
  ``security.join_minutes`` (10).  The PIN never leaves the PC.
* **Wrong guesses lock out.**  Wrong PINs, join codes and device tokens are
  counted per address (5 within 10 minutes: locked for a minute, doubling up
  to an hour) and across the LAN (30 a minute: every outside address locked
  for 5 minutes).
* **Unknown host names are refused**, so a web page cannot reach the cluster
  through DNS rebinding.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import ipaddress
import json
import logging
import secrets
import socket
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional, Set, Tuple

log = logging.getLogger("coordinator.security")

#: join codes: no 0/O/1/I so they can be typed from the screen
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8

#: lockouts
PER_IP_LIMIT = 5
PER_IP_WINDOW = 600.0
LOCK_FIRST = 60.0
LOCK_MAX = 3600.0
GLOBAL_LIMIT = 30
GLOBAL_WINDOW = 60.0
GLOBAL_LOCK = 300.0

SESSION_SECONDS = 12 * 3600

#: what a client that is not the PC itself may reach
REMOTE_EXACT = frozenset({"/w", "/worker.py", "/install.sh", "/install.ps1", "/python-win.zip",
                          "/ca.pem", "/api/info"})
REMOTE_PREFIXES = ("/w/", "/api/w/", "/api/services", "/api/requests")

REMOTE_PAGE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PhoneCluster</title>
<body style="font-family:system-ui;background:#0d1117;color:#c9d1d9;padding:2rem;line-height:1.5">
<h1>This dashboard only opens on the PC itself</h1>
<p>To add this device to the cluster, open <b>Join</b> on the PC's dashboard and scan the
code shown there, or type its one-time code on the <a style="color:#58a6ff" href="/w">browser
worker page</a>.</p></body>
"""


def _now() -> float:
    return time.time()


# --------------------------------------------------------------------------
# certificates
# --------------------------------------------------------------------------

class TLSFiles:
    """The cluster's own CA and the coordinator's certificate in ``data/tls``.

    Devices trust the CA (its fingerprint travels in the join command shown on
    the PC), so the PC's changing IP address never breaks anything: clients
    check the chain, not the host name.
    """

    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)
        self.ca_pem = self.dir / "ca.pem"
        self.ca_key = self.dir / "ca.key"
        self.server_pem = self.dir / "server.pem"
        self.server_key = self.dir / "server.key"

    # ------------------------------------------------------------- create
    def ensure(self, names: Iterable[str] = (), addresses: Iterable[str] = ()) -> None:
        if self.ca_pem.is_file() and self.ca_key.is_file() and self.server_pem.is_file() \
                and self.server_key.is_file():
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        if not (self.ca_pem.is_file() and self.ca_key.is_file()):
            key, cert = _make_ca()
            _write(self.ca_key, _key_pem(key))
            _write(self.ca_pem, _cert_pem(cert))
            log.info("made the cluster's certificate authority in %s", self.dir)
        key, cert = _make_leaf(self._ca(), "PhoneCluster coordinator", list(names), list(addresses),
                               server=True)
        _write(self.server_key, _key_pem(key))
        _write(self.server_pem, _cert_pem(cert))
        log.info("made the coordinator's HTTPS certificate")

    def _ca(self) -> Tuple[Any, Any]:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        key = serialization.load_pem_private_key(self.ca_key.read_bytes(), password=None)
        cert = x509.load_pem_x509_certificate(self.ca_pem.read_bytes())
        return key, cert

    def issue_device(self, device_id: str) -> Dict[str, str]:
        """A certificate for one device: it serves phone-to-phone transfers
        (SPEC 8.3) with it, and other devices check it against the CA."""
        key, cert = _make_leaf(self._ca(), "PhoneCluster device %s" % device_id, [], [], server=True)
        return {"cert_pem": _cert_pem(cert).decode("ascii"), "key_pem": _key_pem(key).decode("ascii")}

    # -------------------------------------------------------------- facts
    def ca_text(self) -> str:
        return self.ca_pem.read_text(encoding="ascii")

    def ca_fingerprint(self) -> str:
        """SHA-256 of the CA certificate (DER), hex: what a phone checks."""
        return hashlib.sha256(_pem_to_der(self.ca_text())).hexdigest()

    def server_pin(self) -> str:
        """``sha256//<base64>`` of the coordinator's public key, for curl's
        ``--pinnedpubkey`` in the Termux install command."""
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        cert = x509.load_pem_x509_certificate(self.server_pem.read_bytes())
        spki = cert.public_key().public_bytes(serialization.Encoding.DER,
                                              serialization.PublicFormat.SubjectPublicKeyInfo)
        return "sha256//" + base64.b64encode(hashlib.sha256(spki).digest()).decode("ascii")


def _write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def _pem_to_der(pem: str) -> bytes:
    body = "".join(line for line in pem.strip().splitlines() if not line.startswith("-----"))
    return base64.b64decode(body)


def _key_pem(key: Any) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _cert_pem(cert: Any) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return cert.public_bytes(serialization.Encoding.PEM)


def _make_ca() -> Tuple[Any, Any]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
                                         "PhoneCluster CA (%s)" % (socket.gethostname() or "pc"))])
    start = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start).not_valid_after(start + datetime.timedelta(days=365 * 20))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                     content_commitment=False, key_encipherment=False,
                                     data_encipherment=False, key_agreement=False,
                                     encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _make_leaf(ca: Tuple[Any, Any], common_name: str, names: List[str], addresses: List[str],
               server: bool) -> Tuple[Any, Any]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    ca_key, ca_cert = ca
    key = ec.generate_private_key(ec.SECP256R1())
    alt: List[Any] = [x509.DNSName(n) for n in dict.fromkeys(["localhost"] + names) if n]
    for raw in dict.fromkeys(["127.0.0.1", "::1"] + addresses):
        try:
            alt.append(x509.IPAddress(ipaddress.ip_address(raw)))
        except ValueError:
            continue
    start = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .issuer_name(ca_cert.subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start).not_valid_after(start + datetime.timedelta(days=365 * 10))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName(alt), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH,
                                              ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                       critical=False)
    )
    return key, builder.sign(ca_key, hashes.SHA256())


# --------------------------------------------------------------------------
# one-time join codes
# --------------------------------------------------------------------------

def normalize_code(code: Optional[str]) -> str:
    return "".join(ch for ch in str(code or "").upper() if ch in CODE_ALPHABET)


def _code_hash(code: str) -> str:
    return hashlib.sha256(normalize_code(code).encode("ascii")).hexdigest()


class JoinCodes:
    """Codes that let one new device join, once, for a few minutes.
    Only their hashes are stored (table ``join_codes``)."""

    def __init__(self, state: Any) -> None:
        self.state = state
        self.db = state.db

    def minutes(self) -> float:
        return float(self.state.setting("security.join_minutes", 10) or 10)

    def create(self, note: str = "") -> Dict[str, Any]:
        raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        code = "%s-%s" % (raw[:4], raw[4:])
        stamp = _now()
        expires = stamp + self.minutes() * 60
        self.db.execute("DELETE FROM join_codes WHERE expires < ?", (stamp - 86400,))
        self.db.execute("INSERT INTO join_codes (code_hash, created, expires, used, device_id, note) "
                        "VALUES (?,?,?,NULL,NULL,?)", (_code_hash(code), stamp, expires, note))
        return {"code": code, "expires": expires, "minutes": self.minutes()}

    def valid(self, code: Optional[str]) -> bool:
        if len(normalize_code(code)) != CODE_LENGTH:
            return False
        row = self.db.row("SELECT expires, used FROM join_codes WHERE code_hash=?", (_code_hash(code or ""),))
        return bool(row and row["used"] is None and float(row["expires"]) >= _now())

    def consume(self, code: Optional[str], device_id: str) -> bool:
        if len(normalize_code(code)) != CODE_LENGTH:
            return False
        cur = self.db.execute(
            "UPDATE join_codes SET used=?, device_id=? WHERE code_hash=? AND used IS NULL AND expires>=?",
            (_now(), device_id, _code_hash(code or ""), _now()))
        return bool(cur.rowcount)


# --------------------------------------------------------------------------
# lockouts
# --------------------------------------------------------------------------

class Guard:
    """Counts wrong PINs, join codes and device tokens; locks out guessers."""

    def __init__(self) -> None:
        self.failures: Dict[str, Deque[float]] = {}
        self.locks: Dict[str, Tuple[float, int]] = {}  # ip -> (until, times locked)
        self.recent: Deque[float] = deque()             # failures from outside the PC
        self.global_until = 0.0

    def blocked(self, ip: str, local: bool) -> float:
        """Seconds this address must wait, 0 when it may try."""
        stamp = _now()
        wait = 0.0
        until, _ = self.locks.get(ip, (0.0, 0))
        if until > stamp:
            wait = until - stamp
        if not local and self.global_until > stamp:
            wait = max(wait, self.global_until - stamp)
        return wait

    def fail(self, ip: str, local: bool) -> None:
        stamp = _now()
        window = self.failures.setdefault(ip, deque())
        window.append(stamp)
        while window and window[0] < stamp - PER_IP_WINDOW:
            window.popleft()
        if len(window) >= PER_IP_LIMIT:
            _, times = self.locks.get(ip, (0.0, 0))
            self.locks[ip] = (stamp + min(LOCK_MAX, LOCK_FIRST * (2 ** times)), times + 1)
            window.clear()
            log.warning("%s locked out after %d wrong attempts", ip, PER_IP_LIMIT)
        if not local:
            self.recent.append(stamp)
            while self.recent and self.recent[0] < stamp - GLOBAL_WINDOW:
                self.recent.popleft()
            if len(self.recent) >= GLOBAL_LIMIT and self.global_until < stamp:
                self.global_until = stamp + GLOBAL_LOCK
                self.recent.clear()
                log.warning("too many wrong attempts on the LAN: outside sign-ins paused for %.0f s",
                            GLOBAL_LOCK)

    def ok(self, ip: str) -> None:
        self.failures.pop(ip, None)


# --------------------------------------------------------------------------
# sessions (dashboard cookie)
# --------------------------------------------------------------------------

class Sessions:
    """A random cookie instead of the PIN itself."""

    def __init__(self) -> None:
        self.tokens: Dict[str, float] = {}

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        stamp = _now()
        self.tokens = {t: until for t, until in self.tokens.items() if until > stamp}
        self.tokens[token] = stamp + SESSION_SECONDS
        return token

    def valid(self, token: Optional[str]) -> bool:
        if not token:
            return False
        until = self.tokens.get(token)
        return bool(until and until > _now())

    def clear(self) -> None:
        self.tokens.clear()


# --------------------------------------------------------------------------
# the whole policy
# --------------------------------------------------------------------------

class Security:
    def __init__(self, state: Any) -> None:
        self.state = state
        self.tls = TLSFiles(Path(state.data) / "tls")
        self.codes = JoinCodes(state)
        self.guard = Guard()
        self.sessions = Sessions()
        self._own: Tuple[float, Set[str]] = (0.0, set())

    # ---------------------------------------------------------- addresses
    def own_addresses(self) -> Set[str]:
        stamp, cached = self._own
        if stamp > _now() - 30:
            return cached
        found: Set[str] = {"127.0.0.1", "::1"}
        try:
            import psutil

            for addrs in psutil.net_if_addrs().values():
                for addr in addrs:
                    if getattr(addr, "family", None) in (socket.AF_INET, socket.AF_INET6):
                        found.add(str(addr.address).split("%", 1)[0])
        except Exception:  # pragma: no cover - psutil missing/blocked
            found.update(self.state.lan_ips())
        self._own = (_now(), found)
        return found

    def is_local(self, host: Optional[str]) -> bool:
        """A client on the PC itself (loopback or one of the PC's addresses)."""
        if not host:
            return False
        host = str(host)
        if host.startswith("::ffff:"):
            host = host[7:]
        try:
            if ipaddress.ip_address(host).is_loopback:
                return True
        except ValueError:
            return False
        return host in self.own_addresses()

    def allowed_host(self, header: Optional[str]) -> bool:
        """Is the Host header one of this PC's names (DNS rebinding guard)?"""
        if not header:
            return True
        name = header.strip().lower()
        if name.startswith("["):
            name = name[1:].split("]", 1)[0]
        elif name.count(":") == 1:
            name = name.rsplit(":", 1)[0]
        if name in ("localhost", "127.0.0.1", "::1") or name in self.own_addresses():
            return True
        hostname = (socket.gethostname() or "").lower()
        if hostname and name in {hostname, hostname + ".local", hostname + ".lan", hostname + ".home",
                                 hostname + ".localdomain"}:
            return True
        extra = self.state.setting("security.extra_hosts", []) or []
        return name in {str(h).lower() for h in extra}

    @staticmethod
    def remote_allowed(path: str) -> bool:
        return path in REMOTE_EXACT or any(path.startswith(p) for p in REMOTE_PREFIXES)

    # ----------------------------------------------------------------- tls
    def ensure_tls(self) -> None:
        """Make the CA and the coordinator certificate on first use."""
        names = [n for n in {socket.gethostname()} if n]
        self.tls.ensure(names, self.state.lan_ips())

    def tls_bundle(self, device_id: str) -> Dict[str, str]:
        self.ensure_tls()
        bundle = self.tls.issue_device(device_id)
        bundle["ca_pem"] = self.tls.ca_text()
        return bundle

    # -------------------------------------------------------------- joining
    def join_info(self, base_url: str, code: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        from .qr import qr_svg

        info: Dict[str, Any] = {
            "url": base_url,
            "urls": self.state.urls(),
            "local_url": self.state.local_url(),
            "ca_fingerprint": self.tls.ca_fingerprint(),
            "server_pin": self.tls.server_pin(),
            "worker_url": "%s/worker.py" % base_url,
        }
        if code is not None:
            value = code["code"]
            browser = "%s/w?code=%s" % (base_url, value)
            info.update({
                "code": value,
                "expires": code["expires"],
                "minutes": code["minutes"],
                "browser_url": browser,
                "install_cmd": 'curl -fsSk --pinnedpubkey "%s" "%s/install.sh?code=%s" | sh'
                               % (info["server_pin"], base_url, value),
                # Windows: PowerShell or Command Prompt; curl.exe comes with Windows 10 and 11.
                # The + ' ' keeps iex quiet when curl fails, so only curl's own message shows.
                "windows_cmd": ("powershell -NoProfile -Command \"iex ((curl.exe -fsSk --pinnedpubkey "
                                "'%s' '%s/install.ps1?code=%s' | Out-String) + ' ')\""
                                % (info["server_pin"], base_url, value)),
                "manual_cmd": "python worker.py --url %s --fp %s --join %s"
                              % (base_url, info["ca_fingerprint"], value),
                "qr_svg": qr_svg(browser),
            })
            # the Termux command as a QR: scan it on the phone, copy, paste into Termux
            info["install_qr_svg"] = qr_svg(info["install_cmd"], error="L")
        return info


# --------------------------------------------------------------------------
# ASGI middleware
# --------------------------------------------------------------------------

class SecurityMiddleware:
    """Refuses unknown host names and keeps everything but the worker side
    to clients on the PC itself."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        state = getattr(getattr(scope.get("app"), "state", None), "cluster", None)
        security: Optional[Security] = getattr(state, "security", None)
        if security is None:  # pragma: no cover - app still starting
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        client = (scope.get("client") or ("", 0))[0]
        path = scope.get("path") or "/"
        if not security.allowed_host(headers.get("host")):
            log.warning("refused a request for unknown host %r from %s", headers.get("host"), client)
            await _deny(scope, send, 421, "unknown host name")
            return
        if not security.is_local(client) and not security.remote_allowed(path):
            await _deny(scope, send, 403, "the dashboard only opens on the PC itself",
                        html=path in ("/", "/index.html", "/join"))
            return
        await self.app(scope, receive, send)


async def _deny(scope: Dict[str, Any], send: Any, status: int, message: str, html: bool = False) -> None:
    if scope.get("type") == "websocket":
        await send({"type": "websocket.close", "code": 1008})
        return
    if html:
        body = REMOTE_PAGE.encode("utf-8")
        ctype = b"text/html; charset=utf-8"
    else:
        body = json.dumps({"error": message}).encode("utf-8")
        ctype = b"application/json"
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", ctype), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
