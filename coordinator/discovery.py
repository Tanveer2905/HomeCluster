"""LAN auto-discovery (SPEC §2.1).

The coordinator answers UDP probes so a worker (or the CLI) can find it
without anyone typing an IP address:

    client  -> broadcast  "PHONECLUSTER_DISCOVER v1"          to <port>/udp
    coordinator -> unicast {"phonecluster": 1, "url": "https://<ip>:8765",
                            "urls": [...], "port": 8765, "name": "<host>"}

The ``url`` is built from the interface the probe arrived on, so a phone on
the Wi-Fi gets the Wi-Fi address and a hotspot client the hotspot one.
No PIN is ever included.  Disable with ``discovery: false`` in config.yaml.
"""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import socket
from typing import Any, Optional, Tuple

log = logging.getLogger("coordinator.discovery")

MAGIC = b"PHONECLUSTER_DISCOVER"
DEFAULT_PORT = 8766


def local_ip_for(peer_ip: str) -> Optional[str]:
    """IP of the local interface that routes to ``peer_ip`` (no packets sent)."""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect((peer_ip, 1))
        ip = str(sock.getsockname()[0])
        return None if ip.startswith("0.") else ip
    except OSError:
        return None
    finally:
        if sock is not None:
            sock.close()


class _Responder(asyncio.DatagramProtocol):
    def __init__(self, state: Any) -> None:
        self.state = state
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.answered = 0

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: Tuple[str, int]) -> None:
        if not data.startswith(MAGIC) or self.transport is None:
            return
        port = int(self.state.port or self.state.config.port)
        best = local_ip_for(addr[0])
        urls = self.state.urls() or ["https://127.0.0.1:%d" % port]
        # HTTPS only; a worker checks the certificate against the cluster CA it
        # was given when it joined, so a fake reply leads nowhere (SPEC 2.1, 3)
        url = "https://%s:%d" % (best, port) if best else urls[0]
        reply = {
            "phonecluster": 1,
            "name": platform.node() or "coordinator",
            "port": port,
            "url": url,
            "urls": urls,
        }
        try:
            self.transport.sendto(json.dumps(reply).encode("utf-8"), addr)
            self.answered += 1
            log.debug("answered discovery probe from %s:%d with %s", addr[0], addr[1], url)
        except OSError as exc:  # pragma: no cover - transient socket error
            log.debug("discovery reply to %s failed: %s", addr, exc)

    def error_received(self, exc: Exception) -> None:  # pragma: no cover
        log.debug("discovery socket error: %s", exc)


class DiscoveryService:
    """Owns the UDP socket for the lifetime of the app."""

    def __init__(self, state: Any) -> None:
        self.state = state
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.protocol: Optional[_Responder] = None

    @property
    def port(self) -> int:
        return int(self.state.setting("discovery_port", DEFAULT_PORT) or DEFAULT_PORT)

    @property
    def enabled(self) -> bool:
        return bool(self.state.setting("discovery", True))

    async def start(self) -> None:
        if not self.enabled:
            log.info("discovery disabled by config")
            return
        loop = asyncio.get_running_loop()
        try:
            self.transport, self.protocol = await loop.create_datagram_endpoint(
                lambda: _Responder(self.state),
                local_addr=("0.0.0.0", self.port),
                allow_broadcast=True,
                reuse_port=False,
            )
        except OSError as exc:
            log.warning("discovery responder could not bind UDP %d: %s", self.port, exc)
            self.transport = None
            return
        log.info("discovery responder listening on UDP %d", self.port)

    async def stop(self) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None

    @property
    def answered(self) -> int:
        return self.protocol.answered if self.protocol else 0
