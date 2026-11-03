"""Job packages: a folder or zip with ``job.yaml`` plus code and data files.

A package turns "upload three files, copy their ids into a jobspec, submit"
into one step.  The manifest is an ordinary jobspec (SPEC §5) or pipeline
(SPEC §5.1) whose values may point at files inside the package:

* ``"@path"`` shorthand at the top level of ``params`` is resolved by the
  plugin's declared param type:
    - ``code`` / ``str``  -> the file's text
    - ``file``            -> uploaded, replaced by its file id
    - ``files``           -> one or more globs, uploaded, replaced by ids
    - ``json``            -> parsed (``.yaml``/``.yml`` are parsed as YAML)
    - anything else       -> error (a number cannot come from a file)
  Params the plugin does not declare are treated as ``json`` when the file
  ends in ``.json``/``.yaml``/``.yml`` and as text otherwise.
* Explicit forms work anywhere, including inside nested dicts and lists:
  ``{"$text": "path"}``, ``{"$file": "path"}``, ``{"$files": "glob"}``
  (a list of globs is fine too) and ``{"$json": "path"}``.

Paths are relative to the package root and may not leave it.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import shutil
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

import yaml

log = logging.getLogger("coordinator.packages")

MANIFEST_NAMES = ("job.yaml", "job.yml", "job.json")
MAX_UNPACKED_BYTES = 1024 * 1024 * 1024  # 1 GiB
MAX_ENTRIES = 10_000
DATA_EXT = (".json", ".yaml", ".yml")


class PackageError(ValueError):
    """Anything wrong with the package itself (a 400 for the API)."""


# --------------------------------------------------------------------- unpack
def _safe_member(name: str) -> Optional[PurePosixPath]:
    """Relative, normalised member path, or ``None`` for entries to skip/reject."""
    raw = name.replace("\\", "/")
    if not raw or raw.endswith("/"):
        return None
    path = PurePosixPath(raw)
    if path.is_absolute() or raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        raise PackageError("zip entry with an absolute path: %s" % name)
    if any(part == ".." for part in path.parts):
        raise PackageError("zip entry escapes the package: %s" % name)
    if any(part in ("__pycache__", ".git") for part in path.parts):
        return None
    return path


def _strip_common_root(members: List[Tuple[PurePosixPath, Any]]) -> List[Tuple[PurePosixPath, Any]]:
    """``pkg/job.yaml, pkg/a.py`` -> ``job.yaml, a.py`` (zips made from a folder)."""
    if not members:
        return members
    if any(path.name in MANIFEST_NAMES and len(path.parts) == 1 for path, _ in members):
        return members
    roots = {path.parts[0] for path, _ in members if len(path.parts) > 1}
    if len(roots) != 1 or any(len(path.parts) == 1 for path, _ in members):
        return members
    return [(PurePosixPath(*path.parts[1:]), info) for path, info in members]


def unpack_zip(archive: Path, dest: Path) -> Path:
    """Extract ``archive`` into ``dest`` safely; returns the package root."""
    try:
        zf = zipfile.ZipFile(str(archive))
    except zipfile.BadZipFile as exc:
        raise PackageError("not a zip file: %s" % exc)
    with zf:
        members: List[Tuple[PurePosixPath, Any]] = []
        total = 0
        for info in zf.infolist():
            path = _safe_member(info.filename)
            if path is None:
                continue
            total += int(info.file_size or 0)
            if total > MAX_UNPACKED_BYTES:
                raise PackageError("package exceeds %d MB unpacked" % (MAX_UNPACKED_BYTES >> 20))
            members.append((path, info))
            if len(members) > MAX_ENTRIES:
                raise PackageError("package has more than %d files" % MAX_ENTRIES)
        members = _strip_common_root(members)
        dest.mkdir(parents=True, exist_ok=True)
        root = dest.resolve()
        for path, info in members:
            target = (dest / Path(*path.parts)).resolve()
            if root not in target.parents and target != root:
                raise PackageError("zip entry escapes the package: %s" % info.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
    return dest


# ------------------------------------------------------------------- manifest
def find_manifest(root: Path) -> Path:
    for name in MANIFEST_NAMES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    raise PackageError("package has no %s" % " / ".join(MANIFEST_NAMES))


def load_manifest(root: Path) -> Dict[str, Any]:
    path = find_manifest(root)
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    except (ValueError, yaml.YAMLError) as exc:
        raise PackageError("%s is not valid: %s" % (path.name, exc))
    if not isinstance(data, dict):
        raise PackageError("%s must contain a mapping" % path.name)
    return data


# ------------------------------------------------------------------ resolving
class Resolver:
    """Turns package-relative references into inline text, parsed data or file ids."""

    def __init__(self, root: Path, state: Any, dry_run: bool = False) -> None:
        self.root = root.resolve()
        self.state = state
        self.dry_run = dry_run
        self.uploaded: List[Dict[str, Any]] = []
        self._ids: Dict[Path, str] = {}

    # -- paths -------------------------------------------------------------
    def path(self, ref: str) -> Path:
        ref = str(ref).strip().replace("\\", "/")
        if not ref or ref.startswith("/") or (len(ref) > 1 and ref[1] == ":"):
            raise PackageError("reference must be relative to the package: %r" % ref)
        target = (self.root / ref).resolve()
        if self.root not in target.parents and target != self.root:
            raise PackageError("reference leaves the package: %r" % ref)
        if not target.is_file():
            raise PackageError("referenced file not found in package: %r" % ref)
        return target

    def glob(self, pattern: str) -> List[Path]:
        pattern = str(pattern).strip().replace("\\", "/")
        if not any(ch in pattern for ch in "*?["):
            return [self.path(pattern)]
        if pattern.startswith("/") or ".." in PurePosixPath(pattern).parts:
            raise PackageError("glob must stay inside the package: %r" % pattern)
        matches = sorted(
            p for p in self.root.rglob("*")
            if p.is_file()
            and fnmatch.fnmatch(p.relative_to(self.root).as_posix(), pattern)
            and "__pycache__" not in p.parts
        )
        if not matches:
            raise PackageError("glob matched nothing in package: %r" % pattern)
        return matches

    # -- leaf conversions --------------------------------------------------
    def text(self, ref: str) -> str:
        return self.path(ref).read_text(encoding="utf-8")

    def data(self, ref: str) -> Any:
        path = self.path(ref)
        raw = path.read_text(encoding="utf-8")
        try:
            if path.suffix.lower() in (".yaml", ".yml"):
                return yaml.safe_load(raw)
            return json.loads(raw)
        except (ValueError, yaml.YAMLError) as exc:
            raise PackageError("%s is not valid JSON/YAML: %s" % (ref, exc))

    def upload(self, path: Path) -> str:
        if path in self._ids:
            return self._ids[path]
        name = path.relative_to(self.root).as_posix()
        if self.dry_run:
            file_id = "<upload:%s>" % name
        else:
            meta = self.state.files.add_path(path, name)
            file_id = str(meta["id"])
            self.uploaded.append({"file_id": file_id, "name": name, "size": meta.get("size")})
        self._ids[path] = file_id
        return file_id

    def files(self, patterns: Any) -> List[str]:
        if isinstance(patterns, str):
            patterns = [patterns]
        out: List[str] = []
        for pattern in patterns or []:
            for path in self.glob(str(pattern)):
                file_id = self.upload(path)
                if file_id not in out:
                    out.append(file_id)
        return out

    # -- generic walk ------------------------------------------------------
    def explicit(self, value: Any) -> Any:
        """Resolve ``$text/$file/$files/$json`` forms anywhere in ``value``."""
        if isinstance(value, dict):
            keys = set(value)
            if keys == {"$text"}:
                return self.text(value["$text"])
            if keys == {"$file"}:
                return self.upload(self.path(value["$file"]))
            if keys == {"$files"}:
                return self.files(value["$files"])
            if keys == {"$json"}:
                return self.data(value["$json"])
            if any(k.startswith("$") and k in ("$text", "$file", "$files", "$json") for k in keys):
                raise PackageError("a $text/$file/$files/$json object takes no other keys: %s" % sorted(keys))
            return {k: self.explicit(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.explicit(v) for v in value]
        return value

    def by_type(self, name: str, value: Any, ptype: Optional[str]) -> Any:
        """Resolve a top-level ``@`` shorthand according to the plugin's param type."""
        if ptype == "files":
            refs = value if isinstance(value, list) else [value]
            if all(isinstance(v, str) and v.startswith("@") for v in refs):
                return self.files([v[1:] for v in refs])
            if any(isinstance(v, str) and v.startswith("@") for v in refs):
                raise PackageError("param %r mixes @references with plain file ids" % name)
            return self.explicit(value)
        if not (isinstance(value, str) and value.startswith("@")):
            return self.explicit(value)
        ref = value[1:]
        if ptype in ("code", "str"):
            return self.text(ref)
        if ptype == "file":
            return self.upload(self.path(ref))
        if ptype == "json":
            return self.data(ref)
        if ptype is None:
            return self.data(ref) if ref.lower().endswith(DATA_EXT) else self.text(ref)
        raise PackageError("param %r has type %s and cannot come from a file" % (name, ptype))

    def params(self, plugin: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        types: Dict[str, Optional[str]] = {}
        if plugin is not None:
            for spec in getattr(plugin, "params", []) or []:
                if spec.get("name"):
                    types[str(spec["name"])] = spec.get("type")
        return {k: self.by_type(k, v, types.get(k)) for k, v in (params or {}).items()}


# ------------------------------------------------------------------- building
def _jobspec(resolver: Resolver, registry: Any, raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise PackageError("each job must be a mapping")
    job_type = raw.get("type")
    if not job_type:
        raise PackageError("job is missing 'type'")
    plugin = registry.get(str(job_type))
    if plugin is None:
        raise PackageError("unknown job type %r" % job_type)
    spec: Dict[str, Any] = {
        "type": str(job_type),
        "params": resolver.params(plugin, dict(raw.get("params") or {})),
        "options": resolver.explicit(dict(raw.get("options") or {})),
        "priority": int(raw.get("priority") or 0),
        "depends_on": [str(d) for d in (raw.get("depends_on") or [])],
    }
    if raw.get("name"):
        spec["name"] = str(raw["name"])
    if raw.get("key") is not None:
        spec["key"] = str(raw["key"])
    return spec


def build(root: Path, state: Any, dry_run: bool = False) -> Dict[str, Any]:
    """Load + resolve a package.  Returns ``{"kind": "job"|"pipeline", ...}``."""
    manifest = load_manifest(root)
    resolver = Resolver(root, state, dry_run=dry_run)
    registry = state.plugins
    if "jobs" in manifest:
        jobs = manifest.get("jobs")
        if not isinstance(jobs, list) or not jobs:
            raise PackageError("'jobs' must be a non-empty list")
        specs = [_jobspec(resolver, registry, job) for job in jobs]
        keys = [s.get("key") for s in specs]
        if len([k for k in keys if k]) != len(set(k for k in keys if k)):
            raise PackageError("pipeline keys must be unique")
        return {
            "kind": "pipeline",
            "name": manifest.get("name"),
            "jobs": specs,
            "uploaded": resolver.uploaded,
        }
    spec = _jobspec(resolver, registry, manifest)
    spec.pop("key", None)
    return {"kind": "job", "spec": spec, "uploaded": resolver.uploaded}


def submit(root: Path, state: Any, dry_run: bool = False) -> Dict[str, Any]:
    """Build and (unless ``dry_run``) submit; returns the API response body."""
    built = build(root, state, dry_run=dry_run)
    if dry_run:
        return built
    if built["kind"] == "pipeline":
        result = state.submit_pipeline(built.get("name"), built["jobs"])
        result["kind"] = "pipeline"
    else:
        job = state.submit_job(built["spec"])
        result = {"kind": "job", "job_id": job["id"], "job": job}
    result["uploaded"] = built["uploaded"]
    state.event(
        "package_submitted",
        "package submitted: %s" % (built.get("name") or built.get("spec", {}).get("name") or built["kind"]),
        job_id=result.get("job_id"),
        data={"files": len(built["uploaded"])},
    )
    return result
