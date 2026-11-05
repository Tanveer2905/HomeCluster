"""Only the final result stays (SPEC §9.10).

An hour after a job finished successfully -- and never inside the window in
which a wrong answer can still reopen it (``verify.recheck_minutes``) -- its
in-between data goes: every task's own result, saved progress and slice of the
input, the double-check copies of its tasks, and output files its final result
does not mention (the pieces of a split video, say), unless something else
still uses them.  Kept: the final result and its files, the files that went in,
each task's row (who ran it, for how long), the task logs and the store of
reusable results (§9.8).  Failed and cancelled jobs keep everything until they
are deleted, so Retry can reuse the tasks that finished.

Deleting a job removes everything that belongs to it: its tasks, their logs and
the files it made, unless something else still uses them.  Files it was given
stay.

Old jobs (user's choices): ``cleanup.keep_days`` (30; 0 = forever) after any job
finished -- done, failed or cancelled -- only the job and its final result stay:
its tasks, their logs and the files it made go as if it were deleted, and so do
events older than that.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Set

from .db import dumps, now

log = logging.getLogger("coordinator.cleanup")

CLEAN_AFTER = 3600.0     # seconds after a job finished
SWEEP_EVERY = 300.0      # how often finished jobs are looked at
FILE_ID = re.compile(r"\b[0-9a-f]{64}\b")   # file store ids are SHA-256 hex


def file_ids(value: Any) -> Set[str]:
    """Every file id mentioned anywhere in a value (a result, params, a spec)."""
    text = value if isinstance(value, str) else dumps(value)
    return set(FILE_ID.findall(text or ""))


class Cleanup:
    def __init__(self, state: Any) -> None:
        self.state = state
        self._task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="cleanup")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014
                pass

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_EVERY)   # nothing is due right after a start
            try:
                await asyncio.to_thread(self.sweep)
                await asyncio.to_thread(self.expire)
            except Exception:  # pragma: no cover - never stop cleaning for one bad job
                log.exception("cleaning finished jobs failed")

    def delay(self) -> float:
        recheck = float(self.state.setting("verify.recheck_minutes", 60) or 0) * 60
        return max(CLEAN_AFTER, recheck + 60.0)

    # -------------------------------------------------------------- sweeping
    def sweep(self, stamp: Optional[float] = None) -> int:
        """Clean every job that finished well long enough ago; how many."""
        cutoff = (now() if stamp is None else stamp) - self.delay()
        jobs = self.state.db.select(
            "jobs", "WHERE status='done' AND finished IS NOT NULL AND finished < ? "
                    "AND options NOT LIKE ?", (cutoff, '%"_cleaned"%'))
        for job in jobs:
            self.clean_job(job)
        return len(jobs)

    def clean_job(self, job: Dict[str, Any]) -> Dict[str, int]:
        db = self.state.db
        keep = file_ids(job.get("result")) | file_ids(job.get("params"))
        dropped: Set[str] = set()
        copies = 0
        for task in db.select("tasks", "WHERE job_id=?", (job["id"],)):
            outputs = [o for o in (task.get("outputs") or []) if isinstance(o, dict)]
            dropped |= {str(o.get("file_id")) for o in outputs if o.get("file_id")} - keep
            if task.get("check_of"):
                db.execute("DELETE FROM tasks WHERE id=?", (task["id"],))   # a double-check copy
                copies += 1
                continue
            meta = (task.get("payload") or {}).get("meta")
            db.update("tasks", {"id": task["id"]}, {
                "result": None, "checkpoint": None,
                "payload": {"meta": meta} if meta else {},
                "outputs": [o for o in outputs if o.get("file_id") in keep],
            })
        removed = self._remove_files(dropped, job["id"])
        options = dict(job.get("options") or {})
        options["_cleaned"] = now()
        db.update("jobs", {"id": job["id"]}, {"options": options})
        log.info("job %s: in-between data cleaned (%d check copies, %d files)", job["id"], copies, removed)
        return {"copies": copies, "files": removed}

    # ------------------------------------------------------------- old jobs
    def expire(self, stamp: Optional[float] = None) -> int:
        """Jobs finished more than ``cleanup.keep_days`` ago keep only their final
        result; older events go.  How many jobs."""
        days = float(self.state.setting("cleanup.keep_days", 30) or 0)
        if days <= 0:
            return 0
        cutoff = (now() if stamp is None else stamp) - days * 86400
        jobs = self.state.db.select(
            "jobs", "WHERE status IN ('done','failed','cancelled') AND finished IS NOT NULL "
                    "AND finished < ? AND options NOT LIKE ?", (cutoff, '%"_expired"%'))
        for job in jobs:
            self.expire_job(job)
        self.state.db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        return len(jobs)

    def expire_job(self, job: Dict[str, Any]) -> int:
        db = self.state.db
        tasks = db.select("tasks", "WHERE job_id=?", (job["id"],))
        db.execute("DELETE FROM tasks WHERE job_id=?", (job["id"],))
        removed = self.forget_job(job, tasks)
        options = dict(job.get("options") or {})
        options["_expired"] = now()
        db.update("jobs", {"id": job["id"]}, {"options": options})
        log.info("job %s is old: kept its final result, removed %d task(s) and %d file(s)",
                 job["id"], len(tasks), removed)
        return removed

    # ------------------------------------------------------------- deleting
    def forget_job(self, job: Dict[str, Any], tasks: List[Dict[str, Any]]) -> int:
        """After a job's rows are gone: its task logs, and the files it made
        unless something else uses them (the files it was given stay)."""
        made: Set[str] = file_ids(job.get("result"))
        for task in tasks:
            made |= {str(o.get("file_id")) for o in (task.get("outputs") or [])
                     if isinstance(o, dict) and o.get("file_id")}
            try:
                self.state.task_log_path(task["id"]).unlink(missing_ok=True)
            except OSError:  # pragma: no cover
                pass
        return self._remove_files(made - file_ids(job.get("params")), job["id"])

    def _remove_files(self, candidates: Iterable[str], job_id: str) -> int:
        removed = 0
        for file_id in sorted(candidates):
            if not self.used_elsewhere(file_id, job_id) and self.state.files.delete(file_id):
                removed += 1
        return removed

    def used_elsewhere(self, file_id: str, job_id: str) -> bool:
        """Another job, task, template or schedule mentions this file."""
        like = "%" + file_id + "%"
        db = self.state.db
        checks = [
            ("SELECT 1 FROM jobs WHERE id<>? AND (params LIKE ? OR result LIKE ?) LIMIT 1",
             (job_id, like, like)),
            ("SELECT 1 FROM tasks WHERE job_id<>? AND (payload LIKE ? OR outputs LIKE ? OR result LIKE ?) LIMIT 1",
             (job_id, like, like, like)),
            ("SELECT 1 FROM templates WHERE job_spec LIKE ? LIMIT 1", (like,)),
            ("SELECT 1 FROM schedules WHERE job_spec LIKE ? LIMIT 1", (like,)),
        ]
        return any(db.row(sql, params) is not None for sql, params in checks)
