"""Job packages: manifest + files -> one submit (coordinator/packages.py)."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any, Dict

import httpx
import pytest

from coordinator import packages
from coordinator.packages import PackageError, Resolver, build, unpack_zip

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def write_tree(root: Path, files: Dict[str, Any]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        elif isinstance(content, str):
            path.write_text(content, encoding="utf-8")
        else:
            path.write_text(json.dumps(content), encoding="utf-8")
    return root


def zip_bytes(files: Dict[str, Any], prefix: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, content in files.items():
            if isinstance(content, bytes):
                data = content
            elif isinstance(content, str):
                data = content.encode("utf-8")
            else:
                data = json.dumps(content).encode("utf-8")
            zf.writestr(prefix + rel, data)
    return buf.getvalue()


async def post_package(client: httpx.AsyncClient, data: bytes, **params: Any) -> httpx.Response:
    return await client.post(
        "/api/packages", params=params, files={"file": ("pkg.zip", data, "application/zip")}
    )


MAP_CODE = "def count(item, ctx):\n    return len(item)\n"

WORDCOUNT = {
    "job.yaml": (
        "name: wordcount\n"
        "type: python_map\n"
        "params:\n"
        "  code: \"@count.py\"\n"
        "  fn_name: count\n"
        "  items: \"@items.json\"\n"
        "  inputs: \"@texts/*.txt\"\n"
        "options:\n"
        "  timeout: 30\n"
        "priority: 2\n"
    ),
    "count.py": MAP_CODE,
    "items.json": ["a", "bb", "ccc"],
    "texts/a.txt": "alpha",
    "texts/b.txt": "beta",
    "texts/skip.md": "not a txt",
}


# --------------------------------------------------------------------------
# resolver / build (no HTTP)
# --------------------------------------------------------------------------

async def test_build_resolves_every_reference_kind(cluster: Any, tmp_path: Path) -> None:
    root = write_tree(tmp_path / "pkg", WORDCOUNT)
    built = build(root, cluster)
    assert built["kind"] == "job"
    spec = built["spec"]
    assert spec["type"] == "python_map"
    assert spec["priority"] == 2
    assert spec["options"] == {"timeout": 30}
    params = spec["params"]
    assert params["code"] == MAP_CODE                      # code -> inlined text
    assert params["fn_name"] == "count"                    # untouched
    assert params["items"] == ["a", "bb", "ccc"]           # json -> parsed
    assert len(params["inputs"]) == 2                      # files -> uploaded ids (glob)
    names = sorted(u["name"] for u in built["uploaded"])
    assert names == ["texts/a.txt", "texts/b.txt"]
    for file_id in params["inputs"]:
        assert cluster.files.meta(file_id) is not None
    assert cluster.files.read(params["inputs"][0]) == b"alpha"


async def test_dry_run_uploads_nothing(cluster: Any, tmp_path: Path) -> None:
    root = write_tree(tmp_path / "pkg", WORDCOUNT)
    built = build(root, cluster, dry_run=True)
    assert built["spec"]["params"]["inputs"] == ["<upload:texts/a.txt>", "<upload:texts/b.txt>"]
    assert built["uploaded"] == []
    assert cluster.files.list() == []


async def test_explicit_forms_work_nested(cluster: Any, tmp_path: Path) -> None:
    root = write_tree(tmp_path / "pkg", {
        "job.yaml": (
            "type: custom\n"
            "params:\n"
            "  impls:\n"
            "    python:\n"
            "      code: {$text: main.py}\n"
            "      entry: run\n"
            "      args: {$json: args.yaml}\n"
            "  inputs: [{$file: data.bin}]\n"
            "  args_list:\n"
            "    - {extra: {$files: 'more/*.csv'}}\n"
        ),
        "main.py": "def run(args, ctx):\n    return args\n",
        "args.yaml": "k: v\nn: 3\n",
        "data.bin": b"\x00\x01",
        "more/1.csv": "1", "more/2.csv": "2",
    })
    spec = build(root, cluster)["spec"]
    impls = spec["params"]["impls"]
    assert impls["python"]["code"].startswith("def run")
    assert impls["python"]["args"] == {"k": "v", "n": 3}
    assert len(spec["params"]["inputs"]) == 1
    assert cluster.files.read(spec["params"]["inputs"][0]) == b"\x00\x01"
    assert len(spec["params"]["args_list"][0]["extra"]) == 2


async def test_pipeline_manifest(cluster: Any, tmp_path: Path) -> None:
    root = write_tree(tmp_path / "pkg", {
        "job.yaml": (
            "name: two-step\n"
            "jobs:\n"
            "  - key: primes\n"
            "    type: prime_range\n"
            "    params: {start: 1, end: 100}\n"
            "  - key: report\n"
            "    type: python_map\n"
            "    depends_on: [primes]\n"
            "    params:\n"
            "      code: \"@report.py\"\n"
            "      fn_name: fmt\n"
            "      items: [{$from_job: primes, path: count}]\n"
        ),
        "report.py": "def fmt(item, ctx):\n    return 'found %s' % item\n",
    })
    built = build(root, cluster)
    assert built["kind"] == "pipeline" and built["name"] == "two-step"
    assert [j["key"] for j in built["jobs"]] == ["primes", "report"]
    assert built["jobs"][1]["depends_on"] == ["primes"]
    assert built["jobs"][1]["params"]["items"] == [{"$from_job": "primes", "path": "count"}]


@pytest.mark.parametrize("files,message", [
    ({"job.yaml": "type: nope\nparams: {}\n"}, "unknown job type"),
    ({"job.yaml": "params: {}\n"}, "missing 'type'"),
    ({"readme.txt": "x"}, "no job.yaml"),
    ({"job.yaml": "type: [\n"}, "not valid"),
    ({"job.yaml": "- a\n"}, "must contain a mapping"),
    ({"job.yaml": "type: python_map\nparams: {code: '@missing.py'}\n"}, "not found in package"),
    ({"job.yaml": "type: python_map\nparams: {code: '@../secret.py'}\n"}, "leaves the package"),
    ({"job.yaml": "type: python_map\nparams: {chunk_size: '@n.txt'}\n", "n.txt": "5"}, "cannot come from a file"),
    ({"job.yaml": "type: python_map\nparams: {inputs: ['@*.bin']}\n"}, "matched nothing"),
    ({"job.yaml": "type: python_map\nparams: {inputs: ['@a.txt', 'abc123']}\n", "a.txt": "x"}, "mixes"),
    ({"job.yaml": "jobs: []\n"}, "non-empty"),
    ({"job.yaml": "jobs:\n  - {key: a, type: shell, params: {cmd: x}}\n  - {key: a, type: shell, params: {cmd: y}}\n"}, "unique"),
])
async def test_bad_manifests_are_package_errors(cluster: Any, tmp_path: Path, files: Dict[str, Any], message: str) -> None:
    root = write_tree(tmp_path / "pkg", files)
    with pytest.raises(PackageError) as info:
        build(root, cluster)
    assert message in str(info.value)


async def test_undeclared_param_uses_extension(cluster: Any, tmp_path: Path) -> None:
    # `custom` declares `impls`/`inputs`/`args_list`/`requires`; `note` is undeclared
    root = write_tree(tmp_path / "pkg", {
        "job.yaml": "type: custom\nparams:\n  impls: {shell: {cmd: echo}}\n  note: '@n.txt'\n  meta: '@m.json'\n",
        "n.txt": "plain text", "m.json": {"a": 1},
    })
    params = build(root, cluster)["spec"]["params"]
    assert params["note"] == "plain text"
    assert params["meta"] == {"a": 1}


async def test_same_file_uploaded_once(cluster: Any, tmp_path: Path) -> None:
    root = write_tree(tmp_path / "pkg", {
        "job.yaml": "type: media\nparams:\n  op: thumbnail\n  inputs: ['@a.mp4', '@*.mp4']\n",
        "a.mp4": b"vid",
    })
    built = build(root, cluster)
    assert len(built["spec"]["params"]["inputs"]) == 1
    assert len(built["uploaded"]) == 1


# --------------------------------------------------------------------------
# zip handling
# --------------------------------------------------------------------------

async def test_unpack_strips_a_common_folder(tmp_path: Path) -> None:
    data = zip_bytes({"job.yaml": "type: shell\n", "a/b.txt": "x"}, prefix="myjob/")
    archive = tmp_path / "p.zip"
    archive.write_bytes(data)
    root = unpack_zip(archive, tmp_path / "out")
    assert (root / "job.yaml").is_file()
    assert (root / "a" / "b.txt").read_text() == "x"


async def test_unpack_rejects_zip_slip(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("job.yaml", "type: shell\n")
        zf.writestr("../../evil.txt", "x")
    archive = tmp_path / "p.zip"
    archive.write_bytes(buf.getvalue())
    with pytest.raises(PackageError) as info:
        unpack_zip(archive, tmp_path / "out")
    assert "escapes" in str(info.value)
    assert not (tmp_path / "evil.txt").exists()


async def test_unpack_rejects_absolute_and_garbage(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("/etc/passwd", "x")
    archive = tmp_path / "p.zip"
    archive.write_bytes(buf.getvalue())
    with pytest.raises(PackageError):
        unpack_zip(archive, tmp_path / "out")
    archive.write_bytes(b"this is not a zip")
    with pytest.raises(PackageError) as info:
        unpack_zip(archive, tmp_path / "out2")
    assert "not a zip" in str(info.value)


async def test_unpack_skips_pycache_and_git(tmp_path: Path) -> None:
    data = zip_bytes({"job.yaml": "type: shell\n", "__pycache__/x.pyc": b"", ".git/HEAD": "ref"})
    archive = tmp_path / "p.zip"
    archive.write_bytes(data)
    root = unpack_zip(archive, tmp_path / "out")
    assert not (root / "__pycache__").exists() and not (root / ".git").exists()


# --------------------------------------------------------------------------
# HTTP endpoint
# --------------------------------------------------------------------------

async def test_post_package_submits_a_job(client: httpx.AsyncClient, cluster: Any) -> None:
    res = await post_package(client, zip_bytes(WORDCOUNT, prefix="wordcount/"))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["kind"] == "job"
    job = cluster.db.get("jobs", body["job_id"])
    assert job is not None and job["type"] == "python_map" and job["name"] == "wordcount"
    assert job["params"]["code"] == MAP_CODE
    assert len(body["uploaded"]) == 2
    kinds = [e["kind"] for e in cluster.db.select("events", "ORDER BY id DESC LIMIT 5")]
    assert "package_submitted" in kinds
    # the temp unpack dir is gone
    assert not list((Path(cluster.data) / "tmp").glob("pkg-*"))


async def test_post_package_dry_run(client: httpx.AsyncClient, cluster: Any) -> None:
    res = await post_package(client, zip_bytes(WORDCOUNT), dry_run="true")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["kind"] == "job"
    assert body["spec"]["params"]["inputs"] == ["<upload:texts/a.txt>", "<upload:texts/b.txt>"]
    assert cluster.db.select("jobs") == []
    assert cluster.files.list() == []


async def test_post_package_pipeline(client: httpx.AsyncClient, cluster: Any) -> None:
    files = {
        "job.json": {
            "name": "pipe",
            "jobs": [
                {"key": "a", "type": "prime_range", "params": {"start": 1, "end": 50}},
                {"key": "b", "type": "python_map", "depends_on": ["a"],
                 "params": {"code": "@f.py", "fn_name": "f", "items": [{"$from_job": "a", "path": "count"}]}},
            ],
        },
        "f.py": "def f(item, ctx):\n    return item\n",
    }
    res = await post_package(client, zip_bytes(files))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["kind"] == "pipeline"
    assert set(body["jobs"]) == {"a", "b"}
    b = cluster.db.get("jobs", body["jobs"]["b"])
    assert b["status"] == "waiting" and b["depends_on"] == ["a"]  # keys, resolved on promotion
    assert b["pipeline_id"] == body["pipeline_id"]


async def test_post_package_errors_are_400(client: httpx.AsyncClient) -> None:
    res = await post_package(client, zip_bytes({"job.yaml": "type: nope\n"}))
    assert res.status_code == 400 and "unknown job type" in res.json()["error"]
    res = await post_package(client, b"not a zip")
    assert res.status_code == 400 and "not a zip" in res.json()["error"]
    res = await post_package(client, zip_bytes({"x.txt": "no manifest"}))
    assert res.status_code == 400 and "job.yaml" in res.json()["error"]


async def test_post_package_needs_pin(anon: httpx.AsyncClient) -> None:
    res = await post_package(anon, zip_bytes({"job.yaml": "type: shell\nparams: {cmd: x}\n"}))
    assert res.status_code == 401


async def test_package_job_runs_end_to_end(client: httpx.AsyncClient, cluster: Any, device: Any) -> None:
    """A packaged python_map actually executes on a FakeDevice and returns."""
    res = await post_package(client, zip_bytes(WORDCOUNT))
    job_id = res.json()["job_id"]
    from conftest import wait_for  # noqa: WPS433

    async def serve() -> bool:
        for cmd in await device.poll():
            if cmd.get("cmd") == "task":
                await device.run_task(cmd)
        job = cluster.db.get("jobs", job_id)
        return job["status"] in ("done", "failed")

    await wait_for(serve, timeout=30)
    job = cluster.db.get("jobs", job_id)
    assert job["status"] == "done", job.get("error")
    assert job["result"] == [1, 2, 3]
