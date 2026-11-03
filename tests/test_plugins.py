"""Every built-in plugin's split() and reduce() (SPEC §7)."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from conftest import SimpleCtx, run_python_impl
from coordinator.plugins import Registry, build_registry


# --------------------------------------------------------------------------
# a tiny stand-in for coordinator.plugin_ctx.PluginContext
# --------------------------------------------------------------------------

class FakeCtx:
    def __init__(self, devices: Optional[List[Dict[str, Any]]] = None, tmp: Optional[Path] = None):
        self._devices = devices if devices is not None else [
            {"id": "dev1", "name": "phone1", "caps": ["python", "shell", "termux_api"],
             "platform": "termux"},
            {"id": "dev2", "name": "pc", "caps": ["python", "shell", "js", "ffmpeg"],
             "platform": "windows"},
        ]
        self.files: Dict[str, Dict[str, Any]] = {}
        self.blobs: Dict[str, bytes] = {}
        self.bench: Dict[str, float] = {}
        self.added: List[Dict[str, Any]] = []
        self._tmp = tmp

    def devices(self) -> List[Dict[str, Any]]:
        return list(self._devices)

    def file_meta(self, file_id: str) -> Optional[Dict[str, Any]]:
        return self.files.get(str(file_id))

    def file_path(self, file_id: str) -> Path:
        return Path(self._tmp or ".") / str(file_id)

    def read_file(self, file_id: str) -> bytes:
        return self.blobs[str(file_id)]

    def read_json(self, file_id: str) -> Any:
        return json.loads(self.read_file(file_id).decode("utf-8"))

    def add_file(self, data: bytes, name: str = "out.bin") -> Dict[str, Any]:
        entry = {"id": hashlib.sha256(data).hexdigest(), "name": name, "size": len(data)}
        self.added.append(entry)
        return entry

    def add_file_path(self, path: Path, name: Optional[str] = None) -> Dict[str, Any]:
        return self.add_file(Path(path).read_bytes(), name or Path(path).name)

    def tmp_dir(self) -> Path:
        return Path(self._tmp or ".")

    def ffmpeg(self) -> Optional[str]:
        return None

    def ffprobe(self) -> Optional[str]:
        return None

    def set_bench(self, device_id: str, score: float, bench: Any = None) -> None:
        self.bench[device_id] = score


@pytest.fixture()
def registry() -> Registry:
    return build_registry(None)


@pytest.fixture()
def ctx() -> FakeCtx:
    return FakeCtx()


def results_from(payloads: List[Dict[str, Any]], device_ids: Optional[List[str]] = None,
                 ctx_factory: Any = None) -> List[Dict[str, Any]]:
    """Run every payload's python impl locally and build the reduce() input."""
    out: List[Dict[str, Any]] = []
    for idx, payload in enumerate(payloads):
        impl = (payload.get("impls") or {}).get("python")
        assert impl is not None, "payload %d has no python impl" % idx
        value = run_python_impl(impl, (ctx_factory or SimpleCtx)())
        out.append(
            {
                "idx": idx,
                "device_id": payload.get("device_id")
                or (device_ids[idx] if device_ids else "dev1"),
                "ok": True,
                "result": value,
                "outputs": [],
                "error": None,
                "meta": payload.get("meta") or {},
            }
        )
    return out


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

def test_every_builtin_is_registered(registry: Registry) -> None:
    assert set(registry.types()) == {
        "python_map", "shell", "media", "hash_search", "monte_carlo_pi",
        "prime_range", "sensor", "benchmark", "custom", "service", "llm_shard", "llm_head", "wasm",
    }
    for described in registry.describe_all():
        assert described["type"]
        assert isinstance(described["params"], list)
        assert isinstance(described["default_options"], dict)
        for spec in described["params"]:
            assert "name" in spec and "type" in spec


def test_registry_lookup(registry: Registry) -> None:
    assert registry.get("nope") is None
    assert "shell" in registry
    assert len(registry) == 13
    hidden = {p["type"] for p in registry.describe_all() if p["hidden"]}
    assert hidden == {"llm_shard", "llm_head"}
    with pytest.raises(KeyError):
        registry.require("nope")


def test_user_plugins_are_loaded(tmp_path: Path) -> None:
    (tmp_path / "hello.py").write_text(
        "TYPE = 'hello'\n"
        "DESCRIPTION = 'user plugin'\n"
        "PARAMS = []\n"
        "def split(params, options, ctx):\n"
        "    return [{'impls': {'shell': {'cmd': 'echo hi'}}}]\n",
        encoding="utf-8",
    )
    (tmp_path / "_skipped.py").write_text("TYPE = 'skipped'\n", encoding="utf-8")
    (tmp_path / "broken.py").write_text("this is not python(\n", encoding="utf-8")
    registry = build_registry(tmp_path)
    assert "hello" in registry
    assert "skipped" not in registry
    assert "broken" not in registry
    plugin = registry.require("hello")
    assert plugin.split({}, {}, None)[0]["impls"]["shell"]["cmd"] == "echo hi"
    # no reduce() defined -> the default collects the per-task results
    assert plugin.reduce({}, [{"result": 1}, {"result": 2}], None) == [1, 2]


# --------------------------------------------------------------------------
# python_map
# --------------------------------------------------------------------------

CODE = "def fn(item, ctx):\n    ctx.log('item', item)\n    return item * item\n"


def test_python_map_split_and_reduce(registry: Registry, ctx: FakeCtx) -> None:
    plugin = registry.require("python_map")
    payloads = plugin.split(
        {"code": CODE, "fn_name": "fn", "items": list(range(7)), "chunk_size": 3},
        {"timeout": 60, "requires": []},
        ctx,
    )
    assert len(payloads) == 3
    assert [p["meta"]["count"] for p in payloads] == [3, 3, 1]
    assert payloads[0]["timeout"] == 60
    assert "js" not in payloads[0]["impls"]
    assert plugin.reduce({}, results_from(payloads), ctx) == [0, 1, 4, 9, 16, 25, 36]


def test_python_map_items_file(registry: Registry, ctx: FakeCtx) -> None:
    ctx.blobs["f1"] = json.dumps([2, 3]).encode("utf-8")
    plugin = registry.require("python_map")
    payloads = plugin.split(
        {"code": CODE, "items_file": "f1", "chunk_size": 10}, {}, ctx
    )
    assert len(payloads) == 1
    assert plugin.reduce({}, results_from(payloads), ctx) == [4, 9]


def test_python_map_adds_a_js_impl(registry: Registry, ctx: FakeCtx) -> None:
    payloads = registry.require("python_map").split(
        {"code": CODE, "items": [1], "js_code": "function fn(i){return i;}"}, {}, ctx
    )
    assert "js" in payloads[0]["impls"]
    assert "async function __pcRun" in payloads[0]["impls"]["js"]["code"]


def test_python_map_validation(registry: Registry, ctx: FakeCtx) -> None:
    plugin = registry.require("python_map")
    with pytest.raises(ValueError):
        plugin.split({"code": "", "items": [1]}, {}, ctx)
    with pytest.raises(ValueError):
        plugin.split({"code": CODE, "items": "nope"}, {}, ctx)
    assert plugin.split({"code": CODE, "items": []}, {}, ctx) == []


def test_python_map_reports_progress(registry: Registry, ctx: FakeCtx) -> None:
    payloads = registry.require("python_map").split(
        {"code": CODE, "items": [1, 2], "chunk_size": 2}, {}, ctx
    )
    task_ctx = SimpleCtx()
    run_python_impl(payloads[0]["impls"]["python"], task_ctx)
    assert task_ctx.progress_values == [0.5, 1.0]
    assert task_ctx.lines == ["item 1", "item 2"]


def test_python_map_missing_function_is_an_error(registry: Registry, ctx: FakeCtx) -> None:
    payloads = registry.require("python_map").split(
        {"code": "x = 1\n", "fn_name": "nope", "items": [1]}, {}, ctx
    )
    with pytest.raises(NameError):
        run_python_impl(payloads[0]["impls"]["python"])


# --------------------------------------------------------------------------
# shell
# --------------------------------------------------------------------------

def test_shell_single_task(registry: Registry, ctx: FakeCtx) -> None:
    payloads = registry.require("shell").split(
        {"cmd": "echo hi", "timeout": 30}, {"targets": "any"}, ctx
    )
    assert len(payloads) == 1
    assert payloads[0]["impls"]["shell"]["cmd"] == "echo hi"
    assert payloads[0]["timeout"] == 30
    assert "device_id" not in payloads[0]


def test_shell_targets_all_fans_out(registry: Registry, ctx: FakeCtx) -> None:
    payloads = registry.require("shell").split(
        {"cmd": "uptime"}, {"targets": "all"}, ctx
    )
    assert [p["device_id"] for p in payloads] == ["dev1", "dev2"]


def test_shell_targets_list(registry: Registry, ctx: FakeCtx) -> None:
    payloads = registry.require("shell").split(
        {"cmd": "uptime"}, {"targets": ["dev2"]}, ctx
    )
    assert [p["device_id"] for p in payloads] == ["dev2"]


def test_shell_cmd_windows_is_passed_through(registry: Registry, ctx: FakeCtx) -> None:
    payloads = registry.require("shell").split(
        {"cmd": "ls", "cmd_windows": "dir"}, {}, ctx
    )
    assert payloads[0]["impls"]["shell"]["cmd_windows"] == "dir"


def test_shell_reduce_keys_by_device(registry: Registry, ctx: FakeCtx) -> None:
    reduced = registry.require("shell").reduce(
        {},
        [
            {"idx": 0, "device_id": "dev1", "ok": True,
             "result": {"exit_code": 0, "stdout": "hi\n", "stderr": ""}},
            {"idx": 1, "device_id": "dev2", "ok": False, "result": None, "error": "boom"},
        ],
        ctx,
    )
    assert reduced["dev1"]["stdout"] == "hi\n"
    assert reduced["dev2"]["stderr"] == "boom"


def test_shell_needs_a_command(registry: Registry, ctx: FakeCtx) -> None:
    with pytest.raises(ValueError):
        registry.require("shell").split({"cmd": "  "}, {}, ctx)
    with pytest.raises(ValueError):
        registry.require("shell").split({"cmd": "x"}, {"targets": []}, ctx)


def test_shell_targets_all_without_devices(registry: Registry) -> None:
    with pytest.raises(ValueError):
        registry.require("shell").split({"cmd": "x"}, {"targets": "all"}, FakeCtx(devices=[]))


# --------------------------------------------------------------------------
# hash_search
# --------------------------------------------------------------------------

def test_hash_search_finds_a_known_prefix(registry: Registry, ctx: FakeCtx) -> None:
    plugin = registry.require("hash_search")
    payloads = plugin.split(
        {"algo": "sha256", "prefix": "0", "start": 0, "count": 40, "chunk_size": 10,
         "salt": "", "max_results": 5},
        {},
        ctx,
    )
    assert len(payloads) == 4
    reduced = plugin.reduce({"max_results": 5}, results_from(payloads), ctx)
    assert reduced["searched"] > 0
    assert reduced["matches"], "sha256 of some 0..39 must start with '0'"
    for match in reduced["matches"]:
        digest = hashlib.sha256(str(match["n"]).encode()).hexdigest()
        assert digest == match["hash"]
        assert digest.startswith("0")
    assert reduced["matches"] == sorted(reduced["matches"], key=lambda m: m["n"])


def test_hash_search_md5_has_no_js_impl(registry: Registry, ctx: FakeCtx) -> None:
    payloads = registry.require("hash_search").split(
        {"algo": "md5", "prefix": "ff", "start": 0, "count": 5, "chunk_size": 5}, {}, ctx
    )
    assert "js" not in payloads[0]["impls"]
    payloads = registry.require("hash_search").split(
        {"algo": "sha256", "prefix": "ff", "start": 0, "count": 5, "chunk_size": 5}, {}, ctx
    )
    assert "js" in payloads[0]["impls"]


def test_hash_search_salt_is_used(registry: Registry, ctx: FakeCtx) -> None:
    plugin = registry.require("hash_search")
    payloads = plugin.split(
        {"prefix": "", "start": 7, "count": 1, "chunk_size": 1, "salt": "pc-"}, {}, ctx
    )
    result = run_python_impl(payloads[0]["impls"]["python"])
    assert result["matches"][0]["hash"] == hashlib.sha256(b"pc-7").hexdigest()


def test_hash_search_validation(registry: Registry, ctx: FakeCtx) -> None:
    with pytest.raises(ValueError):
        registry.require("hash_search").split({"algo": "crc32"}, {}, ctx)
    with pytest.raises(ValueError):
        registry.require("hash_search").split({"algo": "md5", "count": 0}, {}, ctx)


# --------------------------------------------------------------------------
# monte_carlo_pi
# --------------------------------------------------------------------------

def test_monte_carlo_pi(registry: Registry, ctx: FakeCtx) -> None:
    plugin = registry.require("monte_carlo_pi")
    payloads = plugin.split({"samples": 4000, "chunks": 4}, {}, ctx)
    assert len(payloads) == 4
    assert sum(p["meta"]["samples"] for p in payloads) == 4000
    seeds = {p["impls"]["python"]["args"]["seed"] for p in payloads}
    assert len(seeds) == 4
    reduced = plugin.reduce({}, results_from(payloads), ctx)
    assert reduced["samples"] == 4000
    assert 0 <= reduced["inside"] <= 4000
    assert math.isclose(reduced["pi"], math.pi, abs_tol=0.3)


def test_monte_carlo_uneven_split(registry: Registry, ctx: FakeCtx) -> None:
    payloads = registry.require("monte_carlo_pi").split({"samples": 10, "chunks": 3}, {}, ctx)
    assert sorted(p["meta"]["samples"] for p in payloads) == [3, 3, 4]
    with pytest.raises(ValueError):
        registry.require("monte_carlo_pi").split({"samples": 0}, {}, ctx)


def test_monte_carlo_reduce_of_nothing(registry: Registry, ctx: FakeCtx) -> None:
    assert registry.require("monte_carlo_pi").reduce({}, [], ctx) == {
        "pi": 0.0, "inside": 0, "samples": 0
    }


# --------------------------------------------------------------------------
# prime_range
# --------------------------------------------------------------------------

def test_prime_range_counts(registry: Registry, ctx: FakeCtx) -> None:
    plugin = registry.require("prime_range")
    payloads = plugin.split({"start": 2, "end": 100, "chunk_size": 30}, {}, ctx)
    assert len(payloads) == 4
    reduced = plugin.reduce({}, results_from(payloads), ctx)
    assert reduced == {"count": 25}  # 25 primes below 100


def test_prime_range_returns_the_list(registry: Registry, ctx: FakeCtx) -> None:
    plugin = registry.require("prime_range")
    payloads = plugin.split(
        {"start": 0, "end": 30, "chunk_size": 10, "return_list": True}, {}, ctx
    )
    reduced = plugin.reduce({"return_list": True}, results_from(payloads), ctx)
    assert reduced["primes"] == [2, 3, 5, 7, 11, 13, 17, 19, 23, 29]
    assert reduced["count"] == 10


def test_prime_range_validation(registry: Registry, ctx: FakeCtx) -> None:
    with pytest.raises(ValueError):
        registry.require("prime_range").split({"start": 10, "end": 10}, {}, ctx)


# --------------------------------------------------------------------------
# benchmark
# --------------------------------------------------------------------------

def test_benchmark_is_pinned_per_device(registry: Registry, ctx: FakeCtx) -> None:
    plugin = registry.require("benchmark")
    assert plugin.default_options["targets"] == "all"
    payloads = plugin.split({}, {"targets": "all"}, ctx)
    assert [p["device_id"] for p in payloads] == ["dev1", "dev2"]
    assert "js" in payloads[1]["impls"]  # dev2 reports the js cap
    assert "js" not in payloads[0]["impls"]


def test_benchmark_reduce_writes_scores(registry: Registry, ctx: FakeCtx) -> None:
    reduced = registry.require("benchmark").reduce(
        {},
        [
            {"idx": 0, "device_id": "dev1", "ok": True,
             "result": {"score": 3.5, "loop_iters": 3500000, "hash_mb_s": 90.0, "kind": "python"}},
            {"idx": 1, "device_id": "dev2", "ok": True,
             "result": {"score": 12.0, "loop_iters": 12000000, "hash_mb_s": 400.0, "kind": "python"}},
        ],
        ctx,
    )
    assert ctx.bench == {"dev1": 3.5, "dev2": 12.0}
    assert reduced["dev2"]["score"] == 12.0


def test_benchmark_worker_code_follows_the_spec(registry: Registry, ctx: FakeCtx) -> None:
    from coordinator.plugins import benchmark as plugin_module

    assert "x = (x * 1103515245 + 12345) & 0x7fffffff" in plugin_module.WORKER_PY
    assert "start + 2.0" in plugin_module.WORKER_PY
    assert "hstart + 1.0" in plugin_module.WORKER_PY
    assert "iters / 1e6" in plugin_module.WORKER_PY
    assert "iters / 25 / 1e6" in plugin_module.WORKER_JS


def test_benchmark_needs_devices(registry: Registry) -> None:
    with pytest.raises(ValueError):
        registry.require("benchmark").split({}, {"targets": "all"}, FakeCtx(devices=[]))


def test_benchmark_worker_code_actually_runs(registry: Registry, ctx: FakeCtx) -> None:
    payloads = registry.require("benchmark").split({}, {"targets": ["dev1"]}, ctx)
    result = run_python_impl(payloads[0]["impls"]["python"])
    assert result["kind"] == "python"
    assert result["score"] > 0
    assert result["loop_iters"] > 0
    assert result["hash_mb_s"] > 0


# --------------------------------------------------------------------------
# sensor
# --------------------------------------------------------------------------

def test_sensor_builds_termux_commands(registry: Registry, ctx: FakeCtx) -> None:
    plugin = registry.require("sensor")
    payloads = plugin.split({"sensor": "battery"}, {"targets": "all"}, ctx)
    assert len(payloads) == 1  # only dev1 has termux_api
    assert payloads[0]["device_id"] == "dev1"
    assert payloads[0]["impls"]["shell"]["cmd"] == "termux-battery-status"
    assert "termux_api" in payloads[0]["requires"]

    photo = plugin.split({"sensor": "photo", "camera": 1}, {"targets": ["dev1"]}, ctx)
    assert "termux-camera-photo -c 1" in photo[0]["impls"]["shell"]["cmd"]
    audio = plugin.split({"sensor": "audio", "duration": 3}, {"targets": ["dev1"]}, ctx)
    assert "termux-microphone-record" in audio[0]["impls"]["shell"]["cmd"]
    for name, expected in (
        ("location", "termux-location"),
        ("sensors", "termux-sensor -a -n 1"),
        ("wifi", "termux-wifi-connectioninfo"),
    ):
        built = plugin.split({"sensor": name}, {"targets": ["dev1"]}, ctx)
        assert built[0]["impls"]["shell"]["cmd"] == expected


def test_sensor_reduce_parses_json(registry: Registry, ctx: FakeCtx) -> None:
    reduced = registry.require("sensor").reduce(
        {},
        [{"idx": 0, "device_id": "dev1", "ok": True,
          "result": {"exit_code": 0, "stdout": '{"percentage": 88}', "stderr": ""},
          "outputs": []}],
        ctx,
    )
    assert reduced["dev1"]["data"]["percentage"] == 88
    assert reduced["dev1"]["ok"] is True


def test_sensor_validation(registry: Registry, ctx: FakeCtx) -> None:
    with pytest.raises(ValueError):
        registry.require("sensor").split({"sensor": "radar"}, {}, ctx)
    with pytest.raises(ValueError):
        registry.require("sensor").split({"sensor": "battery"}, {}, FakeCtx(devices=[]))


# --------------------------------------------------------------------------
# media
# --------------------------------------------------------------------------

def test_media_builds_one_task_per_input(registry: Registry, ctx: FakeCtx) -> None:
    ctx.files["a" * 64] = {"id": "a" * 64, "name": "clip.mp4", "size": 10}
    ctx.files["b" * 64] = {"id": "b" * 64, "name": "photo.jpg", "size": 10}
    plugin = registry.require("media")
    payloads = plugin.split(
        {"op": "resize", "inputs": ["a" * 64, "b" * 64], "width": 320, "height": 0},
        {"requires": [], "timeout": 900},
        ctx,
    )
    assert len(payloads) == 2
    assert all("ffmpeg" in p["requires"] for p in payloads)
    assert "scale=320:-2" in payloads[0]["impls"]["shell"]["cmd"]
    assert payloads[0]["impls"]["shell"]["cmd_windows"].startswith('"%PC_FFMPEG%"')
    assert '"$PC_INPUT_0"' in payloads[0]["impls"]["shell"]["cmd"]
    assert payloads[1]["meta"]["out_name"] == "photo.jpg"


def test_media_ops_choose_sensible_arguments(registry: Registry, ctx: FakeCtx) -> None:
    ctx.files["c" * 64] = {"id": "c" * 64, "name": "movie.mkv", "size": 1}
    plugin = registry.require("media")
    thumb = plugin.split({"op": "thumbnail", "inputs": ["c" * 64]}, {}, ctx)
    assert "-frames:v 1" in thumb[0]["impls"]["shell"]["cmd"]
    assert thumb[0]["meta"]["ext"] == "jpg"
    audio = plugin.split({"op": "extract_audio", "inputs": ["c" * 64]}, {}, ctx)
    assert "-vn" in audio[0]["impls"]["shell"]["cmd"]
    assert audio[0]["meta"]["ext"] == "mp3"
    custom = plugin.split(
        {"op": "custom", "inputs": ["c" * 64], "ffmpeg_args": "-an -sn", "format": "mkv"}, {}, ctx
    )
    assert "-an" in custom[0]["impls"]["shell"]["cmd"]


def test_media_needs_inputs(registry: Registry, ctx: FakeCtx) -> None:
    with pytest.raises(ValueError):
        registry.require("media").split({"op": "transcode", "inputs": []}, {}, ctx)
    with pytest.raises(ValueError):
        registry.require("media").split({"op": "nope", "inputs": ["x"]}, {}, ctx)


def test_files_param_given_as_bare_string_is_one_input(registry: Registry, ctx: FakeCtx) -> None:
    """A single file id passed as a string must not fan out per character."""
    ctx.files["d" * 64] = {"id": "d" * 64, "name": "pic.png", "size": 1}
    payloads = registry.require("media").split(
        {"op": "resize", "inputs": "d" * 64, "width": 100}, {}, ctx
    )
    assert len(payloads) == 1
    assert payloads[0]["inputs"] == ["d" * 64]
    # Image keeps its own extension and takes the image (q:v) branch, not libx264.
    assert payloads[0]["meta"]["out_name"] == "pic.png"
    assert "libx264" not in payloads[0]["impls"]["shell"]["cmd"]
    # Same for python_map / shell / custom which all declare `inputs` as files.
    for kind in ("shell", "custom"):
        plugin = registry.require(kind)
        assert plugin.normalize_params({"inputs": "abc"})["inputs"] == ["abc"]
        assert plugin.normalize_params({"inputs": ["a", "", None, "b"]})["inputs"] == ["a", "b"]


def test_media_reduce_groups_outputs(registry: Registry, ctx: FakeCtx) -> None:
    reduced = registry.require("media").reduce(
        {},
        [
            {"idx": 0, "device_id": "dev2", "ok": True, "result": {"exit_code": 0},
             "outputs": [{"file_id": "f1", "name": "clip.mp4", "size": 5}],
             "meta": {"file_id": "a" * 64, "name": "clip.mp4", "segment": None, "ext": "mp4"}},
        ],
        ctx,
    )
    assert reduced["files"][0]["name"] == "clip.mp4"
    assert reduced["files"][0]["outputs"][0]["file_id"] == "f1"
    assert "joined" not in reduced["files"][0]


def test_media_reduce_without_ffmpeg_keeps_segments(registry: Registry, ctx: FakeCtx) -> None:
    items = [
        {"idx": i, "device_id": "dev2", "ok": True, "result": {"exit_code": 0},
         "outputs": [{"file_id": "f%d" % i, "name": "clip.part%04d.mp4" % i, "size": 5}],
         "meta": {"file_id": "a" * 64, "name": "clip.mp4", "segment": i, "ext": "mp4"}}
        for i in (0, 1)
    ]
    reduced = registry.require("media").reduce({}, items, ctx)
    assert "joined_error" in reduced["files"][0]
    assert len(reduced["files"][0]["outputs"]) == 2


def test_media_probe_duration_without_ffprobe(tmp_path: Path) -> None:
    from coordinator.plugins.media import probe_duration

    assert probe_duration(None, tmp_path / "nope.mp4") is None


# --------------------------------------------------------------------------
# custom
# --------------------------------------------------------------------------

def test_custom_passthrough(registry: Registry, ctx: FakeCtx) -> None:
    plugin = registry.require("custom")
    payloads = plugin.split(
        {"impls": {"shell": {"cmd": "echo hi"}}, "requires": ["ffmpeg"]}, {}, ctx
    )
    assert len(payloads) == 1
    assert payloads[0]["requires"] == ["ffmpeg"]


def test_custom_args_list_makes_one_task_each(registry: Registry, ctx: FakeCtx) -> None:
    plugin = registry.require("custom")
    payloads = plugin.split(
        {
            "impls": {"python": {"code": "def run(a, c):\n    return a['n']\n", "entry": "run",
                                 "args": {"base": 1}}},
            "args_list": [{"n": 1}, {"n": 2}],
        },
        {},
        ctx,
    )
    assert len(payloads) == 2
    assert payloads[0]["impls"]["python"]["args"] == {"base": 1, "n": 1}
    assert payloads[1]["impls"]["python"]["args"]["n"] == 2
    assert run_python_impl(payloads[1]["impls"]["python"]) == 2
    reduced = plugin.reduce({}, results_from(payloads), ctx)
    assert [r["result"] for r in reduced] == [1, 2]


def test_custom_shell_args_become_env(registry: Registry, ctx: FakeCtx) -> None:
    payloads = registry.require("custom").split(
        {"impls": {"shell": {"cmd": "echo $PC_ARG_N"}}, "args_list": [{"n": 5}]}, {}, ctx
    )
    assert payloads[0]["impls"]["shell"]["env"]["PC_ARG_N"] == "5"


def test_custom_targets_all(registry: Registry, ctx: FakeCtx) -> None:
    payloads = registry.require("custom").split(
        {"impls": {"shell": {"cmd": "echo"}}}, {"targets": "all"}, ctx
    )
    assert [p["device_id"] for p in payloads] == ["dev1", "dev2"]


def test_custom_validation(registry: Registry, ctx: FakeCtx) -> None:
    plugin = registry.require("custom")
    with pytest.raises(ValueError):
        plugin.split({"impls": {}}, {}, ctx)
    with pytest.raises(ValueError):
        plugin.split({"impls": {"perl": {}}}, {}, ctx)
    with pytest.raises(ValueError):
        plugin.split({"impls": {"shell": {"cmd": "x"}}, "args_list": "nope"}, {}, ctx)
