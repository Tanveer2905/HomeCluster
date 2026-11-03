"""SQLite storage layer (SPEC §4).

A single :class:`sqlite3.Connection` guarded by an ``RLock`` -- every statement
is short, so this keeps the code simple and correct without an async driver.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

log = logging.getLogger("coordinator.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    platform        TEXT NOT NULL,
    token_hash      TEXT NOT NULL,
    caps            TEXT NOT NULL DEFAULT '[]',
    info            TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'offline',
    paused          INTEGER NOT NULL DEFAULT 0,
    slots           INTEGER NOT NULL DEFAULT 1,
    battery         INTEGER,
    charging        INTEGER,
    temp            REAL,
    storage_free_mb INTEGER,
    mem_free_mb     INTEGER,
    gpu             TEXT,
    bench_score     REAL,
    bench           TEXT,
    settings        TEXT NOT NULL DEFAULT '{}',
    last_seen       REAL NOT NULL DEFAULT 0,
    created         REAL NOT NULL DEFAULT 0,
    removed         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    type         TEXT NOT NULL,
    params       TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'pending',
    priority     INTEGER NOT NULL DEFAULT 0,
    depends_on   TEXT NOT NULL DEFAULT '[]',
    pipeline_id  TEXT,
    template_id  TEXT,
    created      REAL NOT NULL DEFAULT 0,
    started      REAL,
    finished     REAL,
    total_tasks  INTEGER NOT NULL DEFAULT 0,
    done_tasks   INTEGER NOT NULL DEFAULT 0,
    failed_tasks INTEGER NOT NULL DEFAULT 0,
    result       TEXT,
    error        TEXT,
    options      TEXT NOT NULL DEFAULT '{}',
    report       TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    job_id        TEXT NOT NULL,
    idx           INTEGER NOT NULL,
    payload       TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'queued',
    device_id     TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 3,
    assigned_at   REAL,
    started_at    REAL,
    finished_at   REAL,
    lease_expires REAL,
    result        TEXT,
    outputs       TEXT,
    error         TEXT,
    duration      REAL,
    work_seconds  REAL,
    history       TEXT NOT NULL DEFAULT '[]',
    checkpoint    TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    kind      TEXT NOT NULL,
    device_id TEXT,
    job_id    TEXT,
    task_id   TEXT,
    message   TEXT,
    data      TEXT
);

CREATE TABLE IF NOT EXISTS files (
    id      TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    size    INTEGER NOT NULL DEFAULT 0,
    mime    TEXT,
    created REAL NOT NULL DEFAULT 0,
    refs    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS blobs (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    size        INTEGER NOT NULL DEFAULT 0,
    chunk_size  INTEGER NOT NULL DEFAULT 0,
    n_chunks    INTEGER NOT NULL DEFAULT 0,
    replication INTEGER NOT NULL DEFAULT 1,
    status      TEXT NOT NULL DEFAULT 'storing',
    created     REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS blob_chunks (
    blob_id  TEXT NOT NULL,
    idx      INTEGER NOT NULL,
    chunk_id TEXT NOT NULL,
    size     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (blob_id, idx)
);

CREATE TABLE IF NOT EXISTS chunk_locations (
    chunk_id  TEXT NOT NULL,
    device_id TEXT NOT NULL,
    stored_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (chunk_id, device_id)
);

CREATE TABLE IF NOT EXISTS schedules (
    id       TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    cron     TEXT NOT NULL,
    job_spec TEXT NOT NULL DEFAULT '{}',
    enabled  INTEGER NOT NULL DEFAULT 1,
    last_run REAL,
    next_run REAL,
    created  REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS templates (
    id       TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    job_spec TEXT NOT NULL DEFAULT '{}',
    created  REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_deployments (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    model          TEXT NOT NULL,
    model_size     INTEGER,
    host_device    TEXT NOT NULL,
    shards         TEXT NOT NULL DEFAULT '[]',
    options        TEXT NOT NULL DEFAULT '{}',
    status         TEXT NOT NULL DEFAULT 'starting',
    error          TEXT,
    shard_jobs     TEXT NOT NULL DEFAULT '{}',
    head_job       TEXT,
    endpoints      TEXT NOT NULL DEFAULT '{}',
    head_endpoints TEXT NOT NULL DEFAULT '[]',
    created        REAL NOT NULL DEFAULT 0,
    updated        REAL,
    ready_at       REAL
);

CREATE TABLE IF NOT EXISTS speeds (
    key       TEXT NOT NULL,
    device_id TEXT NOT NULL,
    rate      REAL NOT NULL,
    samples   INTEGER NOT NULL DEFAULT 0,
    seconds   REAL NOT NULL DEFAULT 0,
    updated   REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (key, device_id)
);

CREATE TABLE IF NOT EXISTS cache (
    key        TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    job_type   TEXT,
    result     TEXT,
    outputs    TEXT NOT NULL DEFAULT '[]',
    devices    TEXT NOT NULL DEFAULT '[]',
    size       INTEGER NOT NULL DEFAULT 0,
    source_job TEXT,
    seconds    REAL,
    created    REAL NOT NULL DEFAULT 0,
    used       REAL NOT NULL DEFAULT 0,
    hits       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS blob_parity (
    blob_id  TEXT NOT NULL,
    stripe   INTEGER NOT NULL,
    j        INTEGER NOT NULL,
    chunk_id TEXT NOT NULL,
    size     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (blob_id, stripe, j)
);

CREATE TABLE IF NOT EXISTS join_codes (
    code_hash TEXT PRIMARY KEY,
    created   REAL NOT NULL DEFAULT 0,
    expires   REAL NOT NULL DEFAULT 0,
    used      REAL,
    device_id TEXT,
    note      TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_job    ON tasks(job_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_device ON tasks(device_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_events_ts    ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_kind  ON events(kind);
CREATE INDEX IF NOT EXISTS idx_chunks_blob  ON blob_chunks(blob_id);
CREATE INDEX IF NOT EXISTS idx_loc_device   ON chunk_locations(device_id);
"""

#: JSON-encoded TEXT columns, per table.
JSON_COLUMNS: Dict[str, Sequence[str]] = {
    "devices": ("caps", "info", "settings", "bench", "gpu", "trust"),
    "jobs": ("params", "depends_on", "result", "options", "report"),
    "tasks": ("payload", "result", "outputs", "history", "checkpoint"),
    "events": ("data",),
    "files": (),
    "blobs": (),
    "blob_chunks": (),
    "chunk_locations": (),
    "schedules": ("job_spec",),
    "templates": ("job_spec",),
    "settings": ("value",),
    "llm_deployments": ("shards", "options", "shard_jobs", "endpoints", "head_endpoints"),
    "speeds": (),
    "cache": ("result", "outputs", "devices"),
    "blob_parity": (),
    "join_codes": (),
}

EVENT_KINDS = (
    "device_joined",
    "device_offline",
    "device_online",
    "device_removed",
    "device_paused",
    "job_submitted",
    "job_started",
    "job_done",
    "job_failed",
    "job_cancelled",
    "task_failed",
    "task_requeued",
    "blob_stored",
    "blob_degraded",
    "schedule_fired",
    "notify_sent",
    "job_cached",
    "task_reused",
    "task_checked",
    "wrong_answer",
    "task_disputed",
    "work_not_repeatable",
    "job_rechecked",
    "blob_scrubbed",
    "error",
)


def now() -> float:
    """Current UTC Unix timestamp as a float."""
    return time.time()


def new_id() -> str:
    """12-char random hex id (SPEC §4)."""
    return secrets.token_hex(6)


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def loads(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def decode(table: str, row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    """Turn a sqlite row into a dict with JSON columns decoded."""
    if row is None:
        return None
    out = dict(row)
    for col in JSON_COLUMNS.get(table, ()):
        if col in out:
            out[col] = loads(out[col])
    return out


def decode_all(table: str, rows: Iterable[sqlite3.Row]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for row in rows:
        decoded = decode(table, row)
        if decoded is not None:
            result.append(decoded)
    return result


class Database:
    """Thin synchronous wrapper around a single WAL SQLite connection."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=30.0
        )
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
            self._migrate()
            self.conn.commit()

    #: columns added after the first release: (table, column, DDL type)
    MIGRATIONS: Sequence[Tuple[str, str, str]] = (
        ("tasks", "checkpoint", "TEXT"),
        ("devices", "mem_free_mb", "INTEGER"),
        ("devices", "gpu", "TEXT"),
        ("jobs", "report", "TEXT"),
        ("tasks", "work_seconds", "REAL"),
        ("speeds", "seconds", "REAL NOT NULL DEFAULT 0"),
        # never compute twice / catching wrong answers (SPEC §9.8, §9.9)
        ("tasks", "fingerprint", "TEXT"),
        ("tasks", "reused", "INTEGER NOT NULL DEFAULT 0"),
        ("tasks", "role", "TEXT"),
        ("tasks", "check_of", "TEXT"),
        ("tasks", "impl", "TEXT"),
        ("tasks", "result_hash", "TEXT"),
        ("tasks", "verified", "INTEGER"),
        ("devices", "trust", "TEXT"),
        # erasure-coded storage and scrubbing (SPEC §10)
        ("blobs", "scheme", "TEXT NOT NULL DEFAULT 'copies'"),
        ("blobs", "ec_k", "INTEGER NOT NULL DEFAULT 0"),
        ("blobs", "ec_m", "INTEGER NOT NULL DEFAULT 0"),
        ("chunk_locations", "checked_at", "REAL"),
    )

    def _migrate(self) -> None:
        """Add columns that older databases lack (additive only)."""
        for table, column, ddl in self.MIGRATIONS:
            have = {row[1] for row in self.conn.execute("PRAGMA table_info(%s)" % table)}
            if column not in have:
                self.conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, ddl))

    # --------------------------------------------------------------- plumbing
    def close(self) -> None:
        with self.lock:
            try:
                self.conn.commit()
            except Exception:  # pragma: no cover
                pass
            self.conn.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        with self.lock:
            self.conn.executemany(sql, [tuple(p) for p in seq])
            self.conn.commit()

    def rows(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        with self.lock:
            return list(self.conn.execute(sql, tuple(params)).fetchall())

    def row(self, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, tuple(params)).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = self.row(sql, params)
        if row is None:
            return default
        value = row[0]
        return default if value is None else value

    # ------------------------------------------------------------ CRUD sugar
    def insert(self, table: str, values: Mapping[str, Any]) -> None:
        cols = list(values.keys())
        encoded = [
            dumps(values[c]) if c in JSON_COLUMNS.get(table, ()) else values[c]
            for c in cols
        ]
        sql = "INSERT INTO {} ({}) VALUES ({})".format(
            table, ",".join(cols), ",".join("?" for _ in cols)
        )
        self.execute(sql, encoded)

    def update(self, table: str, key: Mapping[str, Any], values: Mapping[str, Any]) -> None:
        if not values:
            return
        cols = list(values.keys())
        encoded = [
            dumps(values[c]) if c in JSON_COLUMNS.get(table, ()) else values[c]
            for c in cols
        ]
        where_cols = list(key.keys())
        sql = "UPDATE {} SET {} WHERE {}".format(
            table,
            ",".join("{}=?".format(c) for c in cols),
            " AND ".join("{}=?".format(c) for c in where_cols),
        )
        self.execute(sql, encoded + [key[c] for c in where_cols])

    def get(self, table: str, id_value: Any, id_col: str = "id") -> Optional[Dict[str, Any]]:
        row = self.row("SELECT * FROM {} WHERE {}=?".format(table, id_col), (id_value,))
        return decode(table, row)

    def select(self, table: str, sql_tail: str = "", params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        rows = self.rows("SELECT * FROM {} {}".format(table, sql_tail), params)
        return decode_all(table, rows)

    # -------------------------------------------------------------- settings
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.row("SELECT value FROM settings WHERE key=?", (key,))
        if row is None:
            return default
        value = loads(row["value"])
        return default if value is None else value

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, dumps(value)),
        )

    def all_settings(self) -> Dict[str, Any]:
        return {r["key"]: loads(r["value"]) for r in self.rows("SELECT * FROM settings")}

    # ---------------------------------------------------------------- events
    def add_event(
        self,
        kind: str,
        message: str = "",
        *,
        device_id: Optional[str] = None,
        job_id: Optional[str] = None,
        task_id: Optional[str] = None,
        data: Optional[Any] = None,
    ) -> Dict[str, Any]:
        ts = now()
        cur = self.execute(
            "INSERT INTO events (ts, kind, device_id, job_id, task_id, message, data) "
            "VALUES (?,?,?,?,?,?,?)",
            (ts, kind, device_id, job_id, task_id, message, dumps(data) if data is not None else None),
        )
        return {
            "id": cur.lastrowid,
            "ts": ts,
            "kind": kind,
            "device_id": device_id,
            "job_id": job_id,
            "task_id": task_id,
            "message": message,
            "data": data,
        }
