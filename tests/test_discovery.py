"""LAN auto-discovery (coordinator UDP responder + worker probe)."""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any

import pytest

from conftest import free_port
from coordinator.discovery import DiscoveryService, local_ip_for

pytestmark = pytest.mark.asyncio


def probe(port: int, timeout: float = 2.0) -> Any:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        sock.sendto(b"PHONECLUSTER_DISCOVER v1", ("127.0.0.1", port))
        data, _ = sock.recvfrom(4096)
        return json.loads(data.decode("utf-8"))
    finally:
        sock.close()


async def test_responder_answers_with_a_url_for_the_probing_interface(state: Any) -> None:
    port = free_port()
    state.config.set("discovery_port", port)
    state.port = 8799
    service = DiscoveryService(state)
    await service.start()
    try:
        reply = await asyncio.get_running_loop().run_in_executor(None, probe, port)
    finally:
        await service.stop()
    assert reply["phonecluster"] == 1
    assert reply["port"] == 8799
    assert reply["url"] == "https://127.0.0.1:8799"  # the interface the probe came in on
    assert isinstance(reply["urls"], list)
    assert "pin" not in reply and state.config.pin not in json.dumps(reply)
    assert service.answered == 1


async def test_responder_ignores_garbage(state: Any) -> None:
    port = free_port()
    state.config.set("discovery_port", port)
    service = DiscoveryService(state)
    await service.start()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        sock.sendto(b"hello?", ("127.0.0.1", port))
        with pytest.raises(socket.timeout):
            sock.recvfrom(1024)
        sock.close()
    finally:
        await service.stop()
    assert service.answered == 0


async def test_responder_can_be_disabled(state: Any) -> None:
    state.config.set("discovery", False)
    service = DiscoveryService(state)
    await service.start()
    assert service.transport is None
    await service.stop()


async def test_responder_survives_a_busy_port(state: Any) -> None:
    port = free_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    blocker.bind(("0.0.0.0", port))
    try:
        state.config.set("discovery_port", port)
        service = DiscoveryService(state)
        await service.start()  # logs a warning, does not raise
        assert service.transport is None
        await service.stop()
    finally:
        blocker.close()


async def test_local_ip_for_loopback() -> None:
    assert local_ip_for("127.0.0.1") == "127.0.0.1"


async def test_worker_probe_function_finds_the_responder(state: Any) -> None:
    """The stdlib probe shipped in worker.py talks to the real responder."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "pc_worker_disc", Path(__file__).resolve().parents[1] / "worker" / "worker.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]

    port = free_port()
    state.config.set("discovery_port", port)
    state.port = 8801
    service = DiscoveryService(state)
    await service.start()
    try:
        url = await asyncio.get_running_loop().run_in_executor(
            None, lambda: module.discover(timeout=2.0, port=port, hosts=["127.0.0.1"]))
    finally:
        await service.stop()
    assert url == "https://127.0.0.1:8801"
    # nothing listening -> None, quickly
    assert module.discover(timeout=0.3, port=free_port(), hosts=["127.0.0.1"]) is None
