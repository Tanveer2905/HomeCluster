"""``media`` -- ffmpeg transcode / resize / thumbnail / extract_audio (SPEC §7).

One task per input file.  With ``segment_seconds`` a video is cut into ranges,
one task per range, and :func:`reduce` concatenates the pieces back together on
the coordinator with ``tools/ffmpeg/ffmpeg.exe`` when it is present.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("coordinator.plugins.media")

TYPE = "media"
DESCRIPTION = "Transcode, resize, thumbnail or extract audio with ffmpeg"

OPS = ["transcode", "resize", "thumbnail", "extract_audio", "custom"]

PARAMS = [
    {"name": "op", "type": "select", "default": "transcode", "choices": OPS},
    {"name": "inputs", "type": "files", "default": [], "help": "file_ids to process."},
    {"name": "width", "type": "int", "default": 0, "help": "Output width (0 = keep)."},
    {"name": "height", "type": "int", "default": 0, "help": "Output height (0 = auto)."},
    {"name": "format", "type": "str", "default": "", "help": "Output extension (empty = jpg for images, mp4 for video, mp3 for audio)."},
    {"name": "quality", "type": "int", "default": 23, "help": "CRF for video, q:v for images."},
    {"name": "ffmpeg_args", "type": "str", "default": "", "help": "Extra raw ffmpeg arguments."},
    {"name": "segment_seconds", "type": "int", "default": 0, "help": "Split video into N-second tasks."},
]

DEFAULT_OPTIONS: Dict[str, Any] = {"requires": ["ffmpeg"], "timeout": 3600}
#: the same file with the same settings gives the same output: reuse it.  Not
#: checked by a second device: different ffmpeg builds encode slightly
#: differently, so outputs are not byte-identical across devices.
CACHE = True

_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


# --------------------------------------------------------------------- probe
def probe_duration(ffprobe: Optional[str], path: Path) -> Optional[float]:
    """Media duration in seconds via ffprobe, or ``None``."""
    if not ffprobe or not Path(path).is_file():
        return None
    try:
        out = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            timeout=60,
        )
        if out.returncode != 0:
            return None
        data = json.loads(out.stdout.decode("utf-8", "replace") or "{}")
        value = (data.get("format") or {}).get("duration")
        return float(value) if value is not None else None
    except Exception:  # pragma: no cover - probing is best effort
        log.debug("ffprobe failed for %s", path, exc_info=True)
        return None


# ------------------------------------------------------------------ commands
def _filters(params: Dict[str, Any]) -> List[str]:
    width = int(params.get("width") or 0)
    height = int(params.get("height") or 0)
    if width <= 0 and height <= 0:
        return []
    return ["-vf", "scale=%d:%d" % (width or -2, height or -2)]


def _op_args(
    op: str, params: Dict[str, Any], is_image: bool, src_ext: str = ""
) -> Tuple[List[str], str]:
    """Returns (ffmpeg arguments, output extension)."""
    quality = int(params.get("quality") or 23)
    fmt = str(params.get("format") or "").strip().lstrip(".")
    extra = shlex.split(str(params.get("ffmpeg_args") or ""))
    if op == "resize":
        # Keep an image's own format unless the caller asked for another.
        image_ext = src_ext if src_ext in ("jpg", "jpeg", "png", "webp") else "jpg"
        ext = fmt or (image_ext if is_image else "mp4")
        args = _filters(params)
        if is_image or ext in ("jpg", "jpeg", "png", "webp"):
            args += ["-q:v", str(max(1, min(31, quality)))]
        else:
            args += ["-c:v", "libx264", "-crf", str(quality), "-preset", "veryfast"]
        return args + extra, ext
    if op == "thumbnail":
        ext = fmt if fmt in ("jpg", "jpeg", "png", "webp") else "jpg"
        args = ["-frames:v", "1"] + _filters(params) + ["-q:v", str(max(1, min(31, quality)))]
        return args + extra, ext
    if op == "extract_audio":
        ext = fmt if fmt and fmt not in ("mp4", "mkv", "webm") else "mp3"
        return ["-vn", "-b:a", "192k"] + extra, ext
    if op == "custom":
        return extra, fmt or "out"
    # transcode
    ext = fmt or "mp4"
    args = _filters(params)
    args += ["-c:v", "libx264", "-crf", str(quality), "-preset", "veryfast", "-c:a", "aac"]
    return args + extra, ext


def _command(
    args: List[str],
    out_name: str,
    seek: Optional[Tuple[float, float]] = None,
    windows: bool = False,
) -> str:
    ff = '"%PC_FFMPEG%"' if windows else '"${PC_FFMPEG:-ffmpeg}"'
    src = '"%PC_INPUT_0%"' if windows else '"$PC_INPUT_0"'
    dst = (
        '"%PC_OUTPUT_DIR%\\' + out_name + '"'
        if windows
        else '"$PC_OUTPUT_DIR/' + out_name + '"'
    )
    parts = [ff, "-y", "-hide_banner", "-nostdin"]
    if seek is not None:
        parts += ["-ss", "%.3f" % seek[0], "-t", "%.3f" % seek[1]]
    parts += ["-i", src]
    parts += [a if windows else shlex.quote(a) for a in args]
    parts.append(dst)
    return " ".join(parts)


def split(params: Dict[str, Any], options: Dict[str, Any], ctx: Any) -> List[Dict[str, Any]]:
    op = str(params.get("op") or "transcode").lower()
    if op not in OPS:
        raise ValueError("op must be one of %s" % ", ".join(OPS))
    inputs = [str(f) for f in (params.get("inputs") or []) if f]
    if not inputs:
        raise ValueError("media needs at least one input file id")
    requires = list(dict.fromkeys(list(options.get("requires") or []) + ["ffmpeg"]))
    timeout = options.get("timeout") or 3600
    segment = int(params.get("segment_seconds") or 0)
    ffprobe = ctx.ffprobe()

    payloads: List[Dict[str, Any]] = []
    for file_id in inputs:
        meta = ctx.file_meta(file_id) or {}
        name = str(meta.get("name") or file_id)
        stem = Path(name).stem or "media"
        suffix = Path(name).suffix.lower()
        is_image = suffix in _IMAGE_EXT
        args, ext = _op_args(op, params, is_image, suffix.lstrip("."))
        ranges: List[Optional[Tuple[float, float]]] = [None]
        if segment > 0 and not is_image and op != "thumbnail":
            duration = probe_duration(ffprobe, ctx.file_path(file_id))
            if duration and duration > segment:
                ranges = []
                start = 0.0
                while start < duration:
                    ranges.append((start, min(float(segment), duration - start)))
                    start += segment
        for index, seek in enumerate(ranges):
            out_name = "%s.%s" % (stem, ext)
            if seek is not None:
                out_name = "%s.part%04d.%s" % (stem, index, ext)
            payloads.append(
                {
                    "impls": {
                        "shell": {
                            "cmd": _command(args, out_name, seek, windows=False),
                            "cmd_windows": _command(args, out_name, seek, windows=True),
                            "env": {},
                            "cwd": None,
                        }
                    },
                    "inputs": [file_id],
                    "timeout": timeout,
                    "requires": requires,
                    "meta": {
                        "file_id": file_id,
                        "name": name,
                        "segment": index if seek is not None else None,
                        "out_name": out_name,
                        "ext": ext,
                    },
                }
            )
    return payloads


# -------------------------------------------------------------------- reduce
def _concat(ctx: Any, ffmpeg: str, parts: List[Dict[str, Any]], out_name: str) -> Optional[Dict[str, Any]]:
    """Concatenate uploaded segment outputs into one file (demuxer, stream copy)."""
    tmp_root = Path(tempfile.mkdtemp(prefix="pc-concat-", dir=str(ctx.tmp_dir())))
    try:
        listing = tmp_root / "parts.txt"
        lines: List[str] = []
        for index, part in enumerate(parts):
            local = tmp_root / ("part%04d_%s" % (index, part.get("name") or "seg"))
            ctx.state.files.copy_to(str(part["file_id"]), local)
            lines.append("file '%s'" % str(local).replace("\\", "/").replace("'", "'\\''"))
        listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
        target = tmp_root / out_name
        proc = subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-nostdin", "-f", "concat", "-safe", "0",
             "-i", str(listing), "-c", "copy", str(target)],
            capture_output=True,
            timeout=1800,
        )
        if proc.returncode != 0 or not target.is_file():
            log.warning(
                "ffmpeg concat failed (%s): %s",
                proc.returncode,
                proc.stderr.decode("utf-8", "replace")[-500:],
            )
            return None
        return ctx.add_file_path(target, out_name)
    except Exception:
        log.exception("segment concatenation failed")
        return None
    finally:
        for path in sorted(tmp_root.rglob("*"), reverse=True):
            try:
                path.unlink() if path.is_file() else path.rmdir()
            except OSError:  # pragma: no cover
                pass
        try:
            tmp_root.rmdir()
        except OSError:  # pragma: no cover
            pass


def reduce(params: Dict[str, Any], results: List[Dict[str, Any]], ctx: Any) -> Any:
    by_file: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for item in results:
        meta = (item.get("meta") or {}) if isinstance(item.get("meta"), dict) else {}
        file_id = str(meta.get("file_id") or item.get("idx"))
        if file_id not in by_file:
            by_file[file_id] = []
            order.append(file_id)
        by_file[file_id].append(item)

    outputs: List[Dict[str, Any]] = []
    for file_id in order:
        items = sorted(by_file[file_id], key=lambda i: i.get("idx") or 0)
        meta = (items[0].get("meta") or {}) if isinstance(items[0].get("meta"), dict) else {}
        segmented = any(
            isinstance(i.get("meta"), dict) and i["meta"].get("segment") is not None
            for i in items
        )
        produced: List[Dict[str, Any]] = []
        for item in items:
            for out in item.get("outputs") or []:
                produced.append(out)
        entry: Dict[str, Any] = {
            "input": file_id,
            "name": meta.get("name"),
            "outputs": produced,
        }
        if segmented and len(produced) > 1:
            ffmpeg = ctx.ffmpeg()
            stem = Path(str(meta.get("name") or "media")).stem
            ext = str(meta.get("ext") or "mp4")
            joined_name = "%s.%s" % (stem, ext)
            if ffmpeg:
                joined = _concat(ctx, ffmpeg, produced, joined_name)
                if joined:
                    entry["joined"] = {
                        "file_id": joined["id"],
                        "name": joined["name"],
                        "size": joined["size"],
                    }
                else:
                    entry["joined_error"] = "ffmpeg concat failed; segments kept separate"
            else:
                entry["joined_error"] = "no ffmpeg on the coordinator; segments kept separate"
        outputs.append(entry)
    return {"files": outputs}
