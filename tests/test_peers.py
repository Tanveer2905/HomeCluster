"""Phone-to-phone transfers (SPEC 8.3): the coordinator side over the API,
and the worker's temporary sending port + piece-by-piece download for real."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from conftest import ROOT, wait_for

MB = 1024 * 1024


# --------------------------------------------------------------------------
# coordinator side
# --------------------------------------------------------------------------

async def peer_phone(make_device: Any, name: str) -> Any:
    fake = await make_device(name=name, caps=["python", "shell", "termux", "peer"])
    await fake.poll()
    return fake


@pytest.mark.asyncio
async def test_a_task_names_inputs_another_phone_already_has(cluster: Any, make_device: Any) -> None:
    cluster.config.set("transfers", {"peer": True, "min_mb": 1})
    big = cluster.files.add_bytes(os.urandom(2 * MB), "video.mp4")
    small = cluster.files.add_bytes(b"tiny", "tiny.txt")
    a = await peer_phone(make_device, "a")
    b = await peer_phone(make_device, "b")
    cluster.peers.fetched_inputs(cluster.db.get("devices", a.device_id), {"inputs": [big["id"], small["id"]]})
    payload = {"inputs": [big["id"], small["id"], {"file_id": big["id"], "offset": 0, "length": 5}]}
    assert cluster.peers.peer_inputs(payload, b.device_id) == [big["id"]]  # small and ranges: coordinator
    assert cluster.peers.peer_inputs(payload, a.device_id) == []          # nobody else has it
    cluster.config.set("transfers.peer", False)
    assert cluster.peers.peer_inputs(payload, b.device_id) == []


@pytest.mark.asyncio
async def test_the_fetch_handshake_hands_out_one_time_tokens(cluster: Any, client: Any,
                                                            make_device: Any) -> None:
    cluster.config.set("transfers", {"peer": True, "min_mb": 1})
    data = os.urandom(int(9.5 * MB))
    big = cluster.files.add_bytes(data, "model.gguf")
    a = await peer_phone(make_device, "a")
    b = await peer_phone(make_device, "b")
    cluster.peers.note(a.device_id, [big["id"]])

    async def sender() -> Dict[str, Any]:
        for _ in range(40):
            for command in await a.poll():
                if command.get("cmd") == "peer_serve":
                    await client.post("/api/w/peer/ready", headers=a.headers, json={
                        "offer_id": command["offer_id"], "ok": True, "port": 40123, "host": "10.0.0.7"})
                    return command
        raise AssertionError("no peer_serve command")

    serve_task = asyncio.create_task(sender())
    reply = await client.post("/api/w/peer/fetch", headers=b.headers, json={"file_id": big["id"]})
    command = await serve_task
    plan = reply.json()
    assert command["file_id"] == big["id"] and len(command["token"]) >= 20
    (source,) = plan["sources"]
    assert source["url"] == "https://10.0.0.7:40123/f/%s" % big["id"]
    assert source["token"] == command["token"] and source["name"] == "a"
    piece = plan["piece_bytes"]
    assert plan["pieces"] == [hashlib.sha256(data[i:i + piece]).hexdigest()
                              for i in range(0, len(data), piece)]
    # the fetcher reports back: the sender closes its port and the fetcher holds the file now
    done = await client.post("/api/w/peer/result", headers=b.headers, json={
        "offer_id": source["offer_id"], "ok": True, "bytes": len(data), "seconds": 1.0})
    assert done.json()["ok"] is True
    closes = [c for c in await a.poll() if c.get("cmd") == "peer_close"]
    assert closes and closes[0]["offer_id"] == source["offer_id"]
    assert b.device_id in cluster.peers.holdings[big["id"]]
    assert cluster.peers.stats()["peer_bytes"] == len(data)
    # someone else's offer cannot be closed or reported on
    assert (await client.post("/api/w/peer/result", headers=a.headers, json={
        "offer_id": source["offer_id"], "ok": True})).json()["ok"] is False


@pytest.mark.asyncio
async def test_a_holder_without_the_file_is_forgotten(cluster: Any, client: Any, make_device: Any) -> None:
    cluster.config.set("transfers", {"peer": True, "min_mb": 1})
    big = cluster.files.add_bytes(os.urandom(2 * MB), "x.bin")
    a = await peer_phone(make_device, "a")
    b = await peer_phone(make_device, "b")
    cluster.peers.note(a.device_id, [big["id"]])

    async def sender() -> None:
        for _ in range(40):
            for command in await a.poll():
                if command.get("cmd") == "peer_serve":
                    await client.post("/api/w/peer/ready", headers=a.headers, json={
                        "offer_id": command["offer_id"], "ok": False, "error": "not here"})
                    return

    task = asyncio.create_task(sender())
    plan = (await client.post("/api/w/peer/fetch", headers=b.headers, json={"file_id": big["id"]})).json()
    await task
    assert plan["sources"] == []
    assert a.device_id not in cluster.peers.holdings.get(big["id"], {})


@pytest.mark.asyncio
async def test_small_files_and_warm_phones_are_left_alone(cluster: Any, client: Any, make_device: Any) -> None:
    cluster.config.set("transfers", {"peer": True, "min_mb": 4})
    small = cluster.files.add_bytes(os.urandom(MB), "s.bin")
    a = await peer_phone(make_device, "a")
    b = await peer_phone(make_device, "b")
    cluster.peers.note(a.device_id, [small["id"]])
    plan = (await client.post("/api/w/peer/fetch", headers=b.headers, json={"file_id": small["id"]})).json()
    assert plan["sources"] == [] and "pieces" not in plan
    cluster.config.set("transfers.min_mb", 0.5)
    await a.poll(temp=41.0)  # resting after heat: never asked to send
    assert cluster.peers.holders(small["id"], exclude=b.device_id) == []


# --------------------------------------------------------------------------
# worker side, real sockets
# --------------------------------------------------------------------------

def load_worker() -> Any:
    name = "pc_worker_peers"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "worker" / "worker.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


class StubHttp:
    """Stands in for the worker's HttpClient: records calls, serves pieces of
    ``data`` as the coordinator would."""

    def __init__(self, data: bytes) -> None:
        self.host = "127.0.0.1"
        self.data = data
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.ranges = 0

    def call(self, method: str, path: str, obj: Any = None, timeout: float = 30.0) -> Dict[str, Any]:
        self.calls.append((path, obj))
        return {}

    def request(self, method: str, path: str, headers: Any = None, timeout: float = 30.0, **_: Any) -> Any:
        start, end = [int(x) for x in headers["Range"].split("=")[1].split("-")]
        self.ranges += 1
        return 206, self.data[start:end + 1]


@pytest.fixture(scope="module")
def cluster_ca(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """The cluster's CA, as the coordinator makes it in data/tls."""
    from coordinator.security import TLSFiles

    tls = TLSFiles(tmp_path_factory.mktemp("tls"))
    tls.ensure(["test"], ["127.0.0.1"])
    return tls


class StubWorker:
    """What PeerServer / PieceDownload use of a Worker: its cache, its
    coordinator client and its cluster certificates."""

    def __init__(self, module: Any, directory: Path, data: bytes, ca: Any,
                 issuer: Optional[Any] = None) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.module = module
        self.cache = module.FileCache(str(directory / "cache"), cap_mb=500)
        self.http = StubHttp(data)
        self.stopping = False
        bundle = (issuer or ca).issue_device(directory.name)
        self.cert_file = str(directory / "device.pem")
        self.key_file = str(directory / "device.key")
        self.ca_file = str(directory / "ca.pem")
        Path(self.cert_file).write_text(bundle["cert_pem"])
        Path(self.key_file).write_text(bundle["key_pem"])
        Path(self.ca_file).write_text(ca.ca_text())

    def has_device_cert(self) -> bool:
        return os.path.isfile(self.cert_file)

    def peer_context(self) -> Any:
        return self.module.tls_context(self.ca_file)


def offer(module: Any, worker: Any, file_id: str, token: str) -> Tuple[str, int]:
    server = module.PeerServer(worker)
    server.offer({"offer_id": "o1", "file_id": file_id, "token": token, "ttl": 30})
    ready = [obj for path, obj in worker.http.calls if path == "/api/w/peer/ready"][-1]
    assert ready["ok"], ready
    worker._server = server
    return ready["host"], ready["port"]


def test_the_sending_port_serves_only_the_offered_file_with_the_token(tmp_path: Path, cluster_ca: Any) -> None:
    module = load_worker()
    data = os.urandom(300000)
    file_id = hashlib.sha256(data).hexdigest()
    sender = StubWorker(module, tmp_path / "a", data, cluster_ca)
    sender.cache.put_bytes(file_id, data)
    other = hashlib.sha256(b"other").hexdigest()
    sender.cache.put_bytes(other, b"other")
    host, port = offer(module, sender, file_id, "secret-token")
    ctx = sender.peer_context()
    get = module.http_get_range
    assert get(host, port, "/f/%s" % file_id, 10, 19, {module.PEER_TOKEN_HEADER: "secret-token"},
               context=ctx) == data[10:20]
    for path, token in (("/f/%s" % file_id, "wrong"), ("/f/%s" % other, "secret-token"),
                        ("/f/%s" % file_id, "")):
        with pytest.raises(module.ApiError) as err:
            get(host, port, path, 0, 9, {module.PEER_TOKEN_HEADER: token}, context=ctx)
        assert err.value.status == 403
    # the port speaks only HTTPS: a plain request gets nothing
    import http.client

    plain = http.client.HTTPConnection(host, port, timeout=5)
    with pytest.raises(Exception):
        plain.request("GET", "/f/%s" % file_id, headers={module.PEER_TOKEN_HEADER: "secret-token"})
        plain.getresponse().read()
    plain.close()
    sender._server.close("o1")  # last offer gone: the port closes
    assert sender._server.server is None
    with pytest.raises(Exception):
        get(host, port, "/f/%s" % file_id, 0, 9, {module.PEER_TOKEN_HEADER: "secret-token"}, context=ctx)


def test_a_device_with_a_foreign_certificate_is_not_trusted(tmp_path: Path, cluster_ca: Any) -> None:
    """Someone on the Wi-Fi with their own certificate cannot pose as a phone."""
    from coordinator.security import TLSFiles

    module = load_worker()
    impostor_ca = TLSFiles(tmp_path / "impostor-ca")
    impostor_ca.ensure([], [])
    data = os.urandom(3 * MB)
    file_id = hashlib.sha256(data).hexdigest()
    impostor = StubWorker(module, tmp_path / "impostor", data, cluster_ca, issuer=impostor_ca)
    impostor.cache.put_bytes(file_id, data)
    host, port = offer(module, impostor, file_id, "tok")
    fetcher = StubWorker(module, tmp_path / "b", data, cluster_ca)
    with pytest.raises(Exception):
        module.http_get_range(host, port, "/f/%s" % file_id, 0, 9, {module.PEER_TOKEN_HEADER: "tok"},
                              context=fetcher.peer_context())
    piece = 1 * MB
    plan = {"size": len(data), "piece_bytes": piece,
            "pieces": [hashlib.sha256(data[i:i + piece]).hexdigest() for i in range(0, len(data), piece)],
            "sources": [{"offer_id": "o1", "device_id": "x", "name": "x", "token": "tok",
                         "url": "https://%s:%d/f/%s" % (host, port, file_id)}]}
    dest = str(tmp_path / "out.bin")
    assert module.PieceDownload(fetcher, file_id, plan, dest).run()  # all from the coordinator
    assert Path(dest).read_bytes() == data
    (report,) = [obj for p, obj in fetcher.http.calls if p == "/api/w/peer/result"]
    assert report["ok"] is False and report["bytes"] == 0
    impostor._server.close("o1")


def test_pieces_come_from_the_phone_and_the_coordinator_at_once(tmp_path: Path, cluster_ca: Any) -> None:
    module = load_worker()
    data = os.urandom(int(10.5 * MB))
    file_id = hashlib.sha256(data).hexdigest()
    piece = 1 * MB
    sender = StubWorker(module, tmp_path / "a", data, cluster_ca)
    sender.cache.put_bytes(file_id, data)
    host, port = offer(module, sender, file_id, "tok")
    fetcher = StubWorker(module, tmp_path / "b", data, cluster_ca)
    plan = {"size": len(data), "piece_bytes": piece,
            "pieces": [hashlib.sha256(data[i:i + piece]).hexdigest() for i in range(0, len(data), piece)],
            "sources": [{"offer_id": "o1", "device_id": "a", "name": "a", "token": "tok",
                         "url": "https://%s:%d/f/%s" % (host, port, file_id)}]}
    dest = str(tmp_path / "out.bin")
    assert module.PieceDownload(fetcher, file_id, plan, dest).run()
    assert Path(dest).read_bytes() == data
    (report,) = [obj for path, obj in fetcher.http.calls if path == "/api/w/peer/result"]
    assert report["ok"] is True and report["bytes"] > 0          # the phone sent some pieces
    assert fetcher.http.ranges > 0                               # and the coordinator others
    assert report["bytes"] + fetcher.http.ranges * piece >= len(data)
    sender._server.close("o1")


def test_a_phone_sending_damaged_pieces_is_caught(tmp_path: Path, cluster_ca: Any) -> None:
    module = load_worker()
    data = os.urandom(6 * MB)
    file_id = hashlib.sha256(data).hexdigest()
    piece = 1 * MB
    sender = StubWorker(module, tmp_path / "a", data, cluster_ca)
    sender.cache.put_bytes(file_id, b"\x00" * len(data))  # its copy is rotten
    host, port = offer(module, sender, file_id, "tok")
    fetcher = StubWorker(module, tmp_path / "b", data, cluster_ca)
    plan = {"size": len(data), "piece_bytes": piece,
            "pieces": [hashlib.sha256(data[i:i + piece]).hexdigest() for i in range(0, len(data), piece)],
            "sources": [{"offer_id": "o1", "device_id": "a", "name": "a", "token": "tok",
                         "url": "https://%s:%d/f/%s" % (host, port, file_id)}]}
    dest = str(tmp_path / "out.bin")
    assert module.PieceDownload(fetcher, file_id, plan, dest).run()
    assert Path(dest).read_bytes() == data  # every piece ended up right
    (report,) = [obj for path, obj in fetcher.http.calls if path == "/api/w/peer/result"]
    assert report["ok"] is False and report["error"] == "hash mismatch"
    sender._server.close("o1")


def test_tasks_starting_together_download_a_shared_input_once(tmp_path: Path) -> None:
    """Twelve tasks needing the same module at the same moment: one download,
    no task trips over another's half-written file."""
    module = load_worker()
    data = os.urandom(200000)
    file_id = hashlib.sha256(data).hexdigest()

    class CountingHttp(StubHttp):
        downloads = 0

        def request(self, method: str, path: str, headers: Any = None, timeout: float = 30.0, **_: Any) -> Any:
            CountingHttp.downloads += 1
            threading.Event().wait(0.05)  # slow enough for every task to arrive meanwhile
            return 200, self.data

    worker = object.__new__(module.Worker)
    worker.cache = module.FileCache(str(tmp_path / "cache"), cap_mb=50)
    worker.http = CountingHttp(data)
    worker._fetch_guard = threading.Lock()
    worker._fetch_locks = {}
    results: List[Any] = []
    errors: List[BaseException] = []

    def task() -> None:
        try:
            results.append(worker.fetch_inputs([file_id]))
        except BaseException as exc:  # pragma: no cover - the failure being guarded against
            errors.append(exc)

    threads = [threading.Thread(target=task) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == [] and len(results) == 12
    assert CountingHttp.downloads == 1
    assert all(Path(paths[0]).read_bytes() == data for paths in results)


def test_offers_expire_and_close_the_port(tmp_path: Path, cluster_ca: Any) -> None:
    module = load_worker()
    data = b"x" * 1000
    file_id = hashlib.sha256(data).hexdigest()
    sender = StubWorker(module, tmp_path / "a", data, cluster_ca)
    sender.cache.put_bytes(file_id, data)
    server = module.PeerServer(sender)
    server.offer({"offer_id": "o1", "file_id": file_id, "token": "t", "ttl": 5})
    assert server.server is not None
    with server.lock:
        server.offers["o1"]["until"] = 0  # nobody came
    deadline = threading.Event()
    for _ in range(40):
        if server.server is None:
            break
        deadline.wait(0.25)
    assert server.server is None
    missing = module.PeerServer(sender)
    missing.offer({"offer_id": "o2", "file_id": "f" * 64, "token": "t"})
    ready = [obj for path, obj in sender.http.calls if path == "/api/w/peer/ready"][-1]
    assert ready == {"offer_id": "o2", "ok": False, "error": "not here"}
