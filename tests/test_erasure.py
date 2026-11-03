"""Erasure-coded pooled storage and scrubbing (SPEC §10)."""

from __future__ import annotations

import hashlib
import itertools
import os
from typing import Any, Dict, List

import pytest

from conftest import FakeDevice, running_device, wait_for
from coordinator import erasure

MB = 1024 * 1024


# --------------------------------------------------------------------------
# the code itself
# --------------------------------------------------------------------------

@pytest.mark.parametrize("k,m", [(2, 1), (3, 1), (4, 1), (4, 2), (6, 2), (3, 3)])
def test_any_k_pieces_rebuild_everything(k: int, m: int) -> None:
    lengths = [1000 + 37 * i for i in range(k)]
    data = [os.urandom(n) for n in lengths]
    parity = erasure.encode(data, m)
    pieces = dict(enumerate(data + parity))
    for lost in itertools.combinations(range(k + m), m):
        have = {i: p for i, p in pieces.items() if i not in lost}
        assert erasure.reconstruct(have, k, m, lengths, range(k + m)) == pieces


def test_first_parity_is_plain_xor_and_short_stripes_pad_with_zeros() -> None:
    data = [b"\x01\x02\x03", b"\x10\x20"]
    assert erasure.encode(data, 1)[0] == bytes([0x11, 0x22, 0x03])
    lengths = [3, 2, 0]  # a short last stripe: the third position is padding
    parity = erasure.encode(data + [b""], 1)
    rebuilt = erasure.reconstruct({0: data[0], 3: parity[0]}, 3, 1, lengths, [1])
    assert rebuilt[1] == data[1]


def test_too_few_pieces_is_an_error() -> None:
    data = [b"abc", b"def", b"ghi"]
    parity = erasure.encode(data, 1)
    with pytest.raises(ValueError):
        erasure.reconstruct({0: data[0], 3: parity[0]}, 3, 1, [3, 3, 3], [1, 2])


def test_layout_by_device_count() -> None:
    assert erasure.shape(2) == {}
    assert erasure.shape(3) == {"k": 2, "m": 1}
    assert erasure.shape(4) == {"k": 3, "m": 1}
    assert erasure.shape(6) == {"k": 4, "m": 2}
    assert erasure.shape(20) == {"k": 6, "m": 2}


# --------------------------------------------------------------------------
# pooled storage with parity
# --------------------------------------------------------------------------

async def phones(make_device: Any, count: int, prefix: str = "phone") -> List[FakeDevice]:
    out = []
    for i in range(count):
        fake = await make_device(name="%s%d" % (prefix, i), caps=["python", "shell", "termux", "storage"])
        fake.storage_free_mb = 9000 - i
        await fake.poll()
        out.append(fake)
    return out


def ec_settings(cluster: Any) -> None:
    cluster.config.set("storage", {"chunk_mb": 1, "replication": 2, "cache_mb": 512,
                                   "include_pc": False, "scheme": "auto", "scrub_hours": 24})


class Running:
    def __init__(self, devices: List[FakeDevice]) -> None:
        self.devices = devices
        self.contexts: List[Any] = []

    async def __aenter__(self) -> "Running":
        for device in self.devices:
            ctx = running_device(device)
            await ctx.__aenter__()
            self.contexts.append(ctx)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        for ctx in self.contexts:
            await ctx.__aexit__(*exc)


def holders(cluster: Any, blob_id: str) -> Dict[str, List[str]]:
    """piece -> device names, data and parity alike."""
    out = {}
    status = cluster.storage.status(blob_id)
    for piece in status["chunks"] + status.get("parity", []):
        out[piece["chunk_id"]] = [loc["name"] for loc in piece["locations"]]
    return out


@pytest.mark.asyncio
async def test_four_phones_store_three_data_and_one_parity_piece(cluster: Any, make_device: Any) -> None:
    ec_settings(cluster)
    devices = await phones(make_device, 4)
    data = os.urandom(int(5.5 * MB))
    async with Running(devices):
        blob = await cluster.storage.put_bytes(data, "video.bin", wait=True)
    assert blob["scheme"] == "ec" and (blob["ec_k"], blob["ec_m"]) == (3, 1)
    assert blob["status"] == "ready"
    status = cluster.storage.status(blob["id"])
    assert status["protection"]["overhead"] == 1.33 and status["protection"]["survives"] == 1
    assert len(status["chunks"]) == 6 and len(status["parity"]) == 2
    # every piece of a stripe sits on its own phone
    for stripe in cluster.storage.stripes(blob):
        places = [loc["device_id"] for _, cid, _ in cluster.storage.pieces(stripe)
                  for loc in cluster.storage.locations(cid)]
        assert len(places) == len(set(places)) == 4
    stored = sum(len(v) for d in devices for v in d.store.values())
    assert stored < 1.5 * len(data)  # two copies would be 2x


@pytest.mark.asyncio
async def test_a_lost_phone_is_rebuilt_from_parity(cluster: Any, make_device: Any) -> None:
    ec_settings(cluster)
    devices = await phones(make_device, 5)
    data = os.urandom(int(3.2 * MB))
    async with Running(devices):
        blob = await cluster.storage.put_bytes(data, "doc.bin", wait=True)
        assert blob["scheme"] == "ec" and (blob["ec_k"], blob["ec_m"]) == (4, 1)
        # one phone with a data piece is removed, and the staging cache is cleared
        victim = next(d for d in devices if d.store)
        assert cluster.scheduler.remove_device(victim.device_id)
        for path in cluster.storage.cache_dir.glob("*"):
            path.unlink()
        out = b"".join([block async for block in cluster.storage.stream(blob["id"])])
        assert out == data
        # every phone left already holds a piece: the lost one doubles up for now
        await cluster.storage.repair_once()
        assert cluster.storage.recompute(blob["id"]) == "degraded"
        assert all(holders(cluster, blob["id"]).values())
        # a new phone joins: the doubled-up piece spreads out to it
        (spare,) = await phones(make_device, 1, prefix="spare")
        async with Running([spare]):
            await cluster.storage.repair_once()
            assert cluster.storage.recompute(blob["id"]) == "ready"
            assert spare.store
    places = holders(cluster, blob["id"])
    assert all(len(names) == 1 and victim.name not in names for names in places.values())
    assert len({names[0] for names in places.values()}) == 5


@pytest.mark.asyncio
async def test_explicit_copies_and_too_few_phones(cluster: Any, client: Any, make_device: Any) -> None:
    ec_settings(cluster)
    devices = await phones(make_device, 4)
    async with Running(devices):
        blob = await cluster.storage.put_bytes(b"x" * 1000, "a.bin", replication=2, wait=True)
        assert blob["scheme"] == "copies" and blob["status"] == "ready"
    two = await phones(make_device, 0)
    assert two == []
    for device in devices[2:]:
        cluster.scheduler.remove_device(device.device_id)
    reply = await client.post("/api/storage", json={"file_id": cluster.files.add_bytes(b"y", "y")["id"],
                                                    "scheme": "ec"})
    assert reply.status_code == 400 and "3 online storage devices" in reply.json()["error"]
    layout = cluster.storage.choose_scheme(None, None)
    assert layout["scheme"] == "copies"  # auto with two phones: plain copies


@pytest.mark.asyncio
async def test_scrubbing_finds_a_damaged_piece_and_rebuilds_it(cluster: Any, make_device: Any) -> None:
    ec_settings(cluster)
    cluster.config.set("storage.scrub_hours", 0.0001)
    devices = await phones(make_device, 4)
    data = os.urandom(int(2.5 * MB))
    async with Running(devices):
        blob = await cluster.storage.put_bytes(data, "photos.bin", wait=True)
        damaged = next(d for d in devices if d.store)
        chunk_id = next(iter(damaged.store))
        damaged.store[chunk_id] = b"bit rot" + damaged.store[chunk_id][7:]
        cluster.db.execute("UPDATE chunk_locations SET stored_at=0")
        assert cluster.storage.scrub_once() == 4
        await wait_for(lambda: any(e["kind"] == "blob_scrubbed" for e in cluster.db.select("events")),
                       timeout=10)
        # rebuilt from the other three pieces and stored again, correct this time
        await wait_for(lambda: hashlib.sha256(damaged.store.get(chunk_id, b"")).hexdigest() == chunk_id,
                       timeout=15)
        await wait_for(lambda: cluster.storage.recompute(blob["id"]) == "ready", timeout=15)
        assert all(holders(cluster, blob["id"]).values())
        checked = cluster.db.scalar("SELECT COUNT(*) FROM chunk_locations WHERE checked_at IS NOT NULL")
        assert checked >= 3
        out = b"".join([block async for block in cluster.storage.stream(blob["id"])])
    assert out == data


@pytest.mark.asyncio
async def test_deleting_an_erasure_coded_blob_removes_parity_too(cluster: Any, make_device: Any) -> None:
    ec_settings(cluster)
    devices = await phones(make_device, 3)
    async with Running(devices):
        blob = await cluster.storage.put_bytes(os.urandom(MB + 10), "z.bin", wait=True)
        assert blob["scheme"] == "ec"
        assert await cluster.storage.delete(blob["id"])
        await wait_for(lambda: not any(d.store for d in devices), timeout=10)
    assert cluster.db.scalar("SELECT COUNT(*) FROM blob_parity") == 0
    assert cluster.db.scalar("SELECT COUNT(*) FROM chunk_locations") == 0
