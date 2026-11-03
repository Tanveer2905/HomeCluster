"""Phone-to-phone transfers (SPEC §8.3).

When several phones need the same big input file (a video, a model, a data
set), each one downloading it from the PC makes the PC's Wi-Fi the
bottleneck.  Instead, a phone that already has the file sends it on:

1. The coordinator remembers who has which file: a device that finished a
   task with a whole-file input downloaded it into its cache.
2. A task whose big input another online device holds is sent with
   ``peer_inputs``.  The worker then asks ``POST /api/w/peer/fetch``.
3. The coordinator picks up to ``MAX_SOURCES`` holders and sends each a
   ``peer_serve`` command with a random one-time token.  The holder opens a
   port on its LAN address *only now*, serves only that file to whoever shows
   that token, and closes the port when the transfer ends or after
   ``TTL_SECONDS`` without a request.  The port speaks HTTPS with the
   device's own certificate (issued by the cluster CA when it joined), so
   the file is encrypted on the Wi-Fi too.
4. The fetcher downloads the file in pieces from every source at once, the
   coordinator included, like BitTorrent.  Each piece is checked against the
   SHA-256 the coordinator computed from its own copy, so a phone that sends
   wrong bytes is caught and that piece comes from the coordinator instead.
   The whole file is checked against its id (its own SHA-256) at the end.

Browser workers never serve (a web page cannot open a port) and keep
downloading from the coordinator.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from typing import Any, Dict, List, Optional

from .db import now

log = logging.getLogger("coordinator.peers")

PIECE_BYTES = 4 * 1024 * 1024
MAX_SOURCES = 3
READY_TIMEOUT = 5.0
TTL_SECONDS = 60
#: a device is assumed to still hold a file it fetched this recently
HOLD_HOURS = 6.0
#: remembered piece hash lists (whole-file reads are not cheap)
HASH_CACHE = 32


class PeerTransfers:
    def __init__(self, state: Any) -> None:
        self.state = state
        #: file id -> {device id: when it was known to hold it}
        self.holdings: Dict[str, Dict[str, float]] = {}
        #: offer id -> offer dict (waiting for / serving a fetcher)
        self.offers: Dict[str, Dict[str, Any]] = {}
        self._ready: Dict[str, asyncio.Future] = {}
        self._hashes: Dict[str, List[str]] = {}
        self.totals = {"fetches": 0, "peer_bytes": 0, "bad_pieces": 0, "fallbacks": 0}

    # ------------------------------------------------------------- policy
    def enabled(self) -> bool:
        return bool(self.state.setting("transfers.peer", True))

    def min_bytes(self) -> int:
        return int(float(self.state.setting("transfers.min_mb", 4) or 0) * 1024 * 1024)

    # ----------------------------------------------------------- holdings
    def note(self, device_id: str, file_ids: List[Any]) -> None:
        stamp = now()
        for entry in file_ids or []:
            if isinstance(entry, str) and entry:
                self.holdings.setdefault(entry, {})[str(device_id)] = stamp

    def forget(self, device_id: str, file_id: Optional[str] = None) -> None:
        targets = [file_id] if file_id else list(self.holdings)
        for fid in targets:
            self.holdings.get(fid or "", {}).pop(str(device_id), None)

    def holders(self, file_id: str, exclude: Optional[str] = None) -> List[Dict[str, Any]]:
        """Online devices that hold ``file_id`` and may send it now."""
        cutoff = now() - HOLD_HOURS * 3600
        known = {d: t for d, t in (self.holdings.get(file_id) or {}).items() if t >= cutoff}
        self.holdings[file_id] = known
        out = []
        for device_id in known:
            if device_id == exclude:
                continue
            device = self.state.db.get("devices", device_id)
            if not device or device.get("removed") or device.get("status") != "online" \
                    or device.get("paused") or "peer" not in (device.get("caps") or []):
                continue
            if not self.state.safety.check(device)["jobs"] or self.state.scheduler.held(device_id):
                continue  # warm, low battery or answering a request: leave it alone
            out.append(device)
        busy = {}
        for offer in self.offers.values():
            busy[offer["device_id"]] = busy.get(offer["device_id"], 0) + 1
        out.sort(key=lambda d: (busy.get(d["id"], 0), -(float(d.get("bench_score") or 0))))
        return out

    def peer_inputs(self, payload: Dict[str, Any], device_id: str) -> List[str]:
        """Whole-file inputs of a task that are big and held by another device."""
        if not self.enabled():
            return []
        out = []
        for entry in payload.get("inputs") or []:
            if not isinstance(entry, str):
                continue  # byte ranges come from the coordinator
            meta = self.state.files.meta(entry)
            if meta is None or int(meta.get("size") or 0) < max(1, self.min_bytes()):
                continue
            if self.holders(entry, exclude=device_id):
                out.append(entry)
        return out

    # ------------------------------------------------------------ hashing
    def piece_hashes(self, file_id: str) -> List[str]:
        cached = self._hashes.get(file_id)
        if cached is not None:
            return cached
        out: List[str] = []
        with self.state.files.open(file_id) as handle:
            while True:
                block = handle.read(PIECE_BYTES)
                if not block:
                    break
                out.append(hashlib.sha256(block).hexdigest())
        if len(self._hashes) >= HASH_CACHE:
            self._hashes.pop(next(iter(self._hashes)))
        self._hashes[file_id] = out
        return out

    # ------------------------------------------------------------ fetching
    async def fetch(self, fetcher: Dict[str, Any], file_id: str) -> Dict[str, Any]:
        """Where ``fetcher`` can get ``file_id`` from: peers that opened a
        port for it (each with its own token) plus the coordinator."""
        meta = self.state.files.meta(file_id)
        if meta is None:
            return {"file_id": file_id, "sources": [], "error": "no such file"}
        size = int(meta.get("size") or 0)
        answer: Dict[str, Any] = {"file_id": file_id, "size": size, "piece_bytes": PIECE_BYTES,
                                  "sources": []}
        if not self.enabled() or size < max(1, self.min_bytes()):
            return answer
        holders = self.holders(file_id, exclude=fetcher["id"])[:MAX_SOURCES]
        if not holders:
            return answer
        answer["pieces"] = await asyncio.to_thread(self.piece_hashes, file_id)
        loop = asyncio.get_running_loop()
        waiting = []
        for device in holders:
            offer_id = secrets.token_hex(8)
            token = secrets.token_urlsafe(18)
            self.offers[offer_id] = {"id": offer_id, "device_id": device["id"], "fetcher": fetcher["id"],
                                     "file_id": file_id, "token": token, "since": now(), "ready": False}
            future: asyncio.Future = loop.create_future()
            self._ready[offer_id] = future
            self.state.send_command(device["id"], {"cmd": "peer_serve", "offer_id": offer_id,
                                                   "file_id": file_id, "token": token,
                                                   "ttl": TTL_SECONDS, "size": size})
            waiting.append((offer_id, device, future))
        for offer_id, device, future in waiting:
            try:
                ready = await asyncio.wait_for(future, timeout=READY_TIMEOUT)
            except asyncio.TimeoutError:
                ready = None
            finally:
                self._ready.pop(offer_id, None)
            if not ready or not ready.get("ok") or not ready.get("port"):
                self.offers.pop(offer_id, None)
                if ready and ready.get("error") == "not here":
                    self.forget(device["id"], file_id)
                continue
            host = ready.get("host") or (device.get("info") or {}).get("lan_ip")
            if not host:
                self._close(offer_id)
                continue
            self.offers[offer_id]["ready"] = True
            answer["sources"].append({
                "offer_id": offer_id, "device_id": device["id"], "name": device.get("name"),
                "url": "https://%s:%d/f/%s" % (host, int(ready["port"]), file_id),
                "token": self.offers[offer_id]["token"],
            })
        self.totals["fetches"] += 1
        if not answer["sources"]:
            self.totals["fallbacks"] += 1
        return answer

    def on_ready(self, device_id: str, body: Dict[str, Any]) -> bool:
        offer = self.offers.get(str(body.get("offer_id")))
        if offer is None or offer["device_id"] != device_id:
            return False
        future = self._ready.get(offer["id"])
        if future is not None and not future.done():
            future.set_result(body)
        return True

    def on_result(self, fetcher_id: str, body: Dict[str, Any]) -> bool:
        """The fetcher is done with one source: close its port, learn from it."""
        offer = self.offers.get(str(body.get("offer_id")))
        if offer is None or offer["fetcher"] != fetcher_id:
            return False
        self._close(offer["id"])
        sender = self.state.db.get("devices", offer["device_id"]) or {}
        if body.get("ok"):
            self.totals["peer_bytes"] += int(body.get("bytes") or 0)
            self.note(fetcher_id, [offer["file_id"]])
        else:
            error = str(body.get("error") or "failed")
            if "hash" in error:
                self.totals["bad_pieces"] += 1
                self.state.event("wrong_answer", "%s sent damaged pieces of a file; they came from the "
                                 "coordinator instead" % sender.get("name"), device_id=offer["device_id"])
            else:
                log.info("peer transfer from %s failed: %s", sender.get("name"), error)
        return True

    def fetched_inputs(self, device: Dict[str, Any], payload: Dict[str, Any]) -> None:
        """A task finished on ``device``: its big whole-file inputs are in its cache now."""
        if "peer" not in (device.get("caps") or []) or not self.enabled():
            return
        floor = max(1, self.min_bytes())
        for entry in payload.get("inputs") or []:
            if not isinstance(entry, str):
                continue
            meta = self.state.files.meta(entry)
            if meta is not None and int(meta.get("size") or 0) >= floor:
                self.note(device["id"], [entry])

    def _close(self, offer_id: str) -> None:
        offer = self.offers.pop(offer_id, None)
        if offer is not None:
            self.state.send_command(offer["device_id"], {"cmd": "peer_close", "offer_id": offer_id})

    def expire(self) -> int:
        """Offers whose fetcher never reported back: the sender closes by itself
        after its TTL anyway; forget them here too."""
        stamp = now()
        stale = [o for o, offer in self.offers.items() if stamp - offer["since"] > TTL_SECONDS * 10]
        for offer_id in stale:
            self.offers.pop(offer_id, None)
        return len(stale)

    def drop_device(self, device_id: str) -> None:
        self.forget(device_id)
        for offer_id, offer in list(self.offers.items()):
            if offer["device_id"] == device_id:
                self.offers.pop(offer_id, None)

    def stats(self) -> Dict[str, Any]:
        files = sum(1 for holders in self.holdings.values() if holders)
        return dict(self.totals, enabled=self.enabled(), open_offers=len(self.offers),
                    files_known=files)
