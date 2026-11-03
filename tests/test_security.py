"""Keeping outsiders out (SPEC §3): only this PC manages the cluster, phones
join with one-time codes, guessing locks out, foreign host names are refused,
and the cluster's own certificates hold together."""

from __future__ import annotations

import ssl
import time
from typing import Any

import httpx
import pytest

from coordinator import security as security_module

PHONE_IP = "192.168.1.50"


def outsider(app: Any, ip: str = PHONE_IP) -> httpx.AsyncClient:
    """A client somewhere else on the LAN (a phone, or a stranger)."""
    transport = httpx.ASGITransport(app=app, client=(ip, 40000))
    return httpx.AsyncClient(transport=transport, base_url="http://cluster.test", timeout=30.0)


async def register(http: httpx.AsyncClient, **body: Any) -> httpx.Response:
    payload = {"name": "phone", "platform": "termux", "caps": ["python"], "info": {}, "slots": 1}
    payload.update(body)
    return await http.post("/api/w/register", json=payload)


# --------------------------------------------------------------------------
# only this PC manages the cluster
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_dashboard_and_api_are_closed_to_other_devices(client: Any, app: Any) -> None:
    pin = app.state.cluster.config.pin
    async with outsider(app) as phone:
        page = await phone.get("/")
        assert page.status_code == 403 and "only opens on the PC itself" in page.text
        for path in ("/api/devices", "/api/jobs", "/api/settings", "/api/join", "/api/cache",
                     "/static/app.js", "/join", "/qr.svg?text=x"):
            response = await phone.get(path, headers={"X-Pin": pin})
            assert response.status_code == 403, path
        assert (await phone.post("/api/login", json={"pin": pin})).status_code == 403
        assert (await phone.post("/api/join/code", headers={"X-Pin": pin})).status_code == 403
        # what a phone needs stays open
        for path in ("/w", "/w/worker.js", "/worker.py", "/ca.pem", "/api/info"):
            assert (await phone.get(path)).status_code == 200, path
    # the PC itself still works
    assert (await client.get("/api/devices")).status_code == 200


def test_the_dashboard_socket_refuses_other_devices(app: Any) -> None:
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from starlette.testclient import TestClient

        pin = app.state.cluster.config.pin
        # starlette's test client calls the server "testserver"
        app.state.cluster.config.set("security.extra_hosts", ["cluster.test", "testserver"])
        # no "with": the app's startup/shutdown stays out of this test
        phone = TestClient(app, base_url="http://cluster.test", client=(PHONE_IP, 40000))
        with pytest.raises(Exception):
            with phone.websocket_connect("/ws/dashboard?pin=%s" % pin):
                pass
        local = TestClient(app, base_url="http://cluster.test", client=("127.0.0.1", 40001))
        with local.websocket_connect("/ws/dashboard?pin=%s" % pin) as socket:
            assert socket.receive_json()["ev"] == "snapshot"
        with pytest.raises(Exception):
            with local.websocket_connect("/ws/dashboard?pin=000000"):
                pass


@pytest.mark.asyncio
async def test_unknown_host_names_are_refused(client: Any, app: Any) -> None:
    """A web page cannot reach the cluster through DNS rebinding."""
    response = await client.get("/api/devices", headers={"Host": "attacker.example:8765"})
    assert response.status_code == 421
    assert (await client.get("/api/devices", headers={"Host": "127.0.0.1:8764"})).status_code == 200


@pytest.mark.asyncio
async def test_a_session_cookie_is_random_and_lasts(app: Any) -> None:
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://cluster.test") as pc:
            pin = app.state.cluster.config.pin
            response = await pc.post("/api/login", json={"pin": pin})
            assert response.status_code == 200
            cookie = pc.cookies.get("pc_session")
            assert cookie and pin not in cookie and len(cookie) > 30
            assert (await pc.get("/api/devices")).status_code == 200
            app.state.cluster.security.sessions.clear()
            assert (await pc.get("/api/devices")).status_code == 401


# --------------------------------------------------------------------------
# one-time join codes
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_phone_joins_once_with_a_code_and_never_with_the_pin(client: Any, app: Any) -> None:
    cluster = app.state.cluster
    pin = cluster.config.pin
    code = (await client.post("/api/join/code")).json()["code"]
    async with outsider(app) as phone:
        assert (await register(phone, pin=pin)).status_code == 401  # the PIN never works from outside
        joined = await register(phone, join_code=code.lower().replace("-", ""))
        assert joined.status_code == 200
        body = joined.json()
        tls = body["tls"]
        assert tls["ca_pem"] == (await phone.get("/ca.pem")).text
        assert "BEGIN CERTIFICATE" in tls["cert_pem"] and "PRIVATE KEY" in tls["key_pem"]
        again = await register(phone, join_code=code)
        assert again.status_code == 401 and "Join page" in again.json()["error"]
        # its token works from anywhere, for the worker side only
        poll = await phone.post("/api/w/poll", json={"status": {}}, headers={"X-Device-Token": body["token"]})
        assert poll.status_code == 200
        reattach = await register(phone, device_id=body["device_id"], token=body["token"])
        assert reattach.status_code == 200 and reattach.json()["device_id"] == body["device_id"]


@pytest.mark.asyncio
async def test_codes_expire(client: Any, app: Any) -> None:
    code = (await client.post("/api/join/code")).json()["code"]
    app.state.cluster.db.execute("UPDATE join_codes SET expires=?", (time.time() - 1,))
    async with outsider(app) as phone:
        assert (await register(phone, join_code=code)).status_code == 401
        assert (await phone.get("/install.sh", params={"code": code})).status_code == 403


@pytest.mark.asyncio
async def test_the_install_script_is_only_made_for_a_valid_code(client: Any, app: Any) -> None:
    cluster = app.state.cluster
    code = (await client.post("/api/join/code")).json()["code"]
    async with outsider(app) as phone:
        assert (await phone.get("/install.sh")).status_code == 403
        assert (await phone.get("/install.sh", params={"code": "AAAA-BBBB"})).status_code == 403
        script = await phone.get("/install.sh", params={"code": code})
        assert script.status_code == 200
        text = script.text
        assert cluster.config.pin not in text
        assert cluster.security.tls.ca_text() in text
        assert '--pinnedpubkey "%s"' % cluster.security.tls.server_pin() in text
        assert '--ca cluster-ca.pem --join "%s"' % code in text
    # looking at the script does not use the code up
    assert cluster.security.codes.valid(code)


@pytest.mark.asyncio
async def test_the_windows_installer_and_its_python_need_a_valid_code(client: Any, app: Any, monkeypatch: Any,
                                                                      tmp_path: Any) -> None:
    from coordinator import static_routes

    cluster = app.state.cluster
    packed = tmp_path / "python-win.zip"
    packed.write_bytes(b"PK not really")
    monkeypatch.setattr(static_routes, "portable_python", lambda root, data: (packed, "ab" * 32))
    code = (await client.post("/api/join/code")).json()["code"]
    async with outsider(app) as laptop:
        for path in ("/install.ps1", "/python-win.zip"):
            assert (await laptop.get(path)).status_code == 403
            assert (await laptop.get(path, params={"code": "AAAA-BBBB"})).status_code == 403
        script = await laptop.get("/install.ps1", params={"code": code})
        assert script.status_code == 200
        text = script.text
        assert text.isascii() and cluster.config.pin not in text
        assert cluster.security.tls.ca_text() in text
        assert "$pin = '%s'" % cluster.security.tls.server_pin() in text
        assert "$code = '%s'" % code in text and "$url = 'http://cluster.test'" in text
        assert "$pythonSha = '%s'" % ("ab" * 32) in text
        assert not any(mark in text for mark in ("{URL}", "{CODE}", "{SPKI}", "{PYZIP}", "{CA}"))
        python = await laptop.get("/python-win.zip", params={"code": code})
        assert python.status_code == 200 and python.content == b"PK not really"
    assert cluster.security.codes.valid(code)
    # a PC without its own portable Python: the laptop has to bring one
    monkeypatch.setattr(static_routes, "portable_python", lambda root, data: None)
    async with outsider(app) as laptop:
        assert "$pythonSha = ''" in (await laptop.get("/install.ps1", params={"code": code})).text
        assert (await laptop.get("/python-win.zip", params={"code": code})).status_code == 404


@pytest.mark.asyncio
async def test_service_calls_from_phones_need_their_device_token(client: Any, app: Any) -> None:
    pin = app.state.cluster.config.pin
    code = (await client.post("/api/join/code")).json()["code"]
    async with outsider(app) as phone:
        token = (await register(phone, join_code=code)).json()["token"]
        assert (await phone.get("/api/services", headers={"X-Pin": pin})).status_code == 401
        assert (await phone.get("/api/services", headers={"X-Device-Token": token})).status_code == 200


# --------------------------------------------------------------------------
# guessing locks out
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_guessing_join_codes_locks_the_address_out(client: Any, app: Any) -> None:
    code = (await client.post("/api/join/code")).json()["code"]
    async with outsider(app) as guesser:
        for _ in range(security_module.PER_IP_LIMIT):
            assert (await register(guesser, join_code="ZZZZ-ZZZZ")).status_code == 401
        locked = await register(guesser, join_code=code)  # even the right code waits now
        assert locked.status_code == 429 and "try again" in locked.json()["error"]
    async with outsider(app, "192.168.1.51") as neighbour:
        assert (await register(neighbour, join_code=code)).status_code == 200


@pytest.mark.asyncio
async def test_guessing_device_tokens_counts_too(app: Any) -> None:
    async with outsider(app) as guesser:
        for _ in range(security_module.PER_IP_LIMIT - 1):
            response = await guesser.post("/api/w/poll", json={"status": {}}, headers={"X-Device-Token": "x"})
            assert response.status_code == 401
        response = await guesser.post("/api/w/poll", json={"status": {}}, headers={"X-Device-Token": "x"})
        assert response.status_code == 429


@pytest.mark.asyncio
async def test_a_wave_of_guesses_pauses_outside_joins_but_not_this_pc(client: Any, app: Any) -> None:
    for n in range(security_module.GLOBAL_LIMIT):
        async with outsider(app, "10.0.%d.%d" % (n // 200, n % 200 + 2)) as guesser:
            await register(guesser, join_code="ZZZZ-ZZZZ")
    code = (await client.post("/api/join/code")).json()["code"]
    async with outsider(app, "10.9.9.9") as fresh:
        assert (await register(fresh, join_code=code)).status_code == 429
    assert (await client.get("/api/devices")).status_code == 200  # the PC itself is never locked out


@pytest.mark.asyncio
async def test_wrong_pins_on_the_pc_lock_out_for_a_while(app: Any) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://cluster.test") as pc:
        for _ in range(security_module.PER_IP_LIMIT):
            assert (await pc.get("/api/devices", headers={"X-Pin": "000000"})).status_code == 401
        right = app.state.cluster.config.pin
        assert (await pc.get("/api/devices", headers={"X-Pin": right})).status_code == 429
        # no PIN at all is not a guess
        app.state.cluster.security.guard.locks.clear()
        for _ in range(10):
            assert (await pc.get("/api/devices")).status_code == 401
        assert (await pc.get("/api/devices", headers={"X-Pin": right})).status_code == 200


# --------------------------------------------------------------------------
# the cluster's own certificates
# --------------------------------------------------------------------------

def test_certificates_chain_to_the_cluster_ca(tmp_path: Any) -> None:
    from cryptography import x509

    tls = security_module.TLSFiles(tmp_path / "tls")
    tls.ensure(["pc"], ["192.168.1.5"])
    first = tls.ca_fingerprint()
    tls.ensure(["pc"], ["192.168.1.5"])
    assert tls.ca_fingerprint() == first  # made once, kept
    ca = x509.load_pem_x509_certificate(tls.ca_pem.read_bytes())
    server = x509.load_pem_x509_certificate(tls.server_pem.read_bytes())
    server.verify_directly_issued_by(ca)
    device = x509.load_pem_x509_certificate(tls.issue_device("abc")["cert_pem"].encode())
    device.verify_directly_issued_by(ca)
    sans = server.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "192.168.1.5" in [str(ip) for ip in sans.get_values_for_type(x509.IPAddress)]
    assert tls.server_pin().startswith("sha256//")
    # what a worker does: trust exactly this CA, host name unchecked
    ctx = ssl.create_default_context(cafile=str(tls.ca_pem))
    ctx.check_hostname = False
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_join_codes_are_hashed_and_typeable(state: Any) -> None:
    made = state.security.codes.create()
    code = made["code"]
    assert len(code) == 9 and code[4] == "-"
    assert not any(ch in code for ch in "01IO")
    stored = state.db.select("join_codes")
    assert code.replace("-", "") not in str(stored)  # only a hash is kept
    assert state.security.codes.valid(code.lower())
    assert state.security.codes.consume(code, "dev1")
    assert not state.security.codes.consume(code, "dev2")


def test_local_means_this_pc(state: Any) -> None:
    sec = state.security
    assert sec.is_local("127.0.0.1") and sec.is_local("::1")
    for ip in state.lan_ips():
        assert sec.is_local(ip)
    assert not sec.is_local("192.0.2.77") and not sec.is_local("") and not sec.is_local("evil")
    assert sec.allowed_host("127.0.0.1:8764") and sec.allowed_host("localhost")
    assert not sec.allowed_host("rebind.attacker.example")
