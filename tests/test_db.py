"""SQLite layer, config and file store (SPEC §4, §10, §12)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator import config as cfg
from coordinator.db import Database, decode, dumps, loads, new_id, now
from coordinator.files import FileStore


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "cluster.db")
    yield database
    database.close()


# --------------------------------------------------------------------- schema
def test_schema_creates_every_table(db: Database) -> None:
    names = {
        row["name"]
        for row in db.rows("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for table in (
        "devices", "jobs", "tasks", "events", "files", "blobs", "blob_chunks",
        "chunk_locations", "schedules", "templates", "settings",
    ):
        assert table in names


def test_wal_mode_is_enabled(db: Database) -> None:
    assert str(db.scalar("PRAGMA journal_mode")).lower() == "wal"


def test_new_id_is_12_hex_chars() -> None:
    value = new_id()
    assert len(value) == 12
    int(value, 16)
    assert new_id() != value


# ------------------------------------------------------------------ json cols
def test_json_columns_round_trip(db: Database) -> None:
    db.insert(
        "devices",
        {
            "id": "d1",
            "name": "phone",
            "platform": "termux",
            "token_hash": "x" * 64,
            "caps": ["python", "shell"],
            "info": {"cpu_count": 4},
            "settings": {"min_battery": 5},
            "last_seen": now(),
            "created": now(),
        },
    )
    device = db.get("devices", "d1")
    assert device is not None
    assert device["caps"] == ["python", "shell"]
    assert device["info"]["cpu_count"] == 4
    assert device["settings"]["min_battery"] == 5
    assert device["bench"] is None


def test_update_and_select(db: Database) -> None:
    db.insert("jobs", {"id": "j1", "name": "a", "type": "shell", "created": 1.0,
                       "params": {"cmd": "echo"}, "options": {}, "depends_on": []})
    db.insert("jobs", {"id": "j2", "name": "b", "type": "shell", "created": 2.0,
                       "params": {}, "options": {}, "depends_on": []})
    db.update("jobs", {"id": "j1"}, {"status": "done", "result": [1, 2, 3]})
    job = db.get("jobs", "j1")
    assert job["status"] == "done"
    assert job["result"] == [1, 2, 3]
    rows = db.select("jobs", "ORDER BY created DESC")
    assert [r["id"] for r in rows] == ["j2", "j1"]


def test_update_with_no_values_is_a_noop(db: Database) -> None:
    db.insert("templates", {"id": "t1", "name": "n", "job_spec": {}, "created": 1.0})
    db.update("templates", {"id": "t1"}, {})
    assert db.get("templates", "t1")["name"] == "n"


def test_loads_and_dumps_handle_garbage() -> None:
    assert loads(None) is None
    assert loads("") is None
    assert loads("not json") is None
    assert loads('{"a":1}') == {"a": 1}
    assert loads({"a": 1}) == {"a": 1}
    assert json.loads(dumps({"a": 1})) == {"a": 1}
    assert decode("devices", None) is None


# -------------------------------------------------------------------- events
def test_add_event_returns_the_row(db: Database) -> None:
    event = db.add_event("job_done", "hello", job_id="j1", data={"n": 1})
    assert event["kind"] == "job_done"
    assert event["job_id"] == "j1"
    assert event["data"] == {"n": 1}
    stored = db.select("events", "ORDER BY id DESC LIMIT 1")[0]
    assert stored["message"] == "hello"
    assert stored["data"] == {"n": 1}


# ------------------------------------------------------------------ settings
def test_settings_round_trip(db: Database) -> None:
    assert db.get_setting("missing", "fallback") == "fallback"
    db.set_setting("scheduler", {"min_battery": 33})
    assert db.get_setting("scheduler")["min_battery"] == 33
    db.set_setting("scheduler", {"min_battery": 44})
    assert db.get_setting("scheduler")["min_battery"] == 44
    assert "scheduler" in db.all_settings()


# -------------------------------------------------------------------- config
def test_config_is_created_with_defaults_and_a_pin(tmp_path: Path) -> None:
    config = cfg.load_config(tmp_path)
    assert (tmp_path / "config.yaml").is_file()
    assert len(config.pin) == 6 and config.pin.isdigit()
    assert config.port == 8765
    assert config.get("scheduler.min_battery") == 20
    assert config.get("storage.chunk_mb") == 4


def test_config_reload_keeps_the_pin(tmp_path: Path) -> None:
    first = cfg.load_config(tmp_path)
    second = cfg.load_config(tmp_path)
    assert first.pin == second.pin


def test_config_set_get_merge_and_save(tmp_path: Path) -> None:
    config = cfg.load_config(tmp_path)
    config.set("scheduler.min_battery", 7)
    config.merge({"storage": {"replication": 3}})
    config.save()
    again = cfg.load_config(tmp_path)
    assert again.get("scheduler.min_battery") == 7
    assert again.get("storage.replication") == 3
    assert again.get("storage.chunk_mb") == 4  # untouched defaults survive
    assert again.get("nope.nope", "d") == "d"


def test_ensure_dirs_creates_the_tree(tmp_path: Path) -> None:
    cfg.ensure_dirs(tmp_path)
    for sub in ("files", "storage/cache", "logs/tasks", "worker", "plugins"):
        assert (tmp_path / sub).is_dir()


def test_root_and_data_dir_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PC_ROOT", str(tmp_path))
    monkeypatch.setenv("PC_DATA_DIR", str(tmp_path / "d"))
    assert cfg.root_dir() == tmp_path.resolve()
    assert cfg.data_dir() == (tmp_path / "d").resolve()
    assert cfg.data_dir(tmp_path / "other") == (tmp_path / "other").resolve()


# ---------------------------------------------------------------- file store
def test_file_store_is_content_addressed(db: Database, tmp_path: Path) -> None:
    store = FileStore(db, tmp_path / "files")
    first = store.add_bytes(b"hello world", "a.txt")
    second = store.add_bytes(b"hello world", "b.txt")
    assert first["id"] == second["id"]
    assert first["size"] == 11
    assert store.read(first["id"]) == b"hello world"
    assert len(store.list()) == 1
    assert store.meta(first["id"])["name"] == "a.txt"


def test_file_store_streams_and_deletes(db: Database, tmp_path: Path) -> None:
    store = FileStore(db, tmp_path / "files")
    meta = store.add_stream([b"ab", b"cd", b""], "s.bin")
    assert meta["size"] == 4
    assert b"".join(store.iter_content(meta["id"])) == b"abcd"
    store.add_ref(meta["id"])
    assert store.meta(meta["id"])["refs"] == 1
    assert store.total_size() == 4
    assert store.delete(meta["id"]) is True
    assert store.delete(meta["id"]) is False
    assert store.meta(meta["id"]) is None


def test_file_store_rejects_bad_ids(db: Database, tmp_path: Path) -> None:
    store = FileStore(db, tmp_path / "files")
    with pytest.raises(ValueError):
        store.path_for("not-a-sha")


def test_file_store_add_path_and_copy_to(db: Database, tmp_path: Path) -> None:
    store = FileStore(db, tmp_path / "files")
    source = tmp_path / "src.txt"
    source.write_bytes(b"payload")
    meta = store.add_path(source)
    assert meta["name"] == "src.txt"
    dest = store.copy_to(meta["id"], tmp_path / "out" / "copy.txt")
    assert dest.read_bytes() == b"payload"
