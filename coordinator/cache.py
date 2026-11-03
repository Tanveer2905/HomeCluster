"""Never compute the same thing twice (SPEC §9.8).

A result is stored under a *fingerprint* of exactly what produced it:

* a **task**: its code, arguments, input files and required capabilities
  (file ids are SHA-256 hashes of the content, so the same bytes give the
  same fingerprint whatever the file is called);
* a **job**: its type, the plugin's code version, its params (after
  ``$from_job`` references were filled in) and the options that change the
  answer.

A later task or job with the same fingerprint gets the stored result at once.
In a pipeline that is run again, every step whose inputs did not change is
answered from the cache, so only what changed is computed.

Only *repeatable* job types are cached by default (plugins that set
``CACHE = True``: maps, number crunching, media, WebAssembly).  Shell commands,
sensors, services and models never are.  A job opts out with
``options.cache = false``; ``"refresh"`` computes again and replaces the stored
result ("Run again fresh"); ``true`` opts a non-default type in.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, Iterable, List, Optional

from .db import dumps, now

log = logging.getLogger("coordinator.cache")

#: bump when the fingerprint recipe changes
VERSION = 1

#: options that change how work is run, not what it computes
OPERATIONAL = frozenset({
    "timeout", "max_attempts", "requires", "targets", "fail_fast", "cache", "verify",
    "backups", "chunk_size", "priority", "stream_results",
})


def canonical(value: Any) -> str:
    """Stable JSON text: key order and spacing never change the fingerprint."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str,
                      ensure_ascii=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def result_digest(result: Any, outputs: Optional[Iterable[Dict[str, Any]]] = None) -> str:
    """What a check compares: the result and the content of every output file."""
    files = sorted((str(o.get("name") or ""), str(o.get("file_id") or ""))
                   for o in (outputs or []) if isinstance(o, dict))
    return digest({"result": result, "outputs": files})


def task_key(payload: Dict[str, Any]) -> str:
    """Fingerprint of one task: everything the worker gets that changes the answer."""
    return digest({
        "v": VERSION,
        "impls": payload.get("impls") or {},
        "inputs": payload.get("inputs") or [],
        "requires": sorted(str(r) for r in (payload.get("requires") or [])),
    })


def job_key(job_type: str, plugin_version: str, params: Dict[str, Any],
            options: Dict[str, Any]) -> str:
    """Fingerprint of a whole job."""
    relevant = {k: v for k, v in (options or {}).items()
                if not str(k).startswith("_") and k not in OPERATIONAL}
    if (options or {}).get("stream_results"):
        relevant["stream_results"] = True  # the result is a file, not a list
    return digest({"v": VERSION, "type": job_type, "plugin": plugin_version,
                   "params": params or {}, "options": relevant})


def file_refs(value: Any) -> List[str]:
    """Every file id a result points to (``file_id`` / ``results_file`` keys)."""
    found: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if key in ("file_id", "results_file") and isinstance(item, str):
                    found.append(item)
                else:
                    walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return found


class ResultCache:
    """The ``cache`` table: fingerprint -> stored result (SPEC §9.8)."""

    def __init__(self, state: Any) -> None:
        self.state = state
        self.db = state.db

    # ------------------------------------------------------------ policy
    def mode(self, job: Dict[str, Any], plugin: Any) -> str:
        """``on`` (read + write), ``refresh`` (write only) or ``off``."""
        if plugin is None or not self.state.setting("cache.enabled", True):
            return "off"
        options = job.get("options") or {}
        wanted = options.get("cache")
        if wanted is False or wanted == "off":
            return "off"
        if options.get("targets") == "all":
            return "off"  # one task per device: a per-device answer
        if wanted == "refresh":
            return "refresh"
        if wanted is True or wanted == "on" or plugin.cacheable:
            return "on"
        return "off"

    def job_key(self, job: Dict[str, Any], plugin: Any) -> str:
        return job_key(str(job.get("type") or ""), plugin.version,
                       plugin.normalize_params(dict(job.get("params") or {})),
                       dict(job.get("options") or {}))

    # -------------------------------------------------------------- read
    def get(self, key: Optional[str]) -> Optional[Dict[str, Any]]:
        """The stored entry, or None.  An entry whose files were deleted since
        is dropped: its answer can no longer be handed out."""
        if not key:
            return None
        entry = self.db.get("cache", key, id_col="key")
        if entry is None:
            return None
        for file_id in file_refs(entry.get("result")) + file_refs(entry.get("outputs")):
            if self.state.files.meta(file_id) is None:
                self.forget(key)
                return None
        self.db.execute("UPDATE cache SET used=?, hits=hits+1 WHERE key=?", (now(), key))
        return entry

    # ------------------------------------------------------------- write
    def put(self, key: Optional[str], kind: str, job_type: str, result: Any,
            outputs: Optional[List[Dict[str, Any]]] = None,
            devices: Optional[Iterable[str]] = None, source_job: Optional[str] = None,
            seconds: Optional[float] = None) -> bool:
        if not key:
            return False
        text = dumps(result)
        cap = int(self.state.setting("cache.max_result_kb", 4096) or 0) * 1024
        if cap and len(text) > cap:
            return False
        stamp = now()
        self.db.execute(
            "INSERT OR REPLACE INTO cache (key, kind, job_type, result, outputs, devices, size, "
            "source_job, seconds, created, used, hits) VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
            (key, kind, job_type, text, dumps(list(outputs or [])),
             dumps(sorted({str(d) for d in (devices or []) if d})), len(text),
             source_job, seconds, stamp, stamp),
        )
        self.trim()
        return True

    def forget(self, key: str) -> None:
        self.db.execute("DELETE FROM cache WHERE key=?", (key,))

    def forget_device(self, device_id: str, since: float) -> int:
        """Drop what ``device_id`` helped compute since ``since`` (it gave a
        wrong answer, SPEC §9.9)."""
        rows = self.db.rows(
            "SELECT DISTINCT cache.key AS key FROM cache, json_each(cache.devices) d "
            "WHERE d.value=? AND cache.created>=?",
            (str(device_id), float(since)),
        )
        for row in rows:
            self.forget(str(row["key"]))
        return len(rows)

    def clear(self) -> int:
        return int(self.db.execute("DELETE FROM cache").rowcount or 0)

    def trim(self) -> int:
        """Least recently used entries go until the cache fits ``cache.max_mb``."""
        cap = int(self.state.setting("cache.max_mb", 256) or 0) * 1024 * 1024
        if cap <= 0:
            return 0
        total = int(self.db.scalar("SELECT COALESCE(SUM(size),0) FROM cache", default=0))
        if total <= cap:
            return 0
        dropped = 0
        for row in self.db.rows("SELECT key, size FROM cache ORDER BY used ASC"):
            if total <= cap:
                break
            self.forget(str(row["key"]))
            total -= int(row["size"] or 0)
            dropped += 1
        return dropped

    # ------------------------------------------------------------ status
    def stats(self) -> Dict[str, Any]:
        row = self.db.row(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes, COALESCE(SUM(hits),0) AS hits, "
            "COALESCE(SUM(hits * COALESCE(seconds,0)),0) AS saved FROM cache"
        )
        by_kind = {r["kind"]: int(r["n"]) for r in
                   self.db.rows("SELECT kind, COUNT(*) AS n FROM cache GROUP BY kind")}
        return {
            "enabled": bool(self.state.setting("cache.enabled", True)),
            "entries": int(row["n"]) if row else 0,
            "jobs": by_kind.get("job", 0),
            "tasks": by_kind.get("task", 0),
            "bytes": int(row["bytes"]) if row else 0,
            "hits": int(row["hits"]) if row else 0,
            "saved_s": round(float(row["saved"]) if row else 0.0, 1),
            "max_mb": int(self.state.setting("cache.max_mb", 256) or 0),
        }

    def listing(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self.db.rows(
            "SELECT key, kind, job_type, size, source_job, seconds, created, used, hits, devices "
            "FROM cache ORDER BY used DESC LIMIT ?", (int(limit),))
        out = []
        for row in rows:
            entry = dict(row)
            try:
                entry["devices"] = json.loads(entry.get("devices") or "[]")
            except ValueError:
                entry["devices"] = []
            out.append(entry)
        return out
