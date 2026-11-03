"""Pooled storage: blobs chunked out onto phones (SPEC §10).

A blob is split into ``storage.chunk_mb`` MiB chunks.  Every chunk is written to
``data/storage/cache/<chunk_id>`` (an LRU cache, also the staging area the
workers download from) and pushed to devices that report the ``storage``
capability, in one of two ways:

* **copies** -- each chunk on ``replication`` devices (2 by default);
* **erasure coding** (``ec``, automatic from three storage devices on) --
  every ``k`` chunks form a *stripe* that gets ``m`` parity chunks
  (:mod:`coordinator.erasure`), and the ``k + m`` pieces go to ``k + m``
  different devices.  Any ``k`` pieces rebuild the stripe, so with ``m = 1``
  losing one phone loses nothing -- the safety of two copies for
  ``(k + 1) / k`` of the space instead of 2x.

Silent corruption: every stored piece is re-hashed on its phone once per
``storage.scrub_hours`` (a piece's id is its SHA-256, so the phone checks it
without sending anything); a damaged or missing piece is rebuilt from the
others and stored again.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import secrets
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional, Set, Tuple

from . import erasure, policy
from .db import Database, new_id, now

log = logging.getLogger("coordinator.storage")

ACK_TIMEOUT = 60.0
PULL_TIMEOUT = 30.0
REPAIR_INTERVAL = 60.0
READ_BLOCK = 1024 * 1024
#: pieces a device re-hashes per repair round (16 x 4 MiB = 64 MiB a minute)
SCRUB_BATCH = 16

#: Platforms that count as "the PC" for ``storage.include_pc`` (SPEC §10).
PC_PLATFORMS = ("windows", "linux")


class StorageError(Exception):
    pass


class StorageManager:
    """Chunk placement, retrieval and repair."""

    def __init__(self, state: Any) -> None:
        self.state = state
        self.db: Database = state.db
        self.cache_dir: Path = Path(state.data) / "storage" / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.log = logging.getLogger("coordinator.storage")
        self._acks: Dict[Tuple[str, str], asyncio.Future] = {}
        self._pulls: Dict[str, asyncio.Future] = {}
        self._tasks: List[asyncio.Task] = []
        self._repair: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._stop = asyncio.Event()
        self._repair = asyncio.create_task(self._repair_loop(), name="storage-repair")

    async def stop(self) -> None:
        self._stop.set()
        for task in [self._repair] + list(self._tasks):
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        self._repair = None
        for future in list(self._acks.values()) + list(self._pulls.values()):
            if not future.done():
                future.cancel()
        self._acks.clear()
        self._pulls.clear()

    async def _repair_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=REPAIR_INTERVAL)
                return
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:  # pragma: no cover
                return
            try:
                await self.repair_once()
                self.scrub_once()
            except asyncio.CancelledError:  # pragma: no cover
                return
            except Exception:
                self.log.exception("storage repair failed")

    def _spawn(self, coro: Any, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._tasks.append(task)
        task.add_done_callback(lambda t: self._tasks.remove(t) if t in self._tasks else None)
        return task

    # ---------------------------------------------------------------- cache
    def chunk_path(self, chunk_id: str) -> Path:
        safe = "".join(ch for ch in str(chunk_id) if ch in "0123456789abcdef")
        if len(safe) != 64:
            raise StorageError("bad chunk id")
        return self.cache_dir / safe

    def cached(self, chunk_id: str) -> Optional[Path]:
        try:
            path = self.chunk_path(chunk_id)
        except StorageError:
            return None
        if path.is_file():
            with contextlib.suppress(OSError):
                os.utime(path, None)
            return path
        return None

    def write_cache(self, chunk_id: str, data: bytes) -> Path:
        path = self.chunk_path(chunk_id)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return path

    def cache_bytes(self) -> int:
        total = 0
        for path in self.cache_dir.glob("*"):
            with contextlib.suppress(OSError):
                total += path.stat().st_size
        return total

    def enforce_cache(self) -> int:
        """LRU-trim the cache to ``storage.cache_mb``; never drop the last copy."""
        cap = int(self.state.setting("storage.cache_mb", 512) or 512) * 1024 * 1024
        entries: List[Tuple[float, int, Path]] = []
        total = 0
        for path in self.cache_dir.glob("*"):
            if path.suffix == ".part":
                continue
            try:
                stat = path.stat()
            except OSError:  # pragma: no cover
                continue
            entries.append((stat.st_atime, stat.st_size, path))
            total += stat.st_size
        if total <= cap:
            return 0
        entries.sort(key=lambda e: e[0])
        freed = 0
        for _atime, size, path in entries:
            if total - freed <= cap:
                break
            holders = int(
                self.db.scalar(
                    "SELECT COUNT(*) FROM chunk_locations WHERE chunk_id=?",
                    (path.name,),
                    default=0,
                )
            )
            if holders <= 0:
                continue  # the cache is the only copy
            with contextlib.suppress(OSError):
                path.unlink()
                freed += size
        return freed

    # ------------------------------------------------------------- devices
    def candidates(self, exclude: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
        """Online devices with the ``storage`` cap, most free space first.

        The PC worker is only used when ``storage.include_pc`` is set, or as a
        fallback when no phone can take the chunk (see docs/DEVIATIONS.md).
        """
        skip = {str(x) for x in (exclude or ())}
        rows = [
            d
            for d in self.db.select(
                "devices", "WHERE removed=0 AND status='online' AND paused=0"
            )
            if "storage" in (d.get("caps") or []) and d["id"] not in skip
            and policy.role_on(d, "storage")
        ]
        include_pc = bool(self.state.setting("storage.include_pc", False))

        def free(device: Dict[str, Any]) -> int:
            value = device.get("storage_free_mb")
            return int(value) if value is not None else 0

        if include_pc:
            return sorted(rows, key=free, reverse=True)
        phones = sorted(
            [d for d in rows if d.get("platform") not in PC_PLATFORMS], key=free, reverse=True
        )
        if phones:
            return phones
        # Fallback (documented in docs/DEVIATIONS.md): with no phone in the pool
        # the PC worker is used, otherwise pooled storage could never work on a
        # one-machine cluster.
        return sorted(
            [d for d in rows if d.get("platform") in PC_PLATFORMS], key=free, reverse=True
        )

    def locations(self, chunk_id: str) -> List[Dict[str, Any]]:
        rows = self.db.rows(
            "SELECT l.device_id, l.stored_at, d.name, d.status, d.removed "
            "FROM chunk_locations l LEFT JOIN devices d ON d.id = l.device_id "
            "WHERE l.chunk_id=?",
            (chunk_id,),
        )
        return [dict(row) for row in rows]

    def online_locations(self, chunk_id: str) -> List[str]:
        return [
            str(row["device_id"])
            for row in self.locations(chunk_id)
            if row.get("status") == "online" and not row.get("removed")
        ]

    # ---------------------------------------------------------------- write
    def _chunk_file(self, path: Path, chunk_size: int) -> List[Tuple[str, int]]:
        chunks: List[Tuple[str, int]] = []
        with path.open("rb") as handle:
            while True:
                block = handle.read(chunk_size)
                if not block:
                    break
                chunk_id = hashlib.sha256(block).hexdigest()
                self.write_cache(chunk_id, block)
                chunks.append((chunk_id, len(block)))
        return chunks

    def choose_scheme(self, replication: Optional[int], scheme: Optional[str]) -> Dict[str, Any]:
        """Copies or erasure coding for a new blob.

        ``auto`` (the default) codes once three storage devices are online,
        unless a number of copies was asked for explicitly.
        """
        wanted = str(scheme or self.state.setting("storage.scheme", "auto") or "auto").lower()
        if wanted not in ("auto", "copies", "ec"):
            raise StorageError("scheme must be auto, copies or ec")
        copies = max(1, int(replication if replication is not None
                            else self.state.setting("storage.replication", 2) or 2))
        if wanted == "copies" or (wanted == "auto" and replication is not None):
            return {"scheme": "copies", "replication": copies, "k": 0, "m": 0}
        layout = erasure.shape(len(self.candidates()))
        if not layout:
            if wanted == "ec":
                raise StorageError("erasure coding needs at least 3 online storage devices")
            return {"scheme": "copies", "replication": copies, "k": 0, "m": 0}
        return {"scheme": "ec", "replication": 1, "k": layout["k"], "m": layout["m"]}

    async def put_path(
        self,
        path: Path,
        name: str,
        replication: Optional[int] = None,
        *,
        wait: bool = False,
        scheme: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Split a local file into chunks and start pushing them to devices."""
        path = Path(path)
        size = path.stat().st_size
        chunk_mb = int(self.state.setting("storage.chunk_mb", 4) or 4)
        chunk_size = max(1, chunk_mb) * 1024 * 1024
        layout = self.choose_scheme(replication, scheme)

        chunks = await asyncio.to_thread(self._chunk_file, path, chunk_size)
        blob_id = new_id()
        self.db.insert(
            "blobs",
            {
                "id": blob_id,
                "name": name,
                "size": size,
                "chunk_size": chunk_size,
                "n_chunks": len(chunks),
                "replication": layout["replication"],
                "scheme": layout["scheme"],
                "ec_k": layout["k"],
                "ec_m": layout["m"],
                "status": "storing",
                "created": now(),
            },
        )
        for idx, (chunk_id, chunk_len) in enumerate(chunks):
            self.db.execute(
                "INSERT OR REPLACE INTO blob_chunks (blob_id, idx, chunk_id, size) "
                "VALUES (?,?,?,?)",
                (blob_id, idx, chunk_id, chunk_len),
            )
        if layout["scheme"] == "ec":
            await asyncio.to_thread(self._make_parity, blob_id)
            self.log.info("blob %s: %d chunk(s), erasure coded %d+%d", blob_id, len(chunks),
                          layout["k"], layout["m"])
        else:
            self.log.info("blob %s: %d chunk(s), %d copies", blob_id, len(chunks),
                          layout["replication"])
        if wait:
            await self.place_blob(blob_id)
        else:
            self._spawn(self.place_blob(blob_id), "place-%s" % blob_id)
        return self.db.get("blobs", blob_id) or {"id": blob_id}

    async def put_bytes(
        self, data: bytes, name: str, replication: Optional[int] = None, *, wait: bool = False,
        scheme: Optional[str] = None,
    ) -> Dict[str, Any]:
        tmp = Path(self.state.data) / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        staged = tmp / ("blob-%s.bin" % secrets.token_hex(6))
        staged.write_bytes(data)
        try:
            return await self.put_path(staged, name, replication, wait=wait, scheme=scheme)
        finally:
            with contextlib.suppress(OSError):
                staged.unlink()

    # ------------------------------------------------------ erasure stripes
    def stripes(self, blob: Dict[str, Any]) -> List[Dict[str, Any]]:
        """The stripes of an erasure-coded blob: data and parity pieces each."""
        k = int(blob.get("ec_k") or 0)
        if blob.get("scheme") != "ec" or k < 1:
            return []
        data = [dict(r) for r in self.db.rows(
            "SELECT idx, chunk_id, size FROM blob_chunks WHERE blob_id=? ORDER BY idx", (blob["id"],))]
        parity: Dict[int, List[Dict[str, Any]]] = {}
        for row in self.db.rows("SELECT stripe, j, chunk_id, size FROM blob_parity WHERE blob_id=? "
                                "ORDER BY stripe, j", (blob["id"],)):
            parity.setdefault(int(row["stripe"]), []).append(dict(row))
        out = []
        for stripe in range((len(data) + k - 1) // k):
            members = data[stripe * k:(stripe + 1) * k]
            out.append({"stripe": stripe, "k": k, "m": int(blob.get("ec_m") or 0),
                        "data": members, "parity": parity.get(stripe, []),
                        "lengths": [int(d["size"]) for d in members] + [0] * (k - len(members))})
        return out

    @staticmethod
    def pieces(stripe: Dict[str, Any]) -> List[Tuple[int, str, int]]:
        """``(shard index, chunk id, size)``: data 0..k-1, parity k..k+m-1."""
        out = [(i, str(d["chunk_id"]), int(d["size"])) for i, d in enumerate(stripe["data"])]
        out += [(stripe["k"] + int(p["j"]), str(p["chunk_id"]), int(p["size"])) for p in stripe["parity"]]
        return out

    def _make_parity(self, blob_id: str) -> int:
        """Compute the parity pieces of every stripe (data is in the cache)."""
        blob = self.db.get("blobs", blob_id) or {}
        made = 0
        for stripe in self.stripes(blob):
            if stripe["parity"]:
                continue
            data = []
            for member in stripe["data"]:
                path = self.cached(str(member["chunk_id"]))
                if path is None:
                    raise StorageError("chunk %s is not staged" % str(member["chunk_id"])[:12])
                data.append(path.read_bytes())
            data += [b""] * (stripe["k"] - len(data))
            for j, piece in enumerate(erasure.encode(data, stripe["m"])):
                chunk_id = hashlib.sha256(piece).hexdigest()
                self.write_cache(chunk_id, piece)
                self.db.execute(
                    "INSERT OR REPLACE INTO blob_parity (blob_id, stripe, j, chunk_id, size) "
                    "VALUES (?,?,?,?,?)", (blob_id, stripe["stripe"], j, chunk_id, len(piece)))
                made += 1
        return made

    async def place_stripe(self, stripe: Dict[str, Any]) -> int:
        """Every piece of the stripe on its own device; returns pieces placed now.

        A missing piece goes to a device that holds nothing of this stripe.
        When every device already does, it goes next to another piece (still
        safer than no piece: only that one device's loss would then hurt) and
        the blob shows as degraded until a new device lets it spread again.
        """
        pieces = self.pieces(stripe)
        holders: Dict[str, Set[str]] = {cid: {r["device_id"] for r in self.locations(cid)}
                                        for _, cid, _ in pieces}
        used: Set[str] = set().union(*holders.values()) if holders else set()
        missing = [(i, cid, size) for i, cid, size in pieces if not holders[cid]]
        if any(self.cached(cid) is None for _, cid, _ in missing):
            await self.rebuild(stripe, [i for i, cid, _ in missing if self.cached(cid) is None])
        candidates = self.candidates(exclude=used)
        # rotate by stripe so pieces spread over every device, not just the emptiest
        if candidates:
            shift = stripe["stripe"] % len(candidates)
            candidates = candidates[shift:] + candidates[:shift]
        placed = 0
        for _index, chunk_id, size in missing:
            if self.cached(chunk_id) is None:
                continue
            done = False
            while candidates and not done:
                device = candidates.pop(0)
                done = await self.send_chunk_to(device["id"], chunk_id, size)
            if not done:
                load: Dict[str, int] = {}
                for devices in holders.values():
                    for device_id in devices:
                        load[device_id] = load.get(device_id, 0) + 1
                for device in sorted(self.candidates(), key=lambda d: load.get(d["id"], 0)):
                    if await self.send_chunk_to(device["id"], chunk_id, size):
                        done = True
                        break
            if done:
                placed += 1
                holders[chunk_id] = {r["device_id"] for r in self.locations(chunk_id)}
        await self._spread(stripe, holders, candidates)
        return placed

    async def _spread(self, stripe: Dict[str, Any], holders: Dict[str, Set[str]],
                      free: List[Dict[str, Any]]) -> int:
        """Move doubled-up pieces to devices that hold nothing of the stripe."""
        load: Dict[str, int] = {}
        for devices in holders.values():
            for device_id in devices:
                load[device_id] = load.get(device_id, 0) + 1
        moved = 0
        for _index, chunk_id, size in self.pieces(stripe):
            for device_id in sorted(holders.get(chunk_id) or ()):
                if load.get(device_id, 0) <= 1 or not free:
                    continue
                if self.cached(chunk_id) is None and await self.fetch_chunk(chunk_id) is None:
                    continue
                target = free.pop(0)
                if not await self.send_chunk_to(target["id"], chunk_id, size):
                    continue
                self.db.execute("DELETE FROM chunk_locations WHERE chunk_id=? AND device_id=?",
                                (chunk_id, device_id))
                self.state.send_command(device_id, {"cmd": "del_chunk", "chunk_id": chunk_id})
                load[device_id] -= 1
                moved += 1
        return moved

    async def rebuild(self, stripe: Dict[str, Any], want: List[int]) -> Dict[int, bytes]:
        """Recreate pieces ``want`` of a stripe from any ``k`` others (pulled
        back from devices as needed) and stage them in the cache."""
        k, m = stripe["k"], stripe["m"]
        pieces = self.pieces(stripe)
        have: Dict[int, bytes] = {}
        for index, chunk_id, _size in sorted(pieces, key=lambda p: self.cached(p[1]) is None):
            if len(have) + sum(1 for n in stripe["lengths"] if n == 0) >= k:
                break
            if index in want:
                continue
            path = self.cached(chunk_id) or await self.fetch_chunk(chunk_id)
            if path is not None:
                have[index] = await asyncio.to_thread(path.read_bytes)
        out = await asyncio.to_thread(erasure.reconstruct, have, k, m, stripe["lengths"], want)
        by_index = {index: chunk_id for index, chunk_id, _ in pieces}
        for index, data in out.items():
            chunk_id = by_index.get(index)
            if chunk_id and hashlib.sha256(data).hexdigest() == chunk_id:
                self.write_cache(chunk_id, data)
        return out

    # ------------------------------------------------------------ placement
    async def place_blob(self, blob_id: str) -> Dict[str, Any]:
        blob = self.db.get("blobs", blob_id)
        if blob is None:
            return {"error": "unknown blob"}
        replication = int(blob.get("replication") or 1)
        if blob.get("scheme") == "ec":
            stripes = self.stripes(blob)
            if any(len(s["parity"]) < s["m"] for s in stripes):
                # parity not made yet (the data left the cache): pull the data back first
                for stripe in stripes:
                    if len(stripe["parity"]) < stripe["m"]:
                        for member in stripe["data"]:
                            await self.fetch_chunk(str(member["chunk_id"]))
                with contextlib.suppress(StorageError):
                    await asyncio.to_thread(self._make_parity, blob_id)
            for stripe in self.stripes(blob):
                try:
                    await self.place_stripe(stripe)
                except (StorageError, ValueError) as exc:
                    self.log.warning("blob %s stripe %d: %s", blob_id, stripe["stripe"], exc)
        else:
            rows = self.db.rows(
                "SELECT * FROM blob_chunks WHERE blob_id=? ORDER BY idx", (blob_id,)
            )
            for row in rows:
                await self.place_chunk(str(row["chunk_id"]), int(row["size"]), replication)
        status = self.recompute(blob_id)
        self.enforce_cache()
        if status == "ready":
            how = ("%d+%d erasure coded" % (blob.get("ec_k"), blob.get("ec_m"))
                   if blob.get("scheme") == "ec" else "x%d" % replication)
            self.state.event(
                "blob_stored",
                "%s stored (%d chunks %s)" % (blob.get("name"), blob.get("n_chunks"), how),
                data={"blob_id": blob_id},
            )
        elif status != "deleting":
            self.state.event(
                "blob_degraded",
                "%s is %s" % (blob.get("name"), status),
                data={"blob_id": blob_id, "status": status},
            )
        return self.db.get("blobs", blob_id) or {}

    async def place_chunk(self, chunk_id: str, size: int, replication: int) -> int:
        """Push a chunk until ``replication`` devices hold it.  Returns the count."""
        holders = {row["device_id"] for row in self.locations(chunk_id)}
        placed = len(holders)
        if placed >= replication:
            return placed
        if self.cached(chunk_id) is None and not self.online_locations(chunk_id):
            return placed
        if self.cached(chunk_id) is None:
            await self.fetch_chunk(chunk_id)
            if self.cached(chunk_id) is None:
                return placed
        for device in self.candidates(exclude=holders):
            if placed >= replication:
                break
            if await self.send_chunk_to(device["id"], chunk_id, size):
                placed += 1
                holders.add(device["id"])
        return placed

    async def send_chunk_to(self, device_id: str, chunk_id: str, size: int) -> bool:
        """``store_chunk`` + wait for the ack (SPEC §8)."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        key = (str(device_id), str(chunk_id))
        self._acks[key] = future
        self.state.send_command(
            device_id, {"cmd": "store_chunk", "chunk_id": chunk_id, "size": size}
        )
        try:
            ok = await asyncio.wait_for(future, timeout=ACK_TIMEOUT)
        except asyncio.TimeoutError:
            self.log.warning("chunk %s: %s did not ack in %.0fs", chunk_id[:12], device_id, ACK_TIMEOUT)
            return False
        except asyncio.CancelledError:  # pragma: no cover
            raise
        finally:
            self._acks.pop(key, None)
        if not ok:
            return False
        self.db.execute(
            "INSERT OR REPLACE INTO chunk_locations (chunk_id, device_id, stored_at) "
            "VALUES (?,?,?)",
            (chunk_id, device_id, now()),
        )
        return True

    def on_ack(self, device_id: str, chunk_id: str, ok: bool, error: Optional[str] = None) -> bool:
        future = self._acks.get((str(device_id), str(chunk_id)))
        if future is None or future.done():
            return False
        if not ok:
            self.log.warning("chunk %s rejected by %s: %s", str(chunk_id)[:12], device_id, error)
        future.set_result(bool(ok))
        return True

    # ----------------------------------------------------------------- read
    async def fetch_chunk(self, chunk_id: str) -> Optional[Path]:
        """Pull a chunk back from a device that holds it (SPEC §10)."""
        cached = self.cached(chunk_id)
        if cached is not None:
            return cached
        for device_id in self.online_locations(chunk_id):
            loop = asyncio.get_running_loop()
            req_id = secrets.token_hex(8)
            future: asyncio.Future = loop.create_future()
            self._pulls[req_id] = future
            self.state.send_command(
                device_id,
                {"cmd": "send_chunk", "chunk_id": chunk_id, "req_id": req_id},
            )
            try:
                data = await asyncio.wait_for(future, timeout=PULL_TIMEOUT)
            except asyncio.TimeoutError:
                self.log.warning(
                    "chunk %s: %s did not send it back in %.0fs",
                    str(chunk_id)[:12], device_id, PULL_TIMEOUT,
                )
                continue
            except asyncio.CancelledError:  # pragma: no cover
                raise
            finally:
                self._pulls.pop(req_id, None)
            if not data:
                continue
            if hashlib.sha256(data).hexdigest() != str(chunk_id):
                self.log.warning("chunk %s came back corrupted from %s", str(chunk_id)[:12], device_id)
                continue
            return self.write_cache(chunk_id, data)
        return None

    def on_chunk_upload(self, chunk_id: str, req_id: str, data: bytes) -> bool:
        future = self._pulls.get(str(req_id))
        if future is None or future.done():
            return False
        future.set_result(data)
        return True

    async def _read_cached(self, chunk_id: str) -> Optional[bytes]:
        """A cached chunk's bytes, or None if missing or damaged (then dropped)."""
        path = self.cached(chunk_id)
        if path is None:
            return None
        data = await asyncio.to_thread(path.read_bytes)
        if hashlib.sha256(data).hexdigest() != chunk_id:
            self.log.warning("cached chunk %s was damaged; fetching it again", chunk_id[:12])
            with contextlib.suppress(OSError):
                path.unlink()
            return None
        return data

    async def stream(self, blob_id: str) -> AsyncIterator[bytes]:
        """Yield the blob's bytes, pulling chunks back from devices as needed;
        an erasure-coded chunk whose phone is gone is rebuilt from the others."""
        blob = self.db.get("blobs", blob_id) or {}
        rows = self.db.rows(
            "SELECT * FROM blob_chunks WHERE blob_id=? ORDER BY idx", (blob_id,)
        )
        if not rows:
            raise StorageError("blob has no chunks")
        stripes = {s["stripe"]: s for s in self.stripes(blob)}
        k = int(blob.get("ec_k") or 0)
        for row in rows:
            chunk_id = str(row["chunk_id"])
            data = await self._read_cached(chunk_id)
            if data is None and await self.fetch_chunk(chunk_id) is not None:
                data = await self._read_cached(chunk_id)
            if data is None and stripes:
                stripe = stripes[int(row["idx"]) // k]
                try:
                    data = (await self.rebuild(stripe, [int(row["idx"]) % k]))[int(row["idx"]) % k]
                except ValueError:
                    data = None
            if data is None:
                raise StorageError("chunk %s is unavailable" % chunk_id[:12])
            yield data
        self.enforce_cache()

    # --------------------------------------------------------------- status
    def chunk_map(self, blob_id: str) -> List[Dict[str, Any]]:
        rows = self.db.rows(
            "SELECT * FROM blob_chunks WHERE blob_id=? ORDER BY idx", (blob_id,)
        )
        out: List[Dict[str, Any]] = []
        for row in rows:
            chunk_id = str(row["chunk_id"])
            locations = self.locations(chunk_id)
            out.append(
                {
                    "idx": int(row["idx"]),
                    "chunk_id": chunk_id,
                    "size": int(row["size"]),
                    "cached": self.chunk_path(chunk_id).is_file(),
                    "locations": [
                        {
                            "device_id": loc["device_id"],
                            "name": loc.get("name"),
                            "status": loc.get("status"),
                        }
                        for loc in locations
                    ],
                }
            )
        return out

    def parity_map(self, blob: Dict[str, Any]) -> List[Dict[str, Any]]:
        out = []
        for stripe in self.stripes(blob):
            for piece in stripe["parity"]:
                chunk_id = str(piece["chunk_id"])
                out.append({
                    "stripe": stripe["stripe"], "j": int(piece["j"]), "chunk_id": chunk_id,
                    "size": int(piece["size"]), "cached": self.chunk_path(chunk_id).is_file(),
                    "locations": [{"device_id": loc["device_id"], "name": loc.get("name"),
                                   "status": loc.get("status")} for loc in self.locations(chunk_id)],
                })
        return out

    def _ec_status(self, blob: Dict[str, Any]) -> str:
        """ready: every piece stored and every stripe readable; degraded: a
        piece is missing but every stripe still has ``k`` reachable pieces;
        lost: a stripe cannot be read right now."""
        stripes = self.stripes(blob)
        if not stripes:
            return "lost"
        status = "ready"
        for stripe in stripes:
            pieces = self.pieces(stripe)
            if len(stripe["parity"]) < stripe["m"]:
                status = "degraded"
            padding = sum(1 for n in stripe["lengths"] if n == 0)
            reachable = padding + sum(1 for _, cid, _ in pieces if self.online_locations(cid))
            if reachable < stripe["k"]:
                return "lost"
            places = [loc["device_id"] for _, cid, _ in pieces for loc in self.locations(cid)]
            if any(not self.locations(cid) for _, cid, _ in pieces) or len(places) != len(set(places)):
                status = "degraded"  # a piece is missing, or two share one device
        return status

    def recompute(self, blob_id: str) -> str:
        blob = self.db.get("blobs", blob_id)
        if blob is None:
            return "lost"
        if blob.get("status") == "deleting":
            return "deleting"
        replication = int(blob.get("replication") or 1)
        rows = self.db.rows(
            "SELECT chunk_id FROM blob_chunks WHERE blob_id=?", (blob_id,)
        )
        if not rows:
            status = "lost"
        elif blob.get("scheme") == "ec":
            status = self._ec_status(blob)
        else:
            status = "ready"
            for row in rows:
                chunk_id = str(row["chunk_id"])
                total = len(self.locations(chunk_id))
                online = len(self.online_locations(chunk_id))
                if online == 0:
                    status = "lost"
                    break
                if total < replication:
                    status = "degraded"
        if status != blob.get("status"):
            self.db.update("blobs", {"id": blob_id}, {"status": status})
        return status

    @staticmethod
    def describe(blob: Dict[str, Any]) -> Dict[str, Any]:
        """How a blob is protected, in words and as the space it takes."""
        if blob.get("scheme") == "ec":
            k, m = int(blob.get("ec_k") or 1), int(blob.get("ec_m") or 0)
            return {"scheme": "ec", "overhead": round((k + m) / float(k), 2),
                    "survives": m, "label": "%d+%d erasure coded" % (k, m)}
        copies = int(blob.get("replication") or 1)
        return {"scheme": "copies", "overhead": float(copies), "survives": copies - 1,
                "label": "%d cop%s" % (copies, "y" if copies == 1 else "ies")}

    def status(self, blob_id: str) -> Optional[Dict[str, Any]]:
        blob = self.db.get("blobs", blob_id)
        if blob is None:
            return None
        blob["status"] = self.recompute(blob_id)
        blob["chunks"] = self.chunk_map(blob_id)
        blob["protection"] = self.describe(blob)
        if blob.get("scheme") == "ec":
            blob["parity"] = self.parity_map(blob)
        return blob

    def list(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        blobs = self.db.select(
            "blobs", "ORDER BY created DESC LIMIT ? OFFSET ?", (limit, offset)
        )
        for blob in blobs:
            blob["status"] = self.recompute(blob["id"])
            blob["protection"] = self.describe(blob)
        return blobs

    # --------------------------------------------------------------- delete
    async def delete(self, blob_id: str) -> bool:
        blob = self.db.get("blobs", blob_id)
        if blob is None:
            return False
        self.db.update("blobs", {"id": blob_id}, {"status": "deleting"})
        rows = self.db.rows("SELECT chunk_id FROM blob_chunks WHERE blob_id=? UNION "
                            "SELECT chunk_id FROM blob_parity WHERE blob_id=?", (blob_id, blob_id))
        for row in rows:
            chunk_id = str(row["chunk_id"])
            shared = int(
                self.db.scalar(
                    "SELECT (SELECT COUNT(*) FROM blob_chunks WHERE chunk_id=? AND blob_id<>?) + "
                    "(SELECT COUNT(*) FROM blob_parity WHERE chunk_id=? AND blob_id<>?)",
                    (chunk_id, blob_id, chunk_id, blob_id),
                    default=0,
                )
            )
            if shared:
                continue
            for location in self.locations(chunk_id):
                self.state.send_command(
                    location["device_id"], {"cmd": "del_chunk", "chunk_id": chunk_id}
                )
            self.db.execute("DELETE FROM chunk_locations WHERE chunk_id=?", (chunk_id,))
            with contextlib.suppress(OSError, StorageError):
                self.chunk_path(chunk_id).unlink(missing_ok=True)
        self.db.execute("DELETE FROM blob_chunks WHERE blob_id=?", (blob_id,))
        self.db.execute("DELETE FROM blob_parity WHERE blob_id=?", (blob_id,))
        self.db.execute("DELETE FROM blobs WHERE id=?", (blob_id,))
        return True

    # --------------------------------------------------------------- repair
    async def repair_once(self) -> Dict[str, Any]:
        """Re-replicate degraded blobs and drop dead locations (SPEC §10)."""
        removed = self.db.execute(
            "DELETE FROM chunk_locations WHERE device_id IN "
            "(SELECT id FROM devices WHERE removed=1)"
        ).rowcount
        repaired: List[str] = []
        for blob in self.db.select("blobs", "ORDER BY created ASC"):
            if blob.get("status") == "deleting":
                await self.delete(blob["id"])
                continue
            status = self.recompute(blob["id"])
            if status == "ready":
                continue
            before = status
            await self.place_blob(blob["id"])
            after = self.recompute(blob["id"])
            if after != before:
                repaired.append(blob["id"])
        self.enforce_cache()
        return {"repaired": repaired, "stale_locations": removed}

    # -------------------------------------------------------------- scrubbing
    def scrub_once(self) -> int:
        """Ask each storage device to re-hash the pieces it has not checked
        for ``storage.scrub_hours`` (a few per round, so phones stay cool)."""
        hours = float(self.state.setting("storage.scrub_hours", 24) or 0)
        if hours <= 0:
            return 0
        due = now() - hours * 3600
        asked = 0
        for device in self.db.select("devices", "WHERE removed=0 AND status='online' AND paused=0"):
            if not self.state.safety.check(device)["jobs"] or self.state.scheduler.held(device["id"]):
                continue
            rows = self.db.rows(
                "SELECT chunk_id FROM chunk_locations WHERE device_id=? "
                "AND COALESCE(checked_at, stored_at) < ? ORDER BY COALESCE(checked_at, 0) ASC LIMIT ?",
                (device["id"], due, SCRUB_BATCH))
            if rows:
                self.state.send_command(device["id"], {"cmd": "scrub_chunks",
                                                       "chunk_ids": [str(r["chunk_id"]) for r in rows]})
                asked += 1
        return asked

    def on_scrub(self, device_id: str, checked: List[str], bad: List[str],
                 missing: List[str]) -> Dict[str, Any]:
        """A device reported which pieces hashed right; the rest are rebuilt."""
        stamp = now()
        for chunk_id in checked:
            self.db.execute("UPDATE chunk_locations SET checked_at=? WHERE chunk_id=? AND device_id=?",
                            (stamp, str(chunk_id), device_id))
        lost = sorted(set(bad) | set(missing))
        for chunk_id in lost:
            self.db.execute("DELETE FROM chunk_locations WHERE chunk_id=? AND device_id=?",
                            (str(chunk_id), device_id))
        if lost:
            name = (self.db.get("devices", device_id) or {}).get("name") or device_id
            self.state.event(
                "blob_scrubbed",
                "%s: %d damaged and %d missing stored piece(s); rebuilding them from the other phones"
                % (name, len(set(bad)), len(set(missing) - set(bad))),
                device_id=device_id, data={"bad": list(bad), "missing": list(missing)})
            if self._repair is not None:
                self._spawn(self.repair_once(), "scrub-repair")
        return {"ok": True, "checked": len(checked), "rebuilding": len(lost)}
