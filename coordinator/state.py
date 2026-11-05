"""``ClusterState`` -- everything the request handlers share.

Owns the config, the database, the file store, the plugin registry, the
scheduler, pooled storage, terminals, notifications, schedules and the
per-device command queues (SPEC §9).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import VERSION
from .config import EDITABLE_SECTIONS, Config, data_dir as resolve_data_dir, ensure_dirs, load_config, root_dir
from .db import Database, new_id, now
from .files import FileStore
from .hub import Hub
from .notify import Notifier
from .plugins import Registry, build_registry
from .autoscale import Autoscaler
from .cache import ResultCache
from .peers import PeerTransfers
from .policy import Safety
from .security import Security
from .verify import Verifier
from .speeds import SpeedMemory
from .scheduler import Scheduler
from .schedules import CronError, ScheduleRunner, next_run
from .cleanup import Cleanup
from .discovery import DiscoveryService
from .downloads import Downloads
from .llm import LLMManager
from .results import ResultStream
from .services import ServiceRegistry
from .storage import StorageManager
from .screen import ScreenManager
from .terminal import TerminalManager
from .updates import WorkerUpdates

log = logging.getLogger("coordinator.state")

MAX_QUEUE = 500
LOG_TAIL_DEFAULT = 500


class ClusterState:
    """Shared application state; one instance per :func:`create_app`."""

    def __init__(self, data_directory: Optional[Path] = None) -> None:
        self.version = VERSION
        self.root: Path = root_dir()
        self.data: Path = resolve_data_dir(data_directory)
        ensure_dirs(self.data)
        self.config: Config = load_config(self.data)
        self.db = Database(self.data / "cluster.db")
        self._apply_saved_settings()
        self.files = FileStore(self.db, self.data / "files")
        self.downloads = Downloads(self.data)   # the dashboard's Save buttons write here
        self.plugins: Registry = build_registry(self.data / "plugins")
        self.hub = Hub(self)
        self.terminals = TerminalManager(self)
        self.screens = ScreenManager(self)
        self.updates = WorkerUpdates(self)   # workers that update themselves (SPEC 8.4)
        self.notifier = Notifier(self)
        self.storage = StorageManager(self)
        self.results = ResultStream(self)
        self.discovery = DiscoveryService(self)
        self.services = ServiceRegistry(self)
        self.safety = Safety(self)
        self.speeds = SpeedMemory(self)
        self.security = Security(self)
        self.cache = ResultCache(self)
        self.verifier = Verifier(self)
        self.peers = PeerTransfers(self)
        self.llm = LLMManager(self)
        self.autoscaler = Autoscaler(self)
        self.scheduler = Scheduler(self)
        self.schedules = ScheduleRunner(self)
        self.cleanup = Cleanup(self)   # only final results stay (SPEC 9.10)
        self.queues: Dict[str, asyncio.Queue] = {}
        self.started_at = time.time()
        self.port: int = self.config.port
        self.log = logging.getLogger("coordinator.state")

    # ------------------------------------------------------------ lifecycle
    async def startup(self) -> None:
        await asyncio.to_thread(self.security.ensure_tls)  # the cluster's own certificates (SPEC 3)
        self.db.execute("UPDATE devices SET status='offline' WHERE removed=0")
        await self.hub.start()
        await self.scheduler.start()
        await self.storage.start()
        await self.schedules.start()
        await self.discovery.start()
        await self.llm.start()
        await self.cleanup.start()
        self.log.info(
            "coordinator ready: %d plugin(s), data dir %s", len(self.plugins), self.data
        )

    async def shutdown(self) -> None:
        await self.cleanup.stop()
        await self.llm.close()
        await self.discovery.stop()
        await self.schedules.stop()
        await self.storage.stop()
        await self.scheduler.stop()
        await self.hub.stop()
        await asyncio.to_thread(self.screens.close)
        self.db.close()

    # -------------------------------------------------------------- config
    def _apply_saved_settings(self) -> None:
        """``config.yaml`` is the startup source; the settings table overrides it."""
        for key in EDITABLE_SECTIONS:
            saved = self.db.get_setting(key)
            if isinstance(saved, dict):
                merged = dict(self.config.get(key) or {})
                merged.update(saved)
                self.config.set(key, merged)
        pin = self.db.get_setting("pin")
        if isinstance(pin, str) and pin:
            self.config.set("pin", pin)

    def setting(self, dotted: str, default: Any = None) -> Any:
        value = self.config.get(dotted, None)
        return default if value is None else value

    def save_settings(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Apply ``PATCH /api/settings`` to memory, config.yaml and the DB."""
        for key in EDITABLE_SECTIONS:
            section = patch.get(key)
            if not isinstance(section, dict):
                continue
            merged = dict(self.config.get(key) or {})
            merged.update(section)
            self.config.set(key, merged)
            self.db.set_setting(key, merged)
        pin = patch.get("pin")
        if isinstance(pin, str) and pin.strip():
            self.config.set("pin", pin.strip())
            self.db.set_setting("pin", pin.strip())
        self.config.save()
        return self.settings_view()

    def settings_view(self) -> Dict[str, Any]:
        return {
            "scheduler": dict(self.config.get("scheduler") or {}),
            "storage": dict(self.config.get("storage") or {}),
            "notifications": dict(self.config.get("notifications") or {}),
            "llm": dict(self.config.get("llm") or {}),
            "services": dict(self.config.get("services") or {}),
            "cache": dict(self.config.get("cache") or {}),
            "verify": dict(self.config.get("verify") or {}),
            "transfers": dict(self.config.get("transfers") or {}),
            "security": dict(self.config.get("security") or {}),
            "cleanup": dict(self.config.get("cleanup") or {}),
            "pin": self.config.pin,
            "port": self.config.port,
            "local_port": self.config.local_port,
            "local_worker": bool(self.config.get("local_worker", True)),
            "poll_timeout": self.config.poll_timeout,
            "offline_after": self.config.offline_after,
            "version": self.version,
        }

    # ------------------------------------------------------ command queues
    def queue_for(self, device_id: str) -> asyncio.Queue:
        queue = self.queues.get(device_id)
        if queue is None:
            queue = asyncio.Queue(maxsize=MAX_QUEUE)
            self.queues[device_id] = queue
        return queue

    def send_command(self, device_id: str, command: Dict[str, Any]) -> bool:
        """Push a command into a device's queue (SPEC §8/§9)."""
        try:
            queue = self.queue_for(str(device_id))
            queue.put_nowait(command)
            return True
        except asyncio.QueueFull:  # pragma: no cover - a stuck worker
            self.log.warning("command queue for %s is full, dropping %s",
                             device_id, command.get("cmd"))
        except RuntimeError:  # pragma: no cover - outside the loop
            self.log.debug("no event loop for send_command")
        return False

    def drop_queue(self, device_id: str) -> None:
        self.queues.pop(str(device_id), None)

    # -------------------------------------------------------------- events
    def event(
        self,
        kind: str,
        message: str = "",
        *,
        device_id: Optional[str] = None,
        job_id: Optional[str] = None,
        task_id: Optional[str] = None,
        data: Optional[Any] = None,
    ) -> Dict[str, Any]:
        row = self.db.add_event(
            kind, message, device_id=device_id, job_id=job_id, task_id=task_id, data=data
        )
        self.hub.publish({"ev": "event", "event": row})
        return row

    def notify(self, kind: str, message: str, data: Optional[Dict[str, Any]] = None) -> None:
        self.notifier.fire_soon(kind, message, data)

    def publish_device(self, device_id: str, removed: bool = False) -> None:
        device = self.db.get("devices", device_id)
        if device is None:
            return
        if removed:
            device["removed"] = 1
        device.pop("token_hash", None)
        device["control"] = self.safety.view(device)
        device["trust"] = self.verifier.view(device)
        device["update"] = self.updates.view(device)
        self.hub.publish({"ev": "device", "device": device})

    def publish_job(self, job_id: str) -> None:
        job = self.db.get("jobs", job_id)
        if job is not None:
            self.hub.publish({"ev": "job", "job": job})

    def publish_task(self, task_id: str) -> None:
        task = self.db.get("tasks", task_id)
        if task is not None:
            self.services.on_task_update(task)
            self.hub.publish({"ev": "task", "task": _slim_task(task)})

    # ------------------------------------------------------------- devices
    def online_devices(self) -> List[Dict[str, Any]]:
        return self.db.select("devices", "WHERE removed=0 AND status='online'")

    def set_bench_score(
        self, device_id: str, score: float, bench: Optional[Dict[str, Any]] = None
    ) -> None:
        self.db.update(
            "devices",
            {"id": device_id},
            {"bench_score": float(score), "bench": bench or {"score": float(score)}},
        )
        self.publish_device(device_id)

    # ---------------------------------------------------------- task logs
    def task_log_path(self, task_id: str) -> Path:
        safe = "".join(ch for ch in str(task_id) if ch.isalnum() or ch in "-_")
        directory = self.data / "logs" / "tasks"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / ("%s.log" % (safe or "unknown"))

    def append_task_log(self, task_id: str, lines: List[str]) -> int:
        if not lines:
            return 0
        path = self.task_log_path(task_id)
        with path.open("a", encoding="utf-8", errors="replace", newline="\n") as handle:
            for line in lines:
                handle.write(str(line).rstrip("\r\n") + "\n")
        return len(lines)

    def read_task_log(self, task_id: str, tail: Optional[int] = None) -> List[str]:
        path = self.task_log_path(task_id)
        if not path.is_file():
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        limit = LOG_TAIL_DEFAULT if tail is None else int(tail)
        if limit and limit > 0:
            lines = lines[-limit:]
        return lines

    # ----------------------------------------------------------------- jobs
    def effective_options(self, plugin: Any, options: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        merged: Dict[str, Any] = {
            "timeout": self.setting("scheduler.default_timeout", 600),
            "max_attempts": self.setting("scheduler.default_max_attempts", 3),
            "requires": [],
            "targets": "any",
            "fail_fast": False,
        }
        for key, value in (plugin.default_options or {}).items():
            if value is not None:
                merged[key] = value
        for key, value in (options or {}).items():
            if value is not None:
                merged[key] = value
        return merged

    def submit_job(
        self,
        spec: Dict[str, Any],
        *,
        pipeline_id: Optional[str] = None,
        job_id: Optional[str] = None,
        keymap: Optional[Dict[str, str]] = None,
        source: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create a job row from a job spec (SPEC §5).  Raises ``KeyError``/``ValueError``."""
        job_type = str(spec.get("type") or "").strip()
        plugin = self.plugins.get(job_type)
        if plugin is None:
            raise KeyError("unknown job type %r" % job_type)
        job_id = job_id or new_id()
        params = dict(plugin.defaults())
        params.update(dict(spec.get("params") or {}))
        options = self.effective_options(plugin, spec.get("options"))
        if keymap:
            options["_pipeline_keys"] = dict(keymap)
        if source:
            options.update({"_source": source})
        depends_on = [str(d) for d in (spec.get("depends_on") or [])]
        name = str(spec.get("name") or "%s %s" % (job_type, job_id[:6]))
        row = {
            "id": job_id,
            "name": name,
            "type": job_type,
            "params": params,
            "status": "waiting" if depends_on else "pending",
            "priority": int(spec.get("priority") or 0),
            "depends_on": depends_on,
            "pipeline_id": pipeline_id,
            "template_id": spec.get("template_id"),
            "created": now(),
            "started": None,
            "finished": None,
            "total_tasks": 0,
            "done_tasks": 0,
            "failed_tasks": 0,
            "result": None,
            "error": None,
            "options": options,
        }
        self.db.insert("jobs", row)
        self.event("job_submitted", "%s submitted" % name, job_id=job_id)
        self.publish_job(job_id)
        self.scheduler.wake()
        return row

    def rerun_job(self, job_id: str, fresh: bool = False) -> Dict[str, Any]:
        """Submit a job again with the same spec.  ``fresh`` ignores stored
        results and replaces them with the new ones ("Run again fresh", SPEC §9.8)."""
        job = self.db.get("jobs", job_id)
        if job is None:
            raise KeyError("unknown job %r" % job_id)
        options = {k: v for k, v in (job.get("options") or {}).items() if not str(k).startswith("_")}
        if fresh:
            options["cache"] = "refresh"
        elif options.get("cache") == "refresh":
            options.pop("cache")
        spec = {"name": job.get("name"), "type": job["type"], "params": job.get("params") or {},
                "priority": job.get("priority") or 0, "options": options}
        return self.submit_job(spec, source={"rerun_of": job_id, "fresh": bool(fresh)})

    def submit_pipeline(self, name: Optional[str], specs: List[Dict[str, Any]]) -> Dict[str, Any]:
        """SPEC §5.1 -- submit several dependent jobs at once."""
        if not specs:
            raise ValueError("a pipeline needs at least one job")
        pipeline_id = new_id()
        keymap: Dict[str, str] = {}
        ids: List[str] = []
        for index, spec in enumerate(specs):
            job_id = new_id()
            ids.append(job_id)
            key = str(spec.get("key") or index)
            keymap[key] = job_id
        jobs: Dict[str, str] = {}
        for index, spec in enumerate(specs):
            key = str(spec.get("key") or index)
            body = dict(spec)
            body.pop("key", None)
            if name and not body.get("name"):
                body["name"] = "%s / %s" % (name, key)
            self.submit_job(
                body,
                pipeline_id=pipeline_id,
                job_id=keymap[key],
                keymap=keymap,
            )
            jobs[key] = keymap[key]
        return {"pipeline_id": pipeline_id, "jobs": jobs}

    # ---------------------------------------------------------------- stats
    def stats(self) -> Dict[str, Any]:
        devices = self.db.select("devices", "WHERE removed=0")
        online = [d for d in devices if d.get("status") == "online"]
        busy = {
            row["device_id"]: int(row["n"])
            for row in self.db.rows(
                "SELECT device_id, COUNT(*) AS n FROM tasks WHERE status IN "
                "('assigned','running') AND device_id IS NOT NULL GROUP BY device_id"
            )
        }
        cores = 0
        slots_free = 0
        for device in online:
            info = device.get("info") or {}
            try:
                cores += int(info.get("cpu_count") or 0)
            except (TypeError, ValueError):
                pass
            if not device.get("paused"):
                slots_free += max(
                    0, int(device.get("slots") or 1) - busy.get(device["id"], 0)
                )
        stamp = now()
        day_ago = stamp - 86400
        done_counts = {
            row["status"]: int(row["n"])
            for row in self.db.rows(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
            )
        }
        tasks_recent = int(
            self.db.scalar(
                "SELECT COUNT(*) FROM tasks WHERE finished_at IS NOT NULL AND finished_at > ?",
                (stamp - 300,),
                default=0,
            )
        )
        leaderboard = []
        done_per_device = {
            row["device_id"]: int(row["n"])
            for row in self.db.rows(
                "SELECT device_id, COUNT(*) AS n FROM tasks WHERE status='done' "
                "AND device_id IS NOT NULL GROUP BY device_id"
            )
        }
        for device in sorted(
            devices,
            key=lambda d: (d.get("bench_score") is None, -(d.get("bench_score") or 0)),
        ):
            leaderboard.append(
                {
                    "device_id": device["id"],
                    "name": device.get("name"),
                    "bench_score": device.get("bench_score"),
                    "tasks_done": done_per_device.get(device["id"], 0),
                }
            )
        return {
            "devices": {
                "online": len(online),
                "total": len(devices),
                "cores": cores,
                "slots_free": slots_free,
            },
            "jobs": {
                "running": done_counts.get("running", 0),
                "pending": done_counts.get("pending", 0) + done_counts.get("waiting", 0),
                "done_24h": int(
                    self.db.scalar(
                        "SELECT COUNT(*) FROM jobs WHERE status='done' AND finished > ?",
                        (day_ago,),
                        default=0,
                    )
                ),
                "failed_24h": int(
                    self.db.scalar(
                        "SELECT COUNT(*) FROM jobs WHERE status='failed' AND finished > ?",
                        (day_ago,),
                        default=0,
                    )
                ),
            },
            "tasks_per_min": round(tasks_recent / 5.0, 2),
            "leaderboard": leaderboard[:10],
            "storage": {
                "blobs": int(self.db.scalar("SELECT COUNT(*) FROM blobs", default=0)),
                "files": int(self.db.scalar("SELECT COUNT(*) FROM files", default=0)),
            },
            "time": stamp,
        }

    # ------------------------------------------------------------ networking
    def lan_ips(self) -> List[str]:
        """LAN IPv4 addresses, excluding loopback and link-local (SPEC §11)."""
        found: List[str] = []
        try:
            import psutil

            for addrs in psutil.net_if_addrs().values():
                for addr in addrs:
                    if getattr(addr, "family", None) != socket.AF_INET:
                        continue
                    found.append(str(addr.address))
        except Exception:  # pragma: no cover - psutil missing/blocked
            self.log.debug("psutil interface scan failed", exc_info=True)
        if not found:  # pragma: no cover - fallback
            try:
                for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                    found.append(str(info[4][0]))
            except OSError:
                pass
        out: List[str] = []
        for raw in found:
            try:
                addr = ipaddress.IPv4Address(raw)
            except ValueError:
                continue
            if addr.is_loopback or addr.is_link_local or addr.is_unspecified:
                continue
            if str(addr) not in out:
                out.append(str(addr))
        # The interface holding the default route is the one phones on the
        # same Wi-Fi can reach; VirtualBox / hotspot adapters come after it.
        primary = self._default_route_ip()
        out.sort(key=lambda ip: (ip != primary, not ip.startswith("192.168."), ip))
        return out

    @staticmethod
    def _default_route_ip() -> Optional[str]:
        """IP of the interface with the default route (no packets are sent)."""
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(("10.255.255.255", 1))
            return str(sock.getsockname()[0])
        except OSError:
            return None
        finally:
            if sock is not None:
                sock.close()

    def urls(self) -> List[str]:
        """The LAN door: HTTPS on every LAN address (SPEC 3)."""
        port = self.port or self.config.port
        return ["https://%s:%d" % (ip, port) for ip in self.lan_ips()]

    def primary_url(self) -> str:
        urls = self.urls()
        return urls[0] if urls else "https://127.0.0.1:%d" % (self.port or self.config.port)

    def local_url(self) -> str:
        """The PC-only door: plain HTTP on 127.0.0.1 (dashboard, CLI, SDK)."""
        return "http://127.0.0.1:%d" % self.config.local_port

    # ------------------------------------------------------------ schedules
    def create_schedule(
        self, name: str, cron: str, job_spec: Dict[str, Any], enabled: bool = True
    ) -> Dict[str, Any]:
        expr = str(cron).strip()
        try:
            nxt = next_run(expr) if enabled else None
        except CronError as exc:
            raise ValueError(str(exc))
        row = {
            "id": new_id(),
            "name": name,
            "cron": expr,
            "job_spec": job_spec,
            "enabled": 1 if enabled else 0,
            "last_run": None,
            "next_run": nxt,
            "created": now(),
        }
        self.db.insert("schedules", row)
        return row


def _slim_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """Task dict without the (potentially large) payload, for live updates."""
    out = dict(task)
    out.pop("payload", None)
    return out
