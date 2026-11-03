"""Streamed job results (SPEC §6.6).

With ``options.stream_results`` a job's per-item results are appended to
disk as each task finishes instead of being collected into one in-memory
list at the end:

    data/results/<job_id>/<idx>.jsonl      one part per task, JSON per line
    data/results/<job_id>.jsonl            final file (parts joined in idx order)

The final file is registered in the file store, and the job's ``result``
becomes ``{"results_file": <file_id>, "name": ..., "count": N}``.
``GET /api/jobs/{id}/results`` streams whatever exists so far while the job
runs, and the finished file afterwards.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

log = logging.getLogger("coordinator.results")


class ResultStream:
    def __init__(self, state: Any) -> None:
        self.state = state
        self.root = Path(state.data) / "results"

    # -------------------------------------------------------------- helpers
    def enabled(self, job: Dict[str, Any]) -> bool:
        return bool((job.get("options") or {}).get("stream_results"))

    def parts_dir(self, job_id: str) -> Path:
        return self.root / str(job_id)

    def part_path(self, job_id: str, idx: int) -> Path:
        return self.parts_dir(job_id) / ("%06d.jsonl" % int(idx))

    @staticmethod
    def lines_of(plugin: Any, result: Any) -> Iterable[Any]:
        """Items a task result contributes; plugins may override ``stream_lines``."""
        hook = getattr(getattr(plugin, "module", None), "stream_lines", None)
        if callable(hook):
            return hook(result)
        if isinstance(result, list):
            return result
        return [] if result is None else [result]

    # -------------------------------------------------------------- writing
    def write_part(self, job: Dict[str, Any], idx: int, result: Any) -> int:
        """Append one task's items as a part file; returns the item count."""
        plugin = self.state.plugins.get(str(job.get("type")))
        path = self.part_path(job["id"], idx)
        path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        tmp = path.with_suffix(".tmp")
        with tmp.open("wb") as fh:  # binary: keep "\n" line ends on Windows too
            for item in self.lines_of(plugin, result):
                fh.write(json.dumps(item, default=repr).encode("utf-8"))
                fh.write(b"\n")
                count += 1
        tmp.replace(path)
        return count

    def finish(self, job: Dict[str, Any]) -> Dict[str, Any]:
        """Join the parts in idx order into the file store; returns the job result."""
        job_id = str(job["id"])
        parts = sorted(self.parts_dir(job_id).glob("*.jsonl")) if self.parts_dir(job_id).is_dir() else []
        final = self.root / ("%s.jsonl" % job_id)
        final.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with final.open("wb") as out:
            for part in parts:
                with part.open("rb") as src:
                    for line in src:
                        out.write(line)
                        count += 1
        name = "%s.jsonl" % (str(job.get("name") or job_id).replace("/", "_").replace("\\", "_"))
        meta = self.state.files.add_path(final, name, "application/x-ndjson")
        try:
            final.unlink()
        except OSError:
            pass
        shutil.rmtree(self.parts_dir(job_id), ignore_errors=True)
        return {"results_file": meta["id"], "name": name, "count": count, "tasks": len(parts)}

    def discard(self, job_id: str) -> None:
        shutil.rmtree(self.parts_dir(job_id), ignore_errors=True)
        try:
            (self.root / ("%s.jsonl" % job_id)).unlink()
        except OSError:
            pass

    # -------------------------------------------------------------- reading
    def iter_lines(self, job: Dict[str, Any]) -> Iterator[bytes]:
        """Stream JSONL: the final file when done, else the parts so far."""
        result = job.get("result") if isinstance(job.get("result"), dict) else None
        file_id = (result or {}).get("results_file")
        if file_id and self.state.files.meta(str(file_id)) is not None:
            yield from self.state.files.iter_content(str(file_id))
            return
        parts_dir = self.parts_dir(str(job["id"]))
        if not parts_dir.is_dir():
            return
        for part in sorted(parts_dir.glob("*.jsonl")):
            with part.open("rb") as fh:
                while True:
                    block = fh.read(1024 * 1024)
                    if not block:
                        break
                    yield block

    def progress(self, job: Dict[str, Any]) -> Dict[str, Any]:
        parts_dir = self.parts_dir(str(job["id"]))
        parts: List[Path] = sorted(parts_dir.glob("*.jsonl")) if parts_dir.is_dir() else []
        return {"parts": len(parts), "bytes": sum(p.stat().st_size for p in parts)}
