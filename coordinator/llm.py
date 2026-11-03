"""Distributed LLMs: models, placement and deployments (SPEC 6.8).

A *deployment* runs one GGUF model on one or more devices:

  * **single**: the whole model runs on one device (``llm_head`` only).  On a
    PC with a supported graphics card the layers that fit in the user's GPU
    share go to the card, the rest stay on the CPU.  No network in the way.
  * **split**: every holding device runs an ``llm_shard`` service (llama.cpp
    ``ggml-rpc-server`` on its LAN address) with a share of the layers; one
    *host* runs the ``llm_head`` (``llama-server --rpc``) that owns the model
    file, drives the shards and answers chats.  A PC shard can offer its CPU
    and its GPU as two separate parts.

Placement (SPEC 6.8.1):

  * **auto**: a model that fits on one device runs there alone, on the device
    that answers fastest; a bigger one is split over the fastest set of
    devices that holds it.  The user may narrow the candidates.
  * **manual**: the user picks the devices, the host and optional weights.

Only devices whose owner left "AI models" on, that are not switched off and
that pass the safety checks (``policy.py``) are used.  When a device holding
part of an auto-placed model goes away, gets too hot or is switched off, the
model is placed again on what is left; a manual one waits for the device.
A graphics card that runs hot hands its layers back to the CPUs.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import policy
from .db import new_id, now
from .gguf import summarize

log = logging.getLogger("coordinator.llm")

ACTIVE = ("starting", "loading", "ready", "degraded")
#: unloaded after sitting unused; the next call loads it again
IDLE = "idle"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
#: names that reach the default model
ALIASES = ("chat", "llm")
#: a model that failed this recently is not started again by a call
FAILED_COOLDOWN = 60.0
MB = 1024 * 1024
#: the host's own scratch buffer next to the token embeddings
HOST_COMPUTE_MB = 32
#: added to the estimated per-device compute buffer
COMPUTE_MARGIN_MB = 8
#: weights a part can stream per millisecond (rough; only used to rank plans)
GPU_SPEED = 100.0
CPU_SPEED_PER_SCORE = 1.25          # the PC (benchmark ~20) reads ~25 MB/ms
DEFAULT_CPU_SPEED = {"windows": 20.0, "linux": 15.0}
PHONE_CPU_SPEED = 4.0
#: per token, per part across the Wi-Fi / on the host itself
HOP_MS = 6.0
LOOPBACK_MS = 0.5
#: auto placement considers at most this many devices (2^n subsets)
MAX_CANDIDATES = 10
#: how often a waiting auto deployment looks for a new home (seconds)
REPLAN_EVERY = 20.0
PC_PLATFORMS = ("windows", "linux")


def slug(model_file: str) -> str:
    """The name a model file is called by: ``Qwen2.5-0.5B.gguf`` -> ``qwen2.5-0.5b``."""
    base = re.sub(r"\.gguf$", "", str(model_file or ""), flags=re.I).lower()
    base = re.sub(r"[^a-z0-9._-]+", "-", base).lstrip("._-")[:40].rstrip("._-")
    return base or "model"


def llama_platform_of(device: Dict[str, Any]) -> str:
    """Mirror of worker.py ``llama_platform`` from what the device reported."""
    info = device.get("info") or {}
    abi = info.get("abi") or ""
    platform = device.get("platform")
    if platform == "windows":
        return "win-x64" if abi in ("x86_64", "") else "source"
    api = info.get("android_api")
    if api is not None:
        return "android-arm64" if abi == "arm64-v8a" and int(api) >= 28 else "source"
    return "source"


def reserve_mb(device: Dict[str, Any]) -> int:
    return policy.reserve_mb(device)


def usable_mb(device: Dict[str, Any]) -> int:
    return policy.usable_mem_mb(device)


def cpu_speed(device: Dict[str, Any]) -> float:
    score = device.get("bench_score")
    if score:
        return max(1.0, min(40.0, float(score) * CPU_SPEED_PER_SCORE))
    return DEFAULT_CPU_SPEED.get(str(device.get("platform")), PHONE_CPU_SPEED)


class LLMManager:
    def __init__(self, state: Any) -> None:
        self.state = state
        self.fetches: Dict[str, Dict[str, Any]] = {}
        self._task: Optional[asyncio.Task] = None
        self._wake: Optional[asyncio.Event] = None  # created in start(), inside the loop
        self._summaries: Dict[str, Any] = {}
        self._replan_at: Dict[str, float] = {}

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="llm-manager")

    async def close(self) -> None:
        """Stop the background loop (deployments keep running on the devices)."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        for fetch in self.fetches.values():
            task = fetch.get("_task")
            if task is not None:
                task.cancel()

    def wake(self) -> None:
        if self._wake is not None:
            self._wake.set()

    async def _loop(self) -> None:
        while True:
            try:
                self.reconcile()
            except Exception:
                log.exception("llm reconcile failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    # --------------------------------------------------------------- runtime
    @property
    def tools_dir(self) -> Path:
        return Path(self.state.root) / "tools" / "llama.cpp"

    def tag(self) -> Optional[str]:
        version = self.tools_dir / "VERSION"
        try:
            return version.read_text(encoding="ascii").strip() or None
        except OSError:
            return None

    def runtime_manifest(self) -> Dict[str, Any]:
        tag = self.tag()
        assets: Dict[str, Dict[str, Any]] = {}
        if tag:
            for key, name in (("win-x64", "llama-%s-bin-win-cpu-x64.zip" % tag),
                              ("android-arm64", "llama-%s-bin-android-arm64.tar.gz" % tag)):
                path = self.tools_dir / name
                if path.is_file():
                    assets[key] = {"file": name, "size": path.stat().st_size}
        gpu = (self.tools_dir / "win-vulkan-x64").is_dir()
        return {"tag": tag, "assets": assets, "gpu_build": "win-vulkan-x64" if gpu else None,
                "source_repo": "https://github.com/ggml-org/llama.cpp"}

    def runtime_file(self, name: str) -> Optional[Path]:
        allowed = {a["file"] for a in self.runtime_manifest()["assets"].values()}
        if name not in allowed:
            return None
        return self.tools_dir / name

    # ---------------------------------------------------------------- models
    @property
    def models_dir(self) -> Path:
        configured = self.state.setting("llm.models_dir", None)
        path = Path(configured) if configured else Path(self.state.data) / "models"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def check_model_name(name: str) -> str:
        clean = Path(str(name or "")).name
        if not clean or clean != name or not clean.lower().endswith(".gguf"):
            raise ValueError("model name must be a plain file name ending in .gguf")
        return clean

    def model_path(self, name: str) -> Path:
        path = self.models_dir / self.check_model_name(name)
        if not path.is_file():
            raise KeyError("no model %r in %s" % (name, self.models_dir))
        return path

    def model_info(self, name: str) -> Dict[str, Any]:
        path = self.model_path(name)
        stat = path.stat()
        cache_key = (str(path), stat.st_size, stat.st_mtime)
        summary = self._summaries.get(cache_key)
        if summary is None:
            summary = summarize(path)
            self._summaries[cache_key] = summary
        out = dict(summary)
        out["title"] = summary.get("name")  # the name stored inside the GGUF
        out.update({"name": path.name, "size": stat.st_size, "modified": stat.st_mtime})
        return out

    def list_models(self) -> List[Dict[str, Any]]:
        out = []
        used = {}
        for dep in self.deployments(active_only=True):
            used.setdefault(dep["model"], []).append(dep["name"])
        for path in sorted(self.models_dir.glob("*.gguf")):
            info = self.model_info(path.name)
            info["used_by"] = used.get(path.name, [])
            out.append(info)
        for fetch in self.fetches.values():
            if fetch["status"] != "done":
                out.append({k: v for k, v in fetch.items() if not k.startswith("_")})
        return out

    def delete_model(self, name: str) -> None:
        path = self.model_path(name)
        users = [d["name"] for d in self.deployments(active_only=True) if d["model"] == path.name]
        if users:
            raise ValueError("model is in use by %s; stop it first" % ", ".join(users))
        path.unlink()

    async def save_upload(self, name: str, chunks: Any) -> Dict[str, Any]:
        clean = self.check_model_name(name)
        dest = self.models_dir / clean
        if dest.exists():
            raise ValueError("a model named %s already exists" % clean)
        part = dest.with_suffix(".gguf.part")
        size = 0
        head = b""
        with part.open("wb") as out:
            async for block in chunks:
                if not block:
                    continue
                if len(head) < 4:
                    head += block[: 4 - len(head)]
                size += len(block)
                out.write(block)
        if head != b"GGUF":
            part.unlink()
            raise ValueError("not a GGUF file")
        part.replace(dest)
        return self.model_info(clean)

    def start_fetch(self, url: str, name: Optional[str] = None) -> Dict[str, Any]:
        """Download a model from a URL into the models folder in the background."""
        if not re.match(r"^https?://", str(url or "")):
            raise ValueError("url must start with http:// or https://")
        guess = name or url.split("?")[0].rstrip("/").split("/")[-1]
        clean = self.check_model_name(guess)
        if (self.models_dir / clean).exists():
            raise ValueError("a model named %s already exists" % clean)
        if any(f["name"] == clean and f["status"] == "downloading" for f in self.fetches.values()):
            raise ValueError("%s is already downloading" % clean)
        fetch_id = new_id()
        fetch = {"id": fetch_id, "name": clean, "url": url, "status": "downloading",
                 "bytes": 0, "total": None, "error": None, "started": now()}
        self.fetches[fetch_id] = fetch
        fetch["_task"] = asyncio.get_running_loop().create_task(self._fetch(fetch))
        return {k: v for k, v in fetch.items() if not k.startswith("_")}

    async def _fetch(self, fetch: Dict[str, Any]) -> None:
        import httpx

        dest = self.models_dir / fetch["name"]
        part = dest.with_suffix(".gguf.part")
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": "bytes=%d-" % have} if have else {}
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(60.0, read=120.0)) as client:
                async with client.stream("GET", fetch["url"], headers=headers) as resp:
                    if resp.status_code not in (200, 206):
                        raise RuntimeError("HTTP %d from %s" % (resp.status_code, fetch["url"]))
                    if resp.status_code == 200:
                        have = 0
                    length = int(resp.headers.get("content-length") or 0)
                    fetch["total"] = have + length if length else None
                    fetch["bytes"] = have
                    with part.open("ab" if have else "wb") as out:
                        async for block in resp.aiter_bytes(1024 * 1024):
                            out.write(block)
                            fetch["bytes"] += len(block)
            with part.open("rb") as fh:
                if fh.read(4) != b"GGUF":
                    raise RuntimeError("the downloaded file is not a GGUF model")
            part.replace(dest)
            fetch["status"] = "done"
            self.state.event("llm_model_added", "model %s downloaded" % fetch["name"])
        except asyncio.CancelledError:
            fetch["status"] = "cancelled"
            raise
        except Exception as exc:
            fetch["status"] = "failed"
            fetch["error"] = str(exc)
            log.warning("model download %s failed: %s", fetch["name"], exc)
        self.state.hub.publish({"ev": "llm", "models": True})

    # --------------------------------------------------------------- devices
    def devices(self) -> List[Dict[str, Any]]:
        """Every device with what matters for holding a model: memory, GPU,
        llama.cpp build and whether it may take a model right now."""
        manifest = self.runtime_manifest()
        out = []
        for device in self.state.db.select("devices", "WHERE removed=0 ORDER BY created ASC"):
            out.append(self._public(self._device_view(device, manifest)))
        return out

    def _device_view(self, device: Dict[str, Any], manifest: Dict[str, Any]) -> Dict[str, Any]:
        caps = device.get("caps") or []
        platform_key = llama_platform_of(device)
        status = self.state.safety.check(device)
        reason = None
        if device.get("platform") == "browser":
            reason = "browser workers cannot run llama.cpp (they can still chat with a model)"
        elif "service" not in caps:
            reason = "worker is too old to host services; update worker.py"
        elif device.get("status") != "online":
            reason = "offline"
        elif platform_key != "source" and platform_key not in manifest["assets"]:
            reason = "no %s build on the coordinator (run scripts\\get-llama.ps1)" % platform_key
        elif device.get("paused"):
            reason = "switched off"
        elif not policy.roles(device)["ai"]:
            reason = "AI models are switched off for this device"
        elif not status["ai_start"]:
            reason = "; ".join(status["notes"]) or "resting"
        info = device.get("info") or {}
        gpu = policy.gpu_info(device)
        gpu_budget = policy.gpu_budget_mb(device) if status["gpu_start"] else 0
        return {
            "id": device["id"], "name": device.get("name"), "platform": device.get("platform"),
            "status": device.get("status"), "mem_mb": info.get("mem_mb"),
            "mem_free_mb": device.get("mem_free_mb"), "usable_mb": usable_mb(device),
            "storage_free_mb": device.get("storage_free_mb"), "abi": info.get("abi"),
            "android_api": info.get("android_api"), "llama_build": platform_key,
            "bench_score": device.get("bench_score"), "cpu_count": policy.cores(device),
            "threads": policy.model_threads(device),
            "gpu": gpu, "gpu_build": policy.gpu_runtime(device), "gpu_budget_mb": gpu_budget,
            "gpu_pct": policy.limits(device)["gpu_pct"],
            "local_files": bool(info.get("coordinator_local")),
            "eligible": reason is None, "reason": reason,
            "notes": status["notes"],
            "_device": device, "_status": status,
        }

    @staticmethod
    def _public(view: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in view.items() if not k.startswith("_")}

    def _units(self, view: Dict[str, Any], gpu: bool = True) -> List[Dict[str, Any]]:
        """The parts of a device that can hold layers, slowest first."""
        device = view["_device"]
        units = [{"kind": "cpu", "rpc": "CPU", "budget": float(view["usable_mb"]),
                  "speed": cpu_speed(device)}]
        if gpu and view["gpu_budget_mb"] > 0:
            info = view["gpu"] or {}
            units.append({"kind": "gpu", "rpc": "Vulkan0", "budget": float(view["gpu_budget_mb"]),
                          "speed": GPU_SPEED, "vk_index": info.get("vk_index"),
                          "gpu_name": info.get("name")})
        return units

    # --------------------------------------------------------------- planning
    @staticmethod
    def facts(info: Dict[str, Any], ctx: int) -> Dict[str, Any]:
        """Memory figures (MB) for one model at one context size."""
        layers = int(info.get("layers") or 0)
        size_mb = info["size"] / MB
        embd = (info.get("embd_bytes") or 0) / MB
        out = (info.get("output_bytes") or 0) / MB
        if layers and info.get("layer_bytes"):
            weights = info["layer_bytes"] / MB
        elif layers:
            weights = max(0.01, (size_mb - embd - out) / layers)
        else:
            weights = size_mb
        kv_total = ((info.get("kv_bytes_per_token") or int(info["size"] * 0.08 / 2048)) * ctx) / MB
        kv_layer = kv_total / layers if layers else kv_total
        compute = ((info.get("compute_bytes_base") or 40 * MB)
                   + (info.get("compute_bytes_per_ctx") or 4096) * ctx) / MB + COMPUTE_MARGIN_MB
        return {
            "layers": layers, "model_mb": size_mb, "embd_mb": embd, "out_mb": out,
            "weights_mb": weights, "layer_mb": weights + kv_layer, "kv_mb": kv_total,
            "compute_mb": compute, "host_mb": embd + HOST_COMPUTE_MB,
        }

    def plan(self, model: str, device_ids: Optional[List[str]] = None, host: Optional[str] = None,
             weights: Optional[Dict[str, float]] = None, ctx: int = 2048,
             mode: Optional[str] = None, no_gpu: Optional[List[str]] = None) -> Dict[str, Any]:
        """Work out who holds what, whether it fits, and roughly how fast it runs.

        ``mode`` ``auto`` picks the devices among ``device_ids`` (all when
        empty); ``manual`` uses exactly ``device_ids`` with ``host`` and
        ``weights``.  Without a mode it is manual when a host or weights are
        given, else auto.
        """
        info = self.model_info(model)
        ctx = int(ctx or 2048)
        if mode not in ("auto", "manual"):
            mode = "manual" if (host or weights) else "auto"
        f = self.facts(info, ctx)
        manifest = self.runtime_manifest()
        views = {d["id"]: self._device_view(d, manifest)
                 for d in self.state.db.select("devices", "WHERE removed=0 ORDER BY created ASC")}
        skip_gpu = set(no_gpu or [])
        base: Dict[str, Any] = {
            "mode": mode, "model": info, "ctx": ctx,
            "model_mb": int(f["model_mb"]), "kv_mb": int(f["kv_mb"]),
            "compute_mb": int(f["compute_mb"]), "output_mb": int(f["out_mb"]),
            "total_need_mb": int(f["model_mb"] + f["kv_mb"] + f["compute_mb"]),
            "errors": [], "warnings": [],
        }
        if not f["layers"]:
            base["errors"].append("cannot read the layer count from this model's header")
        if mode == "auto":
            result = self._plan_auto(f, views, [str(d) for d in (device_ids or [])], skip_gpu, base)
        else:
            result = self._plan_manual(f, views, [str(d) for d in dict.fromkeys(device_ids or [])],
                                       host, weights, skip_gpu, base)
        result["ok"] = not result["errors"] and result.get("fits", False)
        if result.get("ms_per_token"):
            result["tokens_per_second"] = round(1000.0 / result["ms_per_token"], 1)
        return result

    # -- evaluation -----------------------------------------------------
    def _single(self, f: Dict[str, Any], view: Dict[str, Any], gpu: bool) -> Dict[str, Any]:
        """The whole model on one device (GPU first, rest on its CPU)."""
        units = self._units(view, gpu)
        cpu = units[0]
        card = units[1] if len(units) > 1 else None
        n = f["layers"]
        gpu_layers = 0
        full = False
        if card is not None:
            room = card["budget"] - f["compute_mb"]
            if room >= n * f["layer_mb"] + f["out_mb"]:
                gpu_layers, full = n, True
            else:
                gpu_layers = max(0, min(n, int(room // f["layer_mb"])))
        cpu_layers = n - gpu_layers
        cpu_need = f["host_mb"] + cpu_layers * f["layer_mb"] + (0 if full else f["out_mb"]) \
            + (f["compute_mb"] if not full else 0)
        gpu_need = (gpu_layers * f["layer_mb"] + (f["out_mb"] if full else 0) + f["compute_mb"]) if gpu_layers else 0
        ms = cpu_layers * f["weights_mb"] / cpu["speed"]
        ms += f["out_mb"] / (card["speed"] if full else cpu["speed"])
        if gpu_layers:
            ms += gpu_layers * f["weights_mb"] / card["speed"]
        parts = [{"kind": "cpu", "layers": cpu_layers, "need_mb": int(cpu_need),
                  "budget_mb": int(cpu["budget"]), "fits": cpu_need <= cpu["budget"]}]
        if card is not None:
            parts.append({"kind": "gpu", "layers": gpu_layers, "need_mb": int(gpu_need),
                          "budget_mb": int(card["budget"]), "fits": gpu_need <= card["budget"],
                          "gpu_name": card.get("gpu_name"), "vk_index": card.get("vk_index")})
        return {
            "fits": all(p["fits"] for p in parts), "ms": ms, "gpu_layers": gpu_layers, "full": full,
            "parts": parts, "need_mb": int(cpu_need + gpu_need),
            "vk_index": card.get("vk_index") if card is not None and gpu_layers else None,
        }

    def _split(self, f: Dict[str, Any], views: List[Dict[str, Any]], host_id: str,
               skip_gpu: set, layers_per_device: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
        """Layers over several devices' parts.

        Auto (``layers_per_device`` None): the fastest parts are filled first,
        each to its budget less one layer of slack; the fastest part used also
        takes the output layer.  Manual: each device gets the given layer
        count, its GPU first.  Devices are ordered by their fastest part, so
        that part ends up last and holds the output layer.
        """
        n = f["layers"]
        entries = []  # (view, unit, budget)
        for view in views:
            for unit in self._units(view, view["id"] not in skip_gpu):
                budget = unit["budget"]
                if view["id"] == host_id and unit["kind"] == "cpu":
                    budget -= f["host_mb"]
                entries.append({"view": view, "unit": unit, "budget": budget, "layers": 0, "output": False})
        slack = f["layer_mb"]
        if layers_per_device is None:
            remaining = n
            for entry in sorted(entries, key=lambda e: -e["unit"]["speed"]):
                if remaining <= 0:
                    break
                output = not any(e["layers"] for e in entries)
                room = entry["budget"] - f["compute_mb"] - slack - (f["out_mb"] if output else 0)
                take = max(0, min(remaining, int(room // f["layer_mb"])))
                if take:
                    entry["layers"] = take
                    entry["output"] = output
                    remaining -= take
        else:
            remaining = 0
            for view in views:
                want = int(layers_per_device.get(view["id"], 0))
                mine = sorted([e for e in entries if e["view"]["id"] == view["id"]],
                              key=lambda e: -e["unit"]["speed"])
                for entry in mine:
                    if want <= 0:
                        break
                    if entry is mine[-1]:
                        take = want  # whatever is left lands on the CPU
                    else:
                        room = entry["budget"] - f["compute_mb"] - f["out_mb"]
                        take = max(0, min(want, int(room // f["layer_mb"])))
                    entry["layers"] = take
                    want -= take
        used = [e for e in entries if e["layers"] > 0]
        if not used:
            return {"fits": False, "ms": float("inf"), "devices": [], "rpc": [], "remaining": n}

        # order: devices by their fastest used part, parts slowest first inside a device
        def device_speed(view_id: str) -> float:
            return max(e["unit"]["speed"] for e in used if e["view"]["id"] == view_id)

        position = {view["id"]: index for index, view in enumerate(views)}
        order_ids = sorted({e["view"]["id"] for e in used}, key=lambda v: (device_speed(v), position[v]))
        ordered: List[Dict[str, Any]] = []
        for view_id in order_ids:
            ordered += sorted([e for e in used if e["view"]["id"] == view_id], key=lambda e: e["unit"]["speed"])
        for entry in ordered:
            entry["output"] = False
        ordered[-1]["output"] = True
        ms = 0.0
        rpc = []
        for entry in ordered:
            entry["need"] = entry["layers"] * f["layer_mb"] + f["compute_mb"] + (f["out_mb"] if entry["output"] else 0)
            entry["fits"] = entry["need"] <= entry["budget"]
            ms += entry["layers"] * f["weights_mb"] / entry["unit"]["speed"]
            ms += LOOPBACK_MS if entry["view"]["id"] == host_id else HOP_MS
            rpc.append({"device_id": entry["view"]["id"], "kind": entry["unit"]["kind"],
                        "rpc": entry["unit"]["rpc"], "layers": entry["layers"],
                        "vk_index": entry["unit"].get("vk_index")})
        ms += f["out_mb"] / ordered[-1]["unit"]["speed"]
        devices = []
        for view_id in order_ids:
            mine = [e for e in ordered if e["view"]["id"] == view_id]
            view = mine[0]["view"]
            parts = [{"kind": e["unit"]["kind"], "layers": e["layers"], "need_mb": int(e["need"]),
                      "budget_mb": int(e["budget"]), "fits": e["fits"],
                      "gpu_name": e["unit"].get("gpu_name")} for e in mine]
            devices.append({"view": view, "parts": parts,
                            "layers": sum(e["layers"] for e in mine),
                            "need_mb": int(sum(e["need"] for e in mine)),
                            "fits": all(e["fits"] for e in mine)})
        return {"fits": remaining <= 0 and all(e["fits"] for e in ordered), "ms": ms,
                "devices": devices, "rpc": rpc, "remaining": remaining}

    # -- auto -----------------------------------------------------------
    def _plan_auto(self, f: Dict[str, Any], views: Dict[str, Dict[str, Any]], allowed: List[str],
                   skip_gpu: set, base: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(base)
        pool = [views[i] for i in allowed if i in views] if allowed else list(views.values())
        for missing in [i for i in allowed if i not in views]:
            out["errors"].append("unknown device %s" % missing)
        excluded = [{"id": v["id"], "name": v["name"], "reason": v["reason"]} for v in pool if not v["eligible"]]
        candidates = [v for v in pool if v["eligible"]]
        out["excluded"] = excluded
        if out["errors"]:
            return self._empty(out)
        if not candidates:
            out["errors"].append("no device may take a model right now" +
                                 (": " + "; ".join("%s (%s)" % (e["name"], e["reason"]) for e in excluded)
                                  if excluded else ""))
            return self._empty(out)
        best: Optional[Tuple[float, int, str, Any]] = None
        # one device alone: no network in the way
        for view in candidates:
            if not self._can_host(view, f):
                continue
            single = self._single(f, view, view["id"] not in skip_gpu)
            if single["fits"]:
                key = (single["ms"], 1)
                if best is None or key < best[:2]:
                    best = (single["ms"], 1, "single", (view, single))
        # several devices: try every combination of the fastest candidates
        ranked = sorted(candidates, key=lambda v: -max(u["speed"] for u in self._units(v)))[:MAX_CANDIDATES]
        if len(ranked) >= 2:
            for size in range(2, len(ranked) + 1):
                for combo in itertools.combinations(ranked, size):
                    host = self._pick_host(list(combo), f)
                    if host is None:
                        continue
                    split = self._split(f, list(combo), host["id"], skip_gpu)
                    if not split["fits"] or len(split["devices"]) < size:
                        continue  # a device that ends up with nothing is a smaller combination
                    key = (split["ms"], size)
                    if best is None or key < best[:2]:
                        best = (split["ms"], size, "split", (host, split))
        if best is None:
            capacity = sum(sum(u["budget"] for u in self._units(v, v["id"] not in skip_gpu)) for v in candidates)
            out["errors"].append(
                "the model does not fit: it needs about %d MB, the allowed devices have about %d MB usable"
                % (out["total_need_mb"], capacity))
            return self._empty(out)
        if best[2] == "single":
            view, single = best[3]
            return self._single_result(out, f, view, single, auto=True)
        host, split = best[3]
        return self._split_result(out, f, host, split, auto=True)

    def _can_host(self, view: Dict[str, Any], f: Dict[str, Any]) -> bool:
        """The host keeps the model file: phones need the storage for it."""
        if view["local_files"]:
            return True
        free = view.get("storage_free_mb")
        return free is None or free >= f["model_mb"] * 1.05

    def _pick_host(self, views: List[Dict[str, Any]], f: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        pcs = [v for v in views if v["platform"] in PC_PLATFORMS and self._can_host(v, f)]
        if pcs:
            return max(pcs, key=lambda v: (v["local_files"], v["usable_mb"]))
        able = [v for v in views if self._can_host(v, f) and v["usable_mb"] > f["host_mb"]]
        if not able:
            return None
        return max(able, key=lambda v: (cpu_speed(v["_device"]), v["usable_mb"]))

    # -- manual ---------------------------------------------------------
    def _plan_manual(self, f: Dict[str, Any], views: Dict[str, Dict[str, Any]], ids: List[str],
                     host: Optional[str], weights: Optional[Dict[str, float]], skip_gpu: set,
                     base: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(base)
        errors = out["errors"]
        if not ids:
            errors.append("choose at least one device to hold the model")
        chosen = []
        for device_id in ids:
            view = views.get(device_id)
            if view is None:
                errors.append("unknown device %s" % device_id)
                continue
            if not view["eligible"]:
                errors.append("%s: %s" % (view["name"], view["reason"]))
            chosen.append(view)
        weights = {str(k): float(v) for k, v in (weights or {}).items() if v is not None}
        for view in chosen:
            capacity = sum(u["budget"] for u in self._units(view, view["id"] not in skip_gpu))
            view["_weight"] = weights.get(view["id"]) if weights.get(view["id"]) is not None else float(capacity)
        if any(v["_weight"] <= 0 for v in chosen):
            errors.append("every chosen device needs a share above zero (remove it instead)")
        if host:
            host = str(host)
        elif chosen:
            pcs = [v for v in chosen if v["platform"] in PC_PLATFORMS]
            host = (max(pcs, key=lambda v: v["usable_mb"]) if pcs else max(chosen, key=lambda v: v["_weight"]))["id"]
        host_view = views.get(host or "")
        if host and host_view is None:
            errors.append("unknown host device %s" % host)
        elif host_view is not None and not host_view["eligible"]:
            errors.append("host %s: %s" % (host_view["name"], host_view["reason"]))
        if errors or not chosen or host_view is None:
            return self._empty(out)
        if not self._can_host(host_view, f):
            out["warnings"].append("host %s may lack storage for the %.0f MB model file"
                                   % (host_view["name"], f["model_mb"]))
        if len(chosen) == 1 and chosen[0]["id"] == host_view["id"]:
            single = self._single(f, host_view, host_view["id"] not in skip_gpu)
            return self._single_result(out, f, host_view, single, auto=False)
        total = sum(v["_weight"] for v in chosen) or 1.0
        n = f["layers"]
        ordered = sorted(chosen, key=lambda v: v["_weight"])
        counts: Dict[str, int] = {}
        given = 0
        for index, view in enumerate(ordered):
            if index == len(ordered) - 1:
                counts[view["id"]] = n - given
            else:
                counts[view["id"]] = max(1, int(round(n * view["_weight"] / total)))
                given += counts[view["id"]]
        split = self._split(f, ordered, host_view["id"], skip_gpu, counts)
        result = self._split_result(out, f, host_view, split, auto=False)
        for row in result["devices"]:
            weight = next((v["_weight"] for v in chosen if v["id"] == row["id"]), None)
            row["weight"] = weight
            row["share"] = round((weight or 0) / total, 4)
        if host_view["id"] not in ids and host_view["usable_mb"] < f["host_mb"]:
            out["warnings"].append("host %s has little free memory" % host_view["name"])
        return result

    # -- result shapes --------------------------------------------------
    def _empty(self, out: Dict[str, Any]) -> Dict[str, Any]:
        out.update({"placement": None, "host": None, "host_name": None, "devices": [], "rpc": [],
                    "tensor_split": [], "fits": False, "total_usable_mb": 0})
        return out

    def _row(self, view: Dict[str, Any], role: str, parts: List[Dict[str, Any]], layers: int,
             need: int, fits: bool, n: int) -> Dict[str, Any]:
        row = self._public(view)
        row.update({"role": role, "parts": parts, "layers": layers, "need_mb": need, "fits": fits,
                    "share": round(layers / n, 4) if n else None, "weight": None})
        return row

    def _single_result(self, out: Dict[str, Any], f: Dict[str, Any], view: Dict[str, Any],
                       single: Dict[str, Any], auto: bool) -> Dict[str, Any]:
        n = f["layers"]
        row = self._row(view, "host", single["parts"], n, single["need_mb"], single["fits"], n)
        where = "%s alone" % view["name"]
        if single["gpu_layers"]:
            gpu_name = single["parts"][-1].get("gpu_name") or "GPU"
            where += " (%d of %d layers on the %s)" % (single["gpu_layers"], n, gpu_name)
        out.update({
            "placement": "single", "host": view["id"], "host_name": view["name"], "devices": [row],
            "rpc": [], "tensor_split": [], "gpu_layers": single["gpu_layers"], "gpu_full": single["full"],
            "vk_index": single["vk_index"], "fits": single["fits"], "ms_per_token": round(single["ms"], 2),
            "total_usable_mb": int(sum(p["budget_mb"] for p in single["parts"])),
            "summary": ("fits on " if auto else "runs on ") + where,
        })
        if not single["fits"]:
            for part in single["parts"]:
                if not part["fits"]:
                    out["warnings"].append("%s %s needs %d MB but has about %d MB usable"
                                           % (view["name"], part["kind"].upper(), part["need_mb"], part["budget_mb"]))
        if view["llama_build"] == "source":
            out["warnings"].append("%s will compile llama.cpp on first start (15-40 minutes, needs internet once)"
                                   % view["name"])
        return out

    def _split_result(self, out: Dict[str, Any], f: Dict[str, Any], host: Dict[str, Any],
                      split: Dict[str, Any], auto: bool) -> Dict[str, Any]:
        n = f["layers"]
        rows = []
        for entry in split["devices"]:
            view = entry["view"]
            role = "shard + host" if view["id"] == host["id"] else "shard"
            rows.append(self._row(view, role, entry["parts"], entry["layers"], entry["need_mb"],
                                  entry["fits"], n))
            if not entry["fits"]:
                for part in entry["parts"]:
                    if not part["fits"]:
                        out["warnings"].append("%s %s needs %d MB but has about %d MB usable"
                                               % (view["name"], part["kind"].upper(), part["need_mb"],
                                                  part["budget_mb"]))
        if split.get("remaining", 0) > 0:
            out["warnings"].append("%d layer(s) have no room on the chosen devices" % split["remaining"])
        names = ", ".join("%s %d" % (r["name"], r["layers"]) for r in rows)
        out.update({
            "placement": "split", "host": host["id"], "host_name": host["name"], "devices": rows,
            "rpc": split["rpc"], "tensor_split": [p["layers"] for p in split["rpc"]],
            "fits": split["fits"], "ms_per_token": round(split["ms"], 2) if split["devices"] else None,
            "total_usable_mb": int(sum(p["budget_mb"] for r in rows for p in r["parts"])),
            "summary": ("split over %d devices (layers: %s); %s hosts it" if auto else
                        "split over %d devices (layers: %s); %s hosts it") % (len(rows), names, host["name"]),
        })
        if any(r["llama_build"] == "source" for r in rows) or host["llama_build"] == "source":
            out["warnings"].append("some devices will compile llama.cpp on first start (15-40 minutes, needs internet once)")
        return out

    # ------------------------------------------------------------ deployments
    def deployments(self, active_only: bool = False, replicas: bool = False) -> List[Dict[str, Any]]:
        """Deployments, newest first; extra copies (autoscaling) only with ``replicas``."""
        rows = self.state.db.select("llm_deployments", "ORDER BY created DESC")
        if not replicas:
            rows = [r for r in rows if not (r.get("options") or {}).get("replica_of")]
        if active_only:
            rows = [r for r in rows if r["status"] in ACTIVE]
        return rows

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        row = self.state.db.get("llm_deployments", key)
        if row is None:
            rows = [r for r in self.deployments() if r["name"] == key]
            active = [r for r in rows if r["status"] in ACTIVE]
            row = (active or rows or [None])[0]
        return row

    def replicas_of(self, dep: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [r for r in self.state.db.select("llm_deployments")
                if (r.get("options") or {}).get("replica_of") == dep["id"]]

    def idle_minutes(self, dep: Dict[str, Any]) -> float:
        """Unused this long -> unloaded; 0 keeps it loaded."""
        value = (dep.get("options") or {}).get("idle_minutes")
        if value is None:
            value = self.state.setting("llm.idle_minutes", 10)
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return 10.0

    def describe(self, dep: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(dep)
        names = {d["id"]: d.get("name") for d in self.state.db.select("devices")}
        for shard in out.get("shards") or []:
            shard["name"] = names.get(shard["device_id"])
            shard["endpoint"] = (dep.get("endpoints") or {}).get(shard["device_id"])
        out["host_name"] = names.get(dep.get("host_device"))
        opts = dep.get("options") or {}
        out["mode"] = opts.get("mode") or "manual"
        out["placement"] = opts.get("placement") or "split"
        out["idle_minutes"] = self.idle_minutes(dep)
        out["copies"] = [{"id": r["id"], "status": r["status"], "host_name": names.get(r.get("host_device"))}
                         for r in self.replicas_of(dep) if r["status"] in ACTIVE]
        head = self._head_instance(dep)
        out["head"] = head.to_dict(self.state) if head is not None else None
        return out

    def deploy(self, name: str, model: str, device_ids: Optional[List[str]] = None, host: Optional[str] = None,
               weights: Optional[Dict[str, float]] = None, ctx: int = 2048, threads: int = 0,
               force: bool = False, extra_args: Optional[List[str]] = None,
               mode: Optional[str] = None, idle_minutes: Optional[float] = None,
               on_demand: bool = False) -> Dict[str, Any]:
        name = str(name or "").strip().lower()
        if not NAME_RE.match(name):
            raise ValueError("name must be 1-40 characters: lowercase letters, digits, '.', '_' or '-'")
        if any(d["name"] == name for d in self.deployments(active_only=True)):
            raise ValueError("a deployment named %s is already running" % name)
        if name in self.state.services.names():
            raise ValueError("a service named %s is already running" % name)
        # an unloaded model of the same name gives way to the new one
        for old in self.deployments():
            if old["name"] == name and old["status"] not in ACTIVE:
                self.state.db.execute("DELETE FROM llm_deployments WHERE id=?", (old["id"],))
        tag = self.tag()
        if not tag:
            raise ValueError("llama.cpp is not installed on the coordinator (run scripts\\get-llama.ps1)")
        plan = self.plan(model, device_ids, host, weights, ctx, mode)
        if plan["errors"] and force and plan["mode"] == "auto" and plan.get("excluded") is not None:
            # nothing fits and the user insists: spread it over the allowed devices anyway
            ids = [d for d in (device_ids or [])] or [d["id"] for d in self.devices() if d["eligible"]]
            ids = [d for d in ids if d not in {e["id"] for e in plan["excluded"]}]
            if ids:
                plan = self.plan(model, ids, None, None, ctx, "manual")
        if plan["errors"]:
            raise ValueError("; ".join(plan["errors"]))
        if not plan["ok"] and not force:
            raise ValueError("the model does not fit: " + "; ".join(plan["warnings"] or
                             ["needs %d MB, %d MB usable" % (plan["total_need_mb"], plan["total_usable_mb"])]))
        request = {"mode": plan["mode"], "devices": [str(d) for d in (device_ids or [])],
                   "host": host, "weights": weights or {}, "ctx": plan["ctx"]}
        dep_id = new_id()
        row = {
            "id": dep_id, "name": name, "model": plan["model"]["name"], "model_size": plan["model"]["size"],
            "host_device": plan["host"], "shards": [],
            "options": {"ctx": plan["ctx"], "threads": int(threads or 0), "llama": tag,
                        "extra_args": list(extra_args or []), "request": request,
                        "idle_minutes": idle_minutes, "on_demand": bool(on_demand)},
            "status": "starting", "error": None, "shard_jobs": {}, "head_job": None,
            "endpoints": {}, "head_endpoints": [], "created": now(), "updated": now(), "ready_at": None,
        }
        row.update(self._layout(plan, row["options"]))
        self.state.db.insert("llm_deployments", row)
        for shard in row["shards"]:
            self._submit_shard(row, shard)
        self.state.event("llm_starting", "starting %s (%s): %s" % (name, row["model"], plan.get("summary") or ""))
        self._publish(dep_id)
        self.wake()
        return self.get(dep_id)

    @staticmethod
    def _layout(plan: Dict[str, Any], options: Dict[str, Any]) -> Dict[str, Any]:
        """Turn a plan into the row fields that drive the jobs."""
        opts = dict(options)
        opts.update({"mode": plan["mode"], "placement": plan["placement"], "plan_ok": plan["ok"],
                     "summary": plan.get("summary"), "ms_per_token": plan.get("ms_per_token")})
        shards = []
        if plan["placement"] == "single":
            opts["gpu_layers"] = int(plan.get("gpu_layers") or 0)
            opts["gpu_full"] = bool(plan.get("gpu_full"))
            opts["vk_index"] = plan.get("vk_index")
        else:
            opts.pop("gpu_layers", None)
            for row in plan["devices"]:
                parts = [p for p in plan["rpc"] if p["device_id"] == row["id"]]
                gpu = next((p for p in parts if p["kind"] == "gpu"), None)
                shards.append({
                    "device_id": row["id"], "layers": row["layers"], "need_mb": row["need_mb"],
                    "weight": row.get("weight") if row.get("weight") is not None else row["layers"],
                    "parts": [{"kind": p["kind"], "rpc": p["rpc"], "layers": p["layers"]} for p in parts],
                    "gpu": {"vk_index": gpu.get("vk_index")} if gpu else None,
                })
        return {"host_device": plan["host"], "shards": shards, "options": opts}

    def _threads(self, device_id: str, requested: int) -> int:
        device = self.state.db.get("devices", device_id) or {}
        allowance = policy.model_threads(device) if device else 1
        return min(int(requested), allowance) if requested and int(requested) > 0 else allowance

    def _submit_shard(self, dep: Dict[str, Any], shard: Dict[str, Any]) -> str:
        opts = dep["options"]
        device_id = shard["device_id"]
        params: Dict[str, Any] = {"deployment": dep["id"], "device_id": device_id, "llama": opts["llama"],
                                  "threads": self._threads(device_id, opts.get("threads") or 0)}
        if shard.get("gpu"):
            # the PC offers its CPU and its graphics card as two parts, card last
            params["devices"] = [p["rpc"] for p in shard.get("parts") or []]
            params["gpu"] = shard["gpu"]
        job = self.state.submit_job({
            "type": "llm_shard", "name": "%s shard" % dep["name"],
            "params": params,
            "options": {"targets": [device_id]},
        })
        jobs = dict(self.state.db.get("llm_deployments", dep["id"])["shard_jobs"] or {})
        jobs[device_id] = job["id"]
        self.state.db.update("llm_deployments", {"id": dep["id"]}, {"shard_jobs": jobs})
        return job["id"]

    def _submit_head(self, dep: Dict[str, Any], endpoints: List[str]) -> str:
        opts = dep["options"]
        single = opts.get("placement") == "single"
        if single:
            tensor_split: List[Any] = []
            where = "on %s alone" % ((self.state.db.get("devices", dep["host_device"]) or {}).get("name") or "one device")
        else:
            tensor_split = []
            for shard in dep["shards"]:
                parts = shard.get("parts") or []
                tensor_split += [p["layers"] for p in parts] if parts else [shard.get("weight") or shard.get("layers")]
            where = "on %d device(s)" % len(dep["shards"])
        params: Dict[str, Any] = {
            "deployment": dep["id"], "name": dep["name"], "device_id": dep["host_device"],
            "llama": opts["llama"], "model": {"name": dep["model"], "size": dep["model_size"]},
            "rpc": endpoints, "tensor_split": tensor_split,
            "ctx": opts.get("ctx") or 2048,
            "threads": self._threads(dep["host_device"], opts.get("threads") or 0),
            "extra_args": opts.get("extra_args") or [],
            # shown on the Services page
            "description": "%s %s" % (dep["model"], where),
            "accepts": "text or JSON {messages, max_tokens, temperature}",
            "produces": "text (streamed)", "streaming_output": True,
        }
        if single and opts.get("gpu_layers"):
            params["gpu_layers"] = 999 if opts.get("gpu_full") else int(opts["gpu_layers"])
            params["gpu"] = {"vk_index": opts.get("vk_index")}
        job = self.state.submit_job({
            "type": "llm_head", "name": "%s (model)" % dep["name"],
            "params": params,
            "options": {"targets": [dep["host_device"]]},
        })
        self.state.db.update("llm_deployments", {"id": dep["id"]},
                             {"head_job": job["id"], "head_endpoints": endpoints})
        return job["id"]

    def stop(self, key: str, reason: str = "stopped by user", status: str = "stopped") -> Dict[str, Any]:
        dep = self.get(key)
        if dep is None:
            raise KeyError("no deployment %s" % key)
        for replica in self.replicas_of(dep):
            self.drop_replica(replica, "the model stopped")
        self._cancel_jobs(dep, include_shards=True)
        if dep["status"] in ACTIVE:
            self._set(dep, status, error=None if reason == "stopped by user" or status == IDLE else reason)
            if status == IDLE:
                self.state.event("llm_idle", "%s unloaded (%s); the next call loads it again" % (dep["name"], reason))
            else:
                self.state.event("llm_stopped", "%s stopped" % dep["name"])
        self._replan_at.pop(dep["id"], None)
        return self.get(dep["id"])

    def restart(self, key: str) -> Dict[str, Any]:
        """Start again from scratch, placing the model anew when it still fits
        (so an auto model returns to the GPU after it cooled down)."""
        dep = self.get(key)
        if dep is None:
            raise KeyError("no deployment %s" % key)
        for replica in self.replicas_of(dep):
            self.drop_replica(replica, "the model restarted")
        self._cancel_jobs(dep, include_shards=True)
        fields: Dict[str, Any] = {"shard_jobs": {}, "head_job": None, "endpoints": {},
                                  "head_endpoints": [], "error": None}
        plan = self._replan(dep)
        if plan is not None and plan["ok"]:
            fields.update(self._layout(plan, dep["options"]))
        self.state.db.update("llm_deployments", {"id": dep["id"]}, fields)
        dep = self.state.db.get("llm_deployments", dep["id"])
        for shard in dep["shards"]:
            self._submit_shard(dep, shard)
        self._set(dep, "starting")
        self.wake()
        return self.get(dep["id"])

    def delete(self, key: str) -> None:
        dep = self.get(key)
        if dep is None:
            raise KeyError("no deployment %s" % key)
        if dep["status"] in ACTIVE:
            raise ValueError("stop the deployment first")
        self.state.db.execute("DELETE FROM llm_deployments WHERE id=?", (dep["id"],))
        self.state.hub.publish({"ev": "llm", "deleted": dep["id"]})

    # ---------------------------------------------------- loading on call
    def model_for(self, name: str) -> Optional[str]:
        """The model file whose name (``slug``) is ``name``."""
        for path in sorted(self.models_dir.glob("*.gguf")):
            if slug(path.name) == name:
                return path.name
        return None

    def default_model(self) -> Optional[str]:
        chosen = self.state.setting("llm.default_model", None)
        if chosen and (self.models_dir / str(chosen)).is_file():
            return str(chosen)
        files = sorted(p.name for p in self.models_dir.glob("*.gguf"))
        return files[0] if len(files) == 1 else None

    def set_default(self, model: Optional[str]) -> Optional[str]:
        if model:
            self.model_path(model)  # must exist
        section = dict(self.state.config.get("llm") or {})
        section["default_model"] = model or None
        self.state.save_settings({"llm": section})
        return model or None

    def resolve_alias(self, name: str) -> str:
        """``chat`` / ``llm`` reach the default model, a ``.gguf`` file name its model."""
        name = str(name or "")
        if name in ALIASES and name not in self.state.services.names():
            model = self.default_model()
            if model:
                return self.get_name_for(model)
        if name.lower().endswith(".gguf"):
            return self.get_name_for(name)
        return name

    def get_name_for(self, model: str) -> str:
        """The deployment name a model file is called by (its own, if it has one)."""
        for dep in self.deployments():
            if dep["model"] == model:
                return dep["name"]
        return slug(model)

    def loadable(self) -> List[Dict[str, str]]:
        """Models that are not running but start on the first call."""
        out: List[Dict[str, str]] = []
        running = {d["name"] for d in self.deployments(active_only=True)}
        seen = set()
        for dep in self.deployments():
            if dep["name"] in running or dep["name"] in seen or dep["status"] == "failed":
                continue
            seen.add(dep["name"])
            out.append({"name": dep["name"], "model": dep["model"],
                        "description": "%s - loads on the first call" % dep["model"]})
        if self.tag():
            known = {d["model"] for d in self.deployments()}
            for path in sorted(self.models_dir.glob("*.gguf")):
                name = slug(path.name)
                if path.name in known or name in seen or name in running:
                    continue
                seen.add(name)
                out.append({"name": name, "model": path.name,
                            "description": "%s - loads on the first call" % path.name})
        return out

    def ensure(self, name: str) -> Optional[str]:
        """Called for a service ``name`` that has no running instance: start the
        model of that name (again) and say so; None when ``name`` is no model.
        Raises ``ValueError`` when it cannot run (does not fit, just failed)."""
        rows = [d for d in self.deployments() if d["name"] == name]
        active = [d for d in rows if d["status"] in ACTIVE]
        if active:
            dep = active[0]
            return "%s is %s (%s)" % (name, dep["status"], (dep.get("options") or {}).get("summary") or dep["model"])
        if rows:
            dep = rows[0]
            if dep["status"] == "failed" and time.time() - float(dep.get("updated") or 0) < FAILED_COOLDOWN:
                raise ValueError(dep.get("error") or "the model failed")
            dep = self.restart(dep["id"])
            self.state.event("llm_on_demand", "%s requested: loading it (%s)"
                             % (name, (dep.get("options") or {}).get("summary") or dep["model"]))
            return "loading %s (%s)" % (name, (dep.get("options") or {}).get("summary") or dep["model"])
        model = self.model_for(name)
        if model is None or not self.tag():
            return None
        dep = self.deploy(name, model, mode="auto", on_demand=True)
        summary = (dep.get("options") or {}).get("summary") or ""
        self.state.event("llm_on_demand", "%s requested: loading it (%s)" % (name, summary))
        return "loading %s (%s)" % (name, summary)

    def set_keep(self, key: str, keep: bool) -> Dict[str, Any]:
        """Keep a model loaded (never unload when idle) or let it unload."""
        dep = self.get(key)
        if dep is None:
            raise KeyError("no deployment %s" % key)
        options = dict(dep.get("options") or {})
        options["idle_minutes"] = 0 if keep else None
        self.state.db.update("llm_deployments", {"id": dep["id"]}, {"options": options})
        self._publish(dep["id"])
        return self.get(dep["id"])

    def devices_for(self, deployment_id: str) -> List[str]:
        """Every device a deployment runs on (host and shards)."""
        dep = self.state.db.get("llm_deployments", deployment_id)
        if dep is None:
            return []
        return list(dict.fromkeys([dep["host_device"]] + [s["device_id"] for s in dep.get("shards") or []]))

    def _unload_idle(self, dep: Dict[str, Any]) -> bool:
        """Unload a ready model nobody used for its idle time (SPEC 6.4d)."""
        if dep["status"] != "ready" or (dep.get("options") or {}).get("replica_of"):
            return False
        minutes = self.idle_minutes(dep)
        if minutes <= 0:
            return False
        last = float(dep.get("ready_at") or 0)
        for inst in self.state.services.instances.values():
            if inst.name == dep["name"]:
                if inst.busy:
                    return False
                last = max(last, float(inst.last_used or 0))
        if any(r.service == dep["name"] and r.status not in ("done", "failed", "cancelled")
               for r in self.state.services.requests.values()):
            return False
        if time.time() - last < minutes * 60:
            return False
        self.stop(dep["id"], reason="unused for %g min" % minutes, status=IDLE)
        return True

    # ------------------------------------------------- extra copies (autoscale)
    def add_replica(self, dep: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Another copy of a model that runs alone on one device, on a device it
        does not use yet; None when no other device can hold it alone."""
        if dep["status"] != "ready":
            return None
        used = set(self.devices_for(dep["id"]))
        for replica in self.replicas_of(dep):
            if replica["status"] in ACTIVE:
                used.update(self.devices_for(replica["id"]))
        candidates = [d["id"] for d in self.devices() if d["eligible"] and d["id"] not in used]
        if not candidates:
            return None
        opts = dep.get("options") or {}
        plan = self.plan(dep["model"], candidates, ctx=opts.get("ctx") or 2048, mode="auto")
        if not plan["ok"] or plan["placement"] != "single":
            return None
        row = {
            "id": new_id(), "name": dep["name"], "model": dep["model"], "model_size": dep["model_size"],
            "host_device": plan["host"], "shards": [],
            "options": {"ctx": plan["ctx"], "threads": opts.get("threads") or 0, "llama": opts.get("llama"),
                        "extra_args": list(opts.get("extra_args") or []), "replica_of": dep["id"],
                        "request": {"mode": "auto", "devices": [plan["host"]], "ctx": plan["ctx"]}},
            "status": "starting", "error": None, "shard_jobs": {}, "head_job": None,
            "endpoints": {}, "head_endpoints": [], "created": now(), "updated": now(), "ready_at": None,
        }
        row.update(self._layout(plan, row["options"]))
        self.state.db.insert("llm_deployments", row)
        self.state.event("llm_copy", "%s is busy: another copy on %s" % (dep["name"], plan["host_name"]))
        self._publish(dep["id"])
        self.wake()
        return self.state.db.get("llm_deployments", row["id"])

    def drop_replica(self, replica: Dict[str, Any], why: str) -> None:
        self._cancel_jobs(replica, include_shards=True)
        self.state.db.execute("DELETE FROM llm_deployments WHERE id=?", (replica["id"],))
        parent = (replica.get("options") or {}).get("replica_of")
        log.info("copy of %s on %s stopped: %s", replica["name"], replica.get("host_device"), why)
        if parent:
            self._publish(parent)

    def _cancel_jobs(self, dep: Dict[str, Any], include_shards: bool) -> None:
        if dep.get("head_job"):
            self.state.scheduler.cancel_job(dep["head_job"], "llm deployment changed")
        if include_shards:
            for job_id in (dep.get("shard_jobs") or {}).values():
                self.state.scheduler.cancel_job(job_id, "llm deployment stopped")

    def _set(self, dep: Dict[str, Any], status: str, error: Optional[str] = None, **extra: Any) -> None:
        fields = {"status": status, "error": error, "updated": now()}
        fields.update(extra)
        self.state.db.update("llm_deployments", {"id": dep["id"]}, fields)
        self._publish(dep["id"])

    def _publish(self, dep_id: str) -> None:
        dep = self.state.db.get("llm_deployments", dep_id)
        if dep is not None:
            self.state.hub.publish({"ev": "llm", "deployment": self.describe(dep)})

    # ------------------------------------------------------------- re-placing
    def _replan(self, dep: Dict[str, Any], exclude: Optional[List[str]] = None,
                no_gpu: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
        """A fresh plan from the deployment's original request."""
        opts = dep.get("options") or {}
        request = opts.get("request") or {}
        mode = request.get("mode") or opts.get("mode") or "manual"
        try:
            if mode == "auto":
                chosen = list(request.get("devices") or [])
                if not chosen:  # "any device": every device we know
                    chosen = [d["id"] for d in self.state.db.select("devices", "WHERE removed=0")]
                allowed = [d for d in chosen if d not in (exclude or [])]
                if not allowed:
                    return None
                return self.plan(dep["model"], allowed, ctx=opts.get("ctx") or 2048, mode="auto", no_gpu=no_gpu)
            devices = request.get("devices") or [s["device_id"] for s in dep.get("shards") or []] \
                or [dep["host_device"]]
            return self.plan(dep["model"], devices, request.get("host") or dep["host_device"],
                             request.get("weights") or None, opts.get("ctx") or 2048, "manual", no_gpu=no_gpu)
        except (KeyError, ValueError) as exc:
            log.warning("re-planning %s failed: %s", dep.get("name"), exc)
            return None

    def _apply(self, dep: Dict[str, Any], plan: Dict[str, Any], why: str) -> None:
        self._cancel_jobs(dep, include_shards=True)
        fields = {"shard_jobs": {}, "head_job": None, "endpoints": {}, "head_endpoints": [],
                  "status": "starting", "error": None, "updated": now()}
        fields.update(self._layout(plan, dep["options"]))
        self.state.db.update("llm_deployments", {"id": dep["id"]}, fields)
        fresh = self.state.db.get("llm_deployments", dep["id"])
        for shard in fresh["shards"]:
            self._submit_shard(fresh, shard)
        message = "%s moved (%s): now %s" % (dep["name"], why, plan.get("summary") or "")
        log.info(message)
        self.state.event("llm_moved", message)
        self.state.notify("llm_moved", message, {"deployment": dep["id"]})
        self._publish(dep["id"])
        self.wake()

    def _used(self, dep: Dict[str, Any]) -> Dict[str, bool]:
        """device id -> whether its graphics card holds layers."""
        opts = dep.get("options") or {}
        used: Dict[str, bool] = {}
        if opts.get("placement") == "single":
            used[dep["host_device"]] = bool(opts.get("gpu_layers"))
        else:
            used[dep["host_device"]] = False
            for shard in dep.get("shards") or []:
                used[shard["device_id"]] = used.get(shard["device_id"], False) or bool(shard.get("gpu"))
        return used

    def _guard(self, dep: Dict[str, Any]) -> bool:
        """Keep the model on devices that may hold it.  True: handled, stop this step."""
        trouble: Dict[str, str] = {}
        hot_gpu: List[str] = []
        for device_id, on_gpu in self._used(dep).items():
            device = self.state.db.get("devices", device_id)
            if device is None or device.get("removed"):
                trouble[device_id] = "removed"
                continue
            status = self.state.safety.check(device)
            name = device.get("name") or device_id
            if not status["online"]:
                trouble[device_id] = "%s is offline" % name
            elif not status["ai_keep"]:
                trouble[device_id] = "%s: %s" % (name, "; ".join(status["notes"]) or "resting")
            elif on_gpu and not status["gpu_keep"]:
                hot_gpu.append(device_id)
        mode = (dep.get("options") or {}).get("mode") or "manual"
        if not trouble and not hot_gpu:
            self._replan_at.pop(dep["id"], None)
            return False
        if (dep.get("options") or {}).get("replica_of"):
            # an extra copy just goes away; the autoscaler adds one elsewhere if needed
            self.drop_replica(dep, "; ".join(list(trouble.values()) + ["GPU too hot" for _ in hot_gpu]))
            return True
        if mode != "auto" and not hot_gpu:
            return False  # a manual model waits for its devices (degraded)
        stamp = time.time()
        if stamp - self._replan_at.get(dep["id"], 0.0) < REPLAN_EVERY:
            return dep["status"] == "degraded"
        self._replan_at[dep["id"]] = stamp
        exclude = list(trouble) if mode == "auto" else []
        plan = self._replan(dep, exclude=exclude, no_gpu=hot_gpu)
        why = "; ".join(list(trouble.values()) + ["GPU too hot" for _ in hot_gpu])
        if plan is not None and plan["ok"]:
            self._apply(dep, plan, why)
            return True
        if dep["status"] != "degraded":
            self._cancel_jobs(dep, include_shards=False)
            self.state.db.update("llm_deployments", {"id": dep["id"]}, {"head_job": None})
            self._set(dep, "degraded", error="waiting: %s; no other placement fits" % why)
            self.state.event("llm_degraded", "%s paused: %s" % (dep["name"], why))
        return True

    # ----------------------------------------------------------- reconcile
    def _shard_instance(self, dep: Dict[str, Any], device_id: str) -> Any:
        name = "_llm.%s.%s" % (dep["id"], device_id)
        for inst in self.state.services.instances.values():
            if inst.name == name and inst.device_id == device_id and inst.ready:
                return inst
        return None

    def _head_instance(self, dep: Dict[str, Any]) -> Any:
        for inst in self.state.services.instances.values():
            if inst.job_id == dep.get("head_job") and inst.name == dep["name"]:
                return inst
        return None

    def reconcile(self) -> None:
        for dep in self.deployments(active_only=True, replicas=True):
            try:
                parent_id = (dep.get("options") or {}).get("replica_of")
                if parent_id:
                    parent = self.state.db.get("llm_deployments", parent_id)
                    if parent is None or parent["status"] not in ACTIVE:
                        self.drop_replica(dep, "its model is not running")
                        continue
                if self._unload_idle(dep):
                    continue
                self._step(dep)
            except Exception:
                log.exception("llm deployment %s step failed", dep.get("name"))

    def _job_status(self, job_id: Optional[str]) -> Optional[str]:
        if not job_id:
            return None
        job = self.state.db.get("jobs", job_id)
        return job["status"] if job else None

    def _job_error(self, job_id: Optional[str]) -> str:
        job = self.state.db.get("jobs", job_id) if job_id else None
        return str((job or {}).get("error") or "failed")

    def _step(self, dep: Dict[str, Any]) -> None:
        # 0. devices that may no longer hold it: move it (auto) or let it wait
        if self._guard(dep):
            return
        # 1. shard jobs must stay alive (they only end when cancelled or out of retries)
        for shard in dep["shards"]:
            job_id = (dep["shard_jobs"] or {}).get(shard["device_id"])
            status = self._job_status(job_id)
            if status in ("failed", "done", "cancelled"):
                device = self.state.db.get("devices", shard["device_id"]) or {}
                reason = "shard on %s %s: %s" % (device.get("name") or shard["device_id"], status,
                                                   self._job_error(job_id))
                self._cancel_jobs(dep, include_shards=True)
                self._set(dep, "failed", error=reason)
                self.state.event("llm_failed", "%s failed: %s" % (dep["name"], reason))
                return
        # 2. which shards are listening, and where
        endpoints: Dict[str, str] = {}
        for shard in dep["shards"]:
            inst = self._shard_instance(dep, shard["device_id"])
            endpoint = (inst.meta or {}).get("endpoint") if inst is not None else None
            if endpoint:
                endpoints[shard["device_id"]] = endpoint
        if endpoints != (dep.get("endpoints") or {}):
            self.state.db.update("llm_deployments", {"id": dep["id"]}, {"endpoints": endpoints})
            dep["endpoints"] = endpoints
            self._publish(dep["id"])
        all_ready = len(endpoints) == len(dep["shards"])
        ordered = [endpoints.get(s["device_id"]) for s in dep["shards"]]
        head_status = self._job_status(dep.get("head_job"))
        head = self._head_instance(dep)

        # 3. a head pointed at stale addresses (a shard restarted) is replaced
        if dep.get("head_job") and head_status in ("pending", "running") and all_ready \
                and ordered != list(dep.get("head_endpoints") or []):
            log.info("%s: shard addresses changed, restarting the head", dep["name"])
            self._cancel_jobs(dep, include_shards=False)
            self.state.db.update("llm_deployments", {"id": dep["id"]}, {"head_job": None})
            dep["head_job"] = None
            head_status = None
            head = None

        if head_status == "failed":
            reason = "model host failed: %s" % self._job_error(dep["head_job"])
            self._cancel_jobs(dep, include_shards=True)
            self._set(dep, "failed", error=reason)
            self.state.event("llm_failed", "%s failed: %s" % (dep["name"], reason))
            return

        if not all_ready:
            if dep["status"] in ("loading", "ready"):
                missing = [s["device_id"] for s in dep["shards"] if s["device_id"] not in endpoints]
                names = [(self.state.db.get("devices", d) or {}).get("name") or d for d in missing]
                self._cancel_jobs(dep, include_shards=False)
                self.state.db.update("llm_deployments", {"id": dep["id"]}, {"head_job": None})
                self._set(dep, "degraded", error="waiting for %s" % ", ".join(names))
                self.state.event("llm_degraded", "%s paused: waiting for %s" % (dep["name"], ", ".join(names)))
            return

        if not dep.get("head_job") or head_status in (None, "cancelled", "done"):
            self._submit_head(dep, ordered)
            self._set(dep, "loading")
            return

        if head is not None and head.ready and dep["status"] != "ready":
            self._set(dep, "ready", ready_at=now(), error=None)
            if (dep.get("options") or {}).get("replica_of"):
                return  # an extra copy: no announcement
            self.state.event("llm_ready", "%s is ready (%s)"
                             % (dep["name"], (dep.get("options") or {}).get("summary") or dep["model"]))
            if not (dep.get("options") or {}).get("on_demand"):
                self.state.notify("llm_ready", "Model ready: %s" % dep["name"], {"deployment": dep["id"]})
        elif head is None and dep["status"] == "ready":
            self._set(dep, "loading")
