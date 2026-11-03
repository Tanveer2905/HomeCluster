"""Configuration handling for the PhoneCluster coordinator (SPEC §12).

Paths are derived from the environment:

* ``PC_ROOT``     -- project root, default: parent directory of this package.
* ``PC_DATA_DIR`` -- runtime data directory, default ``<PC_ROOT>/data``.
"""

from __future__ import annotations

import copy
import logging
import os
import secrets
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Union

import yaml

log = logging.getLogger("coordinator.config")

PathLike = Union[str, "os.PathLike[str]", Path]

DEFAULT_PORT = 8765

#: Defaults written to ``data/config.yaml`` on first start (SPEC §12).
DEFAULTS: Dict[str, Any] = {
    "port": DEFAULT_PORT,     # the LAN door: HTTPS only, for phones (SPEC 3)
    "host": "0.0.0.0",
    "local_port": 8764,       # plain HTTP on 127.0.0.1 only: dashboard, CLI, SDK, the PC worker
    "pin": "",  # generated on first start
    "local_worker": True,
    "local_worker_slots": 0,  # 0 = every core but one (the CPU limit per device still applies)
    "offline_after": 30,
    "poll_timeout": 25,
    "discovery": True,        # answer UDP probes so workers find us without an IP
    "discovery_port": 8766,
    "scheduler": {
        # Balanced safety profile (coordinator/policy.py); these can only make it stricter
        "min_battery": 20,
        "max_temp": 40.0,
        "resume_temp": 36.0,
        "default_timeout": 600,
        "default_max_attempts": 3,
        # requests first (SPEC 9.7): devices answering a service call pause their tasks
        "requests_first": True,
        "hold_seconds": 120,
    },
    "llm": {
        "idle_minutes": 10,     # unload a model nobody used this long (0 = never)
        "load_wait": 900,       # how long a call waits for a model it started
        "default_model": None,  # what "chat" reaches
    },
    "services": {
        "autoscale": True,      # start another copy of a busy service
        "max_copies": 3,
        "scale_up_after": 2,    # seconds a request waited
        "scale_down_minutes": 3,
    },
    "storage": {
        "chunk_mb": 4,
        "replication": 2,
        "cache_mb": 512,
        "include_pc": False,
        # "auto": erasure coding (parity pieces) once 3+ phones store data, else copies
        "scheme": "auto",
        "scrub_hours": 24,      # every stored piece is re-hashed on its phone this often
    },
    # never compute the same thing twice (SPEC 9.8)
    "cache": {
        "enabled": True,
        "max_mb": 256,          # stored results, least recently used go first
        "max_result_kb": 4096,  # bigger results are not kept
    },
    # catching wrong answers (SPEC 9.9)
    "verify": {
        "share": 0.05,          # tasks run a second time on another device
        "suspect_share": 0.5,   # ... on a device that gave a wrong answer lately
        "recheck_minutes": 60,  # a wrong device's work this recent is done again
        "strikes": 3,           # wrong answers within a day before it gets no jobs
    },
    # keeping outsiders out (SPEC 3)
    "security": {
        "join_minutes": 10,     # a join code works once, for this long
        "extra_hosts": [],      # other names this PC may be reached by (normally none)
    },
    # phone-to-phone transfers (SPEC 8.3)
    "transfers": {
        "peer": True,
        "min_mb": 4,            # smaller inputs always come from the coordinator
    },
    "notifications": {
        "desktop": True,
        "phone": False,
        "webhook_url": None,
        "on": ["job_done", "job_failed", "device_offline", "device_joined"],
    },
    "log_level": "info",
}

#: Keys that ``PATCH /api/settings`` may touch (plus ``pin``).
EDITABLE_SECTIONS = ("scheduler", "storage", "notifications", "llm", "services",
                     "cache", "verify", "transfers", "security")


def root_dir() -> Path:
    """Project root (the folder that holds ``coordinator/``, unless ``PC_ROOT`` says otherwise)."""
    env = os.environ.get("PC_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def data_dir(explicit: Optional[PathLike] = None) -> Path:
    """Runtime data directory."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("PC_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return root_dir() / "data"


def generate_pin() -> str:
    """Six random digits."""
    return "".join(secrets.choice("0123456789") for _ in range(6))


def deep_merge(base: Mapping[str, Any], patch: Mapping[str, Any]) -> Dict[str, Any]:
    """Recursively merge ``patch`` into a copy of ``base``."""
    out: Dict[str, Any] = dict(copy.deepcopy(dict(base)))
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Config:
    """The contents of ``data/config.yaml`` with dotted access helpers."""

    def __init__(self, path: Path, data: Dict[str, Any]) -> None:
        self.path = path
        self.data: Dict[str, Any] = data

    # ------------------------------------------------------------------ access
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node: Dict[str, Any] = self.data
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = value

    def merge(self, patch: Mapping[str, Any]) -> None:
        self.data = deep_merge(self.data, patch)

    def to_dict(self) -> Dict[str, Any]:
        return copy.deepcopy(self.data)

    # --------------------------------------------------------------- shortcuts
    @property
    def pin(self) -> str:
        return str(self.data.get("pin") or "")

    @property
    def port(self) -> int:
        return int(self.data.get("port") or DEFAULT_PORT)

    @property
    def local_port(self) -> int:
        return int(self.data.get("local_port") or 8764)

    @property
    def host(self) -> str:
        return str(self.data.get("host") or "0.0.0.0")

    @property
    def poll_timeout(self) -> float:
        return float(self.data.get("poll_timeout") or 25)

    @property
    def offline_after(self) -> float:
        return float(self.data.get("offline_after") or 30)

    @property
    def log_level(self) -> str:
        return str(self.data.get("log_level") or "info")

    # ------------------------------------------------------------------- write
    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".yaml.tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            yaml.safe_dump(self.data, fh, sort_keys=False, allow_unicode=True)
        tmp.replace(self.path)


def load_config(directory: Path) -> Config:
    """Load ``<directory>/config.yaml``, creating it with defaults if missing."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.yaml"
    raw: Dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except Exception:  # pragma: no cover - corrupt config
            log.exception("config.yaml is unreadable, falling back to defaults")
    merged = deep_merge(DEFAULTS, raw)
    created = not path.is_file()
    if not merged.get("pin"):
        merged["pin"] = generate_pin()
        created = True
    cfg = Config(path, merged)
    if created or merged != raw:
        cfg.save()
    return cfg


def ensure_dirs(directory: Path) -> None:
    """Create the runtime directory tree (SPEC §1)."""
    for sub in (
        "",
        "files",
        "storage/cache",
        "logs/tasks",
        "worker",
        "plugins",
        "tmp",
        "tls",
    ):
        (directory / sub).mkdir(parents=True, exist_ok=True)


def iter_editable(sections: Iterable[str] = EDITABLE_SECTIONS) -> Iterable[str]:
    return sections
