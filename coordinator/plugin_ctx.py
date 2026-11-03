"""The ``ctx`` object handed to ``plugin.split()`` / ``plugin.reduce()``.

SPEC §7 requires ``ctx.devices()`` and ``ctx.file_meta(id)``; the extra helpers
below are documented in ``docs/DEVIATIONS.md`` (they only add capabilities).
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("coordinator.plugin_ctx")


class PluginContext:
    """Read/write access to the cluster for a plugin's split/reduce."""

    def __init__(self, state: Any, job: Dict[str, Any], options: Dict[str, Any]) -> None:
        self.state = state
        self.job = job
        self.options = options

    # ------------------------------------------------------------- devices
    def devices(self, role: str = "jobs") -> List[Dict[str, Any]]:
        """Online, non-paused, non-removed devices with ``role`` switched on
        (``jobs``, or ``ai`` for models and services), fastest first (SPEC §7/§9.7)."""
        from . import policy

        rows = self.state.db.select(
            "devices",
            "WHERE removed=0 AND status='online' AND paused=0 ORDER BY "
            "COALESCE(bench_score, -1) DESC, created ASC",
        )
        return [d for d in rows if policy.role_on(d, role)]

    def all_devices(self) -> List[Dict[str, Any]]:
        return self.state.db.select("devices", "WHERE removed=0 ORDER BY created ASC")

    def device(self, device_id: str) -> Optional[Dict[str, Any]]:
        return self.state.db.get("devices", device_id)

    def set_bench(self, device_id: str, score: float, bench: Optional[Dict[str, Any]] = None) -> None:
        """Record a benchmark result (used by the ``benchmark`` plugin)."""
        self.state.set_bench_score(device_id, score, bench)

    # --------------------------------------------------------------- files
    def file_meta(self, file_id: str) -> Optional[Dict[str, Any]]:
        return self.state.files.meta(file_id)

    def file_path(self, file_id: str) -> Path:
        return self.state.files.path_for(file_id)

    def read_file(self, file_id: str) -> bytes:
        return self.state.files.read(file_id)

    def read_json(self, file_id: str) -> Any:
        return json.loads(self.read_file(file_id).decode("utf-8"))

    def line_ranges(self, file_id: str, lines_per_range: int) -> List[Tuple[int, int, int]]:
        """``(offset, length, first_line)`` slices of a text file (SPEC §6.6)."""
        return self.state.files.line_ranges(file_id, lines_per_range)

    def add_file(self, data: bytes, name: str = "output.bin") -> Dict[str, Any]:
        return self.state.files.add_bytes(data, name)

    def add_file_path(self, path: Path, name: Optional[str] = None) -> Dict[str, Any]:
        return self.state.files.add_path(Path(path), name)

    # ---------------------------------------------------------------- misc
    @property
    def root(self) -> Path:
        return self.state.root

    @property
    def data_dir(self) -> Path:
        return self.state.data

    def tmp_dir(self) -> Path:
        path = self.state.data / "tmp"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def ffmpeg(self) -> Optional[str]:
        """Path to the coordinator-side ffmpeg (``tools/ffmpeg``), else PATH."""
        for name in ("ffmpeg.exe", "ffmpeg"):
            candidate = self.state.root / "tools" / "ffmpeg" / name
            if candidate.is_file():
                return str(candidate)
        return shutil.which("ffmpeg")

    def ffprobe(self) -> Optional[str]:
        for name in ("ffprobe.exe", "ffprobe"):
            candidate = self.state.root / "tools" / "ffmpeg" / name
            if candidate.is_file():
                return str(candidate)
        return shutil.which("ffprobe")

    def config(self, dotted: str, default: Any = None) -> Any:
        return self.state.setting(dotted, default)
