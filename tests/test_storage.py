"""Pooled storage: chunking, placement, repair and retrieval (SPEC §10)."""

from __future__ import annotations

import asyncio
import hashlib
import os
from typing import Any, Dict, List

import pytest

from conftest import FakeDevice, running_device, wait_for
from coordinator import storage as storage_module

pytestmark = pytest.mark.asyncio


def payload(size: int) -> bytes:
    return os.urandom(size)


async def make_phone(make_device: Any, name: str, free_mb: int = 8192, **kwargs: Any) -> FakeDevice:
    fake = await make_device(name=name, caps=["python", "shell", "termux", "storage"], **kwargs)
    fake.storage_free_mb = free_mb
    await fake.poll()
    return fake


# --------------------------------------------------------------------------
# chunking
# --------------------------------------------------------------------------

async def test_content_is_split_into_chunks(cluster: Any, client: Any, make_device: Any) -> None:
    cluster.config.set("storage", {"chunk_mb": 1, "replication": 1, "cache_mb": 512,
                                   "include_pc": False})
    phone = await make_phone(make_device, "phone1")
    data = payload(int(2.5 * 1024 * 1024))
    async with running_device(phone):
        blob = await cluster.storage.put_bytes(data, "big.bin", replication=1, wait=True)
    assert blob["n_chunks"] == 3
    assert blob["size"] == len(data)
    assert blob["chunk_size"] == 1024 * 1024
    chunks = cluster.storage.chunk_map(blob["id"])
    assert [c["size"] for c in chunks] == [1048576, 1048576, len(data) - 2 * 1048576]
    for index, chunk in enumerate(chunks):
        start = index * 1048576
        expected = hashlib.sha256(data[start : start + chunk["size"]]).hexdigest()
        assert chunk["chunk_id"] == expected


async def test_identical_chunks_are_shared(cluster: Any, make_device: Any) -> None:
    cluster.config.set("storage", {"chunk_mb": 1, "replication": 1, "cache_mb": 512,
                                   "include_pc": False})
    phone = await make_phone(make_device, "phone1")
    data = b"z" * (512 * 1024)
    async with running_device(phone):
        first = await cluster.storage.put_bytes(data, "a.bin", replication=1, wait=True)
        second = await cluster.storage.put_bytes(data, "b.bin", replication=1, wait=True)
    assert first["id"] != second["id"]
    ids = {c["chunk_id"] for c in cluster.storage.chunk_map(first["id"])}
    assert ids == {c["chunk_id"] for c in cluster.storage.chunk_map(second["id"])}


# --------------------------------------------------------------------------
# placement
# --------------------------------------------------------------------------

async def test_chunks_land_on_the_emptiest_phones(cluster: Any, make_device: Any) -> None:
    cluster.config.set("storage", {"chunk_mb": 4, "replication": 2, "cache_mb": 512,
                                   "include_pc": False})
    small = await make_phone(make_device, "small", free_mb=500)
    big = await make_phone(make_device, "big", free_mb=90000)
    medium = await make_phone(make_device, "medium", free_mb=5000)
    ordered = [d["name"] for d in cluster.storage.candidates()]
    assert ordered == ["big", "medium", "small"]

    async with running_device(small), running_device(big), running_device(medium):
        blob = await cluster.storage.put_bytes(b"x" * 1024, "s.bin", replication=2, wait=True)
    assert blob["status"] == "ready"
    holders = {loc["name"] for loc in cluster.storage.chunk_map(blob["id"])[0]["locations"]}
    assert holders == {"big", "medium"}
    assert set(big.store) and set(medium.store) and not small.store


async def test_pc_is_skipped_unless_it_is_the_only_device(cluster: Any, make_device: Any) -> None:
    cluster.config.set("storage", {"chunk_mb": 4, "replication": 1, "cache_mb": 512,
                                   "include_pc": False})
    pc = await make_device(name="pc", platform="windows",
                           caps=["python", "shell", "windows", "storage"])
    await pc.poll()
    # only the PC is online -> it is used as a fallback (see docs/DEVIATIONS.md)
    assert [d["name"] for d in cluster.storage.candidates()] == ["pc"]

    phone = await make_phone(make_device, "phone1")
    assert [d["name"] for d in cluster.storage.candidates()] == ["phone1"]

    cluster.config.set("storage.include_pc", True)
    assert {d["name"] for d in cluster.storage.candidates()} == {"phone1", "pc"}


async def test_devices_without_the_storage_cap_are_ignored(cluster: Any, make_device: Any) -> None:
    plain = await make_device(name="plain", caps=["python", "shell", "termux"])
    await plain.poll()
    assert cluster.storage.candidates() == []


async def test_ack_timeout_moves_the_chunk_to_another_device(
    cluster: Any, make_device: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage_module, "ACK_TIMEOUT", 0.5)
    cluster.config.set("storage", {"chunk_mb": 4, "replication": 1, "cache_mb": 512,
                                   "include_pc": False})
    silent = await make_phone(make_device, "silent", free_mb=90000)
    silent.accept_chunks = None  # never acks
    backup = await make_phone(make_device, "backup", free_mb=1000)

    async with running_device(silent), running_device(backup):
        blob = await cluster.storage.put_bytes(b"y" * 4096, "s.bin", replication=1, wait=True)
    assert blob["status"] == "ready"
    locations = cluster.storage.chunk_map(blob["id"])[0]["locations"]
    assert [loc["name"] for loc in locations] == ["backup"]
    assert silent.commands_of("store_chunk")  # it was asked first
    assert set(backup.store)


async def test_a_rejected_chunk_moves_on(
    cluster: Any, make_device: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage_module, "ACK_TIMEOUT", 2.0)
    cluster.config.set("storage", {"chunk_mb": 4, "replication": 1, "cache_mb": 512,
                                   "include_pc": False})
    grumpy = await make_phone(make_device, "grumpy", free_mb=90000)
    grumpy.accept_chunks = False
    backup = await make_phone(make_device, "backup", free_mb=1000)
    async with running_device(grumpy), running_device(backup):
        blob = await cluster.storage.put_bytes(b"y" * 4096, "s.bin", replication=1, wait=True)
    assert [loc["name"] for loc in cluster.storage.chunk_map(blob["id"])[0]["locations"]] == ["backup"]


async def test_nowhere_to_store_leaves_the_blob_lost(cluster: Any) -> None:
    blob = await cluster.storage.put_bytes(b"orphan", "o.bin", replication=1, wait=True)
    assert cluster.storage.recompute(blob["id"]) == "lost"
    assert any(e["kind"] == "blob_degraded" for e in cluster.db.select("events"))


# --------------------------------------------------------------------------
# status, degraded and repair
# --------------------------------------------------------------------------

async def test_degraded_when_replication_is_not_met(cluster: Any, make_device: Any) -> None:
    cluster.config.set("storage", {"chunk_mb": 4, "replication": 2, "cache_mb": 512,
                                   "include_pc": False})
    only = await make_phone(make_device, "only")
    async with running_device(only):
        blob = await cluster.storage.put_bytes(b"data", "d.bin", replication=2, wait=True)
    assert blob["status"] == "degraded"
    assert cluster.storage.status(blob["id"])["status"] == "degraded"


async def test_repair_replicates_to_a_new_device(cluster: Any, make_device: Any) -> None:
    cluster.config.set("storage", {"chunk_mb": 4, "replication": 2, "cache_mb": 512,
                                   "include_pc": False})
    first = await make_phone(make_device, "first")
    async with running_device(first):
        blob = await cluster.storage.put_bytes(b"repair me", "r.bin", replication=2, wait=True)
    assert cluster.storage.recompute(blob["id"]) == "degraded"

    second = await make_phone(make_device, "second")
    async with running_device(first), running_device(second):
        report = await cluster.storage.repair_once()
    assert blob["id"] in report["repaired"]
    assert cluster.storage.recompute(blob["id"]) == "ready"
    assert set(second.store)


async def test_repair_drops_locations_of_removed_devices(cluster: Any, make_device: Any) -> None:
    cluster.config.set("storage", {"chunk_mb": 4, "replication": 1, "cache_mb": 512,
                                   "include_pc": False})
    phone = await make_phone(make_device, "phone1")
    async with running_device(phone):
        blob = await cluster.storage.put_bytes(b"bye", "b.bin", replication=1, wait=True)
    assert cluster.storage.recompute(blob["id"]) == "ready"

    cluster.scheduler.remove_device(phone.device_id)
    report = await cluster.storage.repair_once()
    assert report["stale_locations"] == 1
    assert cluster.storage.recompute(blob["id"]) == "lost"


async def test_offline_holder_makes_the_blob_lost(cluster: Any, make_device: Any) -> None:
    cluster.config.set("storage", {"chunk_mb": 4, "replication": 1, "cache_mb": 512,
                                   "include_pc": False})
    phone = await make_phone(make_device, "phone1")
    async with running_device(phone):
        blob = await cluster.storage.put_bytes(b"offline", "o.bin", replication=1, wait=True)
    cluster.db.update("devices", {"id": phone.device_id}, {"status": "offline"})
    assert cluster.storage.recompute(blob["id"]) == "lost"


# --------------------------------------------------------------------------
# retrieval
# --------------------------------------------------------------------------

async def test_stream_serves_from_the_cache(cluster: Any, make_device: Any) -> None:
    cluster.config.set("storage", {"chunk_mb": 1, "replication": 1, "cache_mb": 512,
                                   "include_pc": False})
    phone = await make_phone(make_device, "phone1")
    data = payload(300 * 1024)
    async with running_device(phone):
        blob = await cluster.storage.put_bytes(data, "c.bin", replication=1, wait=True)
    chunks = [chunk async for chunk in cluster.storage.stream(blob["id"])]
    assert b"".join(chunks) == data
    assert not phone.commands_of("send_chunk")


async def test_stream_pulls_chunks_back_with_send_chunk(cluster: Any, make_device: Any) -> None:
    cluster.config.set("storage", {"chunk_mb": 1, "replication": 1, "cache_mb": 512,
                                   "include_pc": False})
    phone = await make_phone(make_device, "phone1")
    data = payload(int(2.2 * 1024 * 1024))
    async with running_device(phone):
        blob = await cluster.storage.put_bytes(data, "pull.bin", replication=1, wait=True)
        # wipe the coordinator's cache so the bytes must come back from the phone
        for path in cluster.storage.cache_dir.glob("*"):
            path.unlink()
        collected = b"".join([chunk async for chunk in cluster.storage.stream(blob["id"])])
    assert collected == data
    assert len(phone.commands_of("send_chunk")) == 3


async def test_stream_of_an_unavailable_chunk_raises(cluster: Any, make_device: Any) -> None:
    cluster.config.set("storage", {"chunk_mb": 4, "replication": 1, "cache_mb": 512,
                                   "include_pc": False})
    phone = await make_phone(make_device, "phone1")
    async with running_device(phone):
        blob = await cluster.storage.put_bytes(b"gone", "g.bin", replication=1, wait=True)
    for path in cluster.storage.cache_dir.glob("*"):
        path.unlink()
    phone.store.clear()
    cluster.db.update("devices", {"id": phone.device_id}, {"status": "offline"})
    with pytest.raises(storage_module.StorageError):
        [chunk async for chunk in cluster.storage.stream(blob["id"])]


async def test_stream_of_an_empty_blob_raises(cluster: Any) -> None:
    cluster.db.insert(
        "blobs",
        {"id": "deadbeef0000", "name": "x", "size": 0, "chunk_size": 0, "n_chunks": 0,
         "replication": 1, "status": "storing", "created": 0.0},
    )
    with pytest.raises(storage_module.StorageError):
        [chunk async for chunk in cluster.storage.stream("deadbeef0000")]


# --------------------------------------------------------------------------
# cache + delete
# --------------------------------------------------------------------------

async def test_cache_is_lru_trimmed_but_keeps_the_last_copy(
    cluster: Any, make_device: Any
) -> None:
    cluster.config.set("storage", {"chunk_mb": 1, "replication": 1, "cache_mb": 1,
                                   "include_pc": False})
    phone = await make_phone(make_device, "phone1")
    async with running_device(phone):
        blob = await cluster.storage.put_bytes(payload(3 * 1024 * 1024), "l.bin",
                                               replication=1, wait=True)
    assert blob["status"] == "ready"
    # replicated chunks may be dropped: the phone still has them
    assert cluster.storage.cache_bytes() <= 1024 * 1024

    orphan = payload(2 * 1024 * 1024)
    chunk_id = hashlib.sha256(orphan).hexdigest()
    cluster.storage.write_cache(chunk_id, orphan)
    cluster.storage.enforce_cache()
    assert cluster.storage.cached(chunk_id) is not None  # nobody else has it
    assert cluster.storage.cache_bytes() >= len(orphan)


async def test_delete_removes_chunks_everywhere(cluster: Any, make_device: Any) -> None:
    cluster.config.set("storage", {"chunk_mb": 4, "replication": 1, "cache_mb": 512,
                                   "include_pc": False})
    phone = await make_phone(make_device, "phone1")
    async with running_device(phone):
        blob = await cluster.storage.put_bytes(b"delete me", "d.bin", replication=1, wait=True)
        chunk_id = cluster.storage.chunk_map(blob["id"])[0]["chunk_id"]
        assert chunk_id in phone.store
        assert await cluster.storage.delete(blob["id"]) is True
        await wait_for(lambda: chunk_id not in phone.store, timeout=5)

    assert cluster.db.get("blobs", blob["id"]) is None
    assert cluster.storage.chunk_map(blob["id"]) == []
    assert cluster.storage.cached(chunk_id) is None
    assert chunk_id not in phone.store
    assert await cluster.storage.delete(blob["id"]) is False


async def test_delete_keeps_chunks_shared_with_another_blob(
    cluster: Any, make_device: Any
) -> None:
    cluster.config.set("storage", {"chunk_mb": 4, "replication": 1, "cache_mb": 512,
                                   "include_pc": False})
    phone = await make_phone(make_device, "phone1")
    data = b"shared bytes"
    async with running_device(phone):
        first = await cluster.storage.put_bytes(data, "a.bin", replication=1, wait=True)
        second = await cluster.storage.put_bytes(data, "b.bin", replication=1, wait=True)
        chunk_id = cluster.storage.chunk_map(first["id"])[0]["chunk_id"]
        await cluster.storage.delete(first["id"])
    assert cluster.storage.cached(chunk_id) is not None
    assert chunk_id in phone.store
    assert cluster.storage.recompute(second["id"]) == "ready"


async def test_bad_chunk_ids_are_rejected(cluster: Any) -> None:
    with pytest.raises(storage_module.StorageError):
        cluster.storage.chunk_path("nope")
    assert cluster.storage.cached("nope") is None


async def test_unknown_ack_and_upload_are_ignored(cluster: Any) -> None:
    assert cluster.storage.on_ack("d1", "a" * 64, True) is False
    assert cluster.storage.on_chunk_upload("a" * 64, "no-such-req", b"x") is False


# --------------------------------------------------------------------------
# REST surface
# --------------------------------------------------------------------------

async def test_storage_rest_round_trip(cluster: Any, client: Any, make_device: Any) -> None:
    cluster.config.set("storage", {"chunk_mb": 1, "replication": 1, "cache_mb": 512,
                                   "include_pc": False})
    phone = await make_phone(make_device, "phone1")
    data = payload(200 * 1024)
    async with running_device(phone):
        response = await client.post(
            "/api/storage",
            files={"file": ("rest.bin", data, "application/octet-stream")},
            data={"replication": "1"},
        )
        assert response.status_code == 200
        blob_id = response.json()["id"]
        await wait_for(lambda: cluster.storage.recompute(blob_id) == "ready", timeout=10)

        listing = await client.get("/api/storage")
        assert [b["id"] for b in listing.json()] == [blob_id]

        status = await client.get("/api/storage/%s/status" % blob_id)
        assert status.json()["status"] == "ready"
        assert status.json()["chunks"][0]["locations"][0]["name"] == "phone1"

        for path in cluster.storage.cache_dir.glob("*"):
            path.unlink()
        download = await client.get("/api/storage/%s" % blob_id)
        assert download.status_code == 200
        assert download.content == data

        assert (await client.delete("/api/storage/%s" % blob_id)).status_code == 200
    assert (await client.get("/api/storage/%s/status" % blob_id)).status_code == 404
    assert (await client.delete("/api/storage/%s" % blob_id)).status_code == 404


async def test_storage_from_an_existing_file_id(cluster: Any, client: Any, make_device: Any) -> None:
    cluster.config.set("storage", {"chunk_mb": 4, "replication": 1, "cache_mb": 512,
                                   "include_pc": False})
    phone = await make_phone(make_device, "phone1")
    upload = await client.put("/api/files", params={"name": "src.bin"}, content=b"from a file")
    file_id = upload.json()["id"]
    async with running_device(phone):
        response = await client.post(
            "/api/storage", json={"file_id": file_id, "replication": 1}
        )
        assert response.status_code == 200
        blob_id = response.json()["id"]
        await wait_for(lambda: cluster.storage.recompute(blob_id) == "ready", timeout=10)
        download = await client.get("/api/storage/%s" % blob_id)
    assert download.content == b"from a file"


async def test_storage_rest_errors(client: Any) -> None:
    assert (await client.post("/api/storage", json={})).status_code == 400
    assert (
        await client.post("/api/storage", json={"file_id": "f" * 64})
    ).status_code == 404
    assert (await client.get("/api/storage/nope")).status_code == 404
