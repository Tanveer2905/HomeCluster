"""Content-addressed file store, ``data/files/<sha256>`` (SPEC §10)."""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import shutil
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterable, Iterator, List, Optional, Tuple

from .db import Database, now

log = logging.getLogger("coordinator.files")

CHUNK = 1024 * 1024


class FileStore:
    """SHA-256 addressed blob store with a metadata row per file."""

    def __init__(self, db: Database, root: Path) -> None:
        self.db = db
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths
    def path_for(self, file_id: str) -> Path:
        safe = "".join(ch for ch in file_id if ch in "0123456789abcdef")
        if len(safe) != 64:
            raise ValueError("bad file id")
        return self.root / safe

    # ------------------------------------------------------------------ write
    def add_bytes(self, data: bytes, name: str = "file", mime: Optional[str] = None) -> Dict[str, Any]:
        digest = hashlib.sha256(data).hexdigest()
        path = self.root / digest
        if not path.exists():
            tmp = self.root / (digest + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        return self._record(digest, name, len(data), mime)

    def add_stream(self, stream: Iterable[bytes], name: str = "file", mime: Optional[str] = None) -> Dict[str, Any]:
        tmp = self.root / ("upload-%s.tmp" % os.urandom(6).hex())
        sha = hashlib.sha256()
        size = 0
        with tmp.open("wb") as fh:
            for block in stream:
                if not block:
                    continue
                sha.update(block)
                size += len(block)
                fh.write(block)
        digest = sha.hexdigest()
        path = self.root / digest
        if path.exists():
            tmp.unlink(missing_ok=True)
        else:
            os.replace(tmp, path)
        return self._record(digest, name, size, mime)

    def add_path(self, src: Path, name: Optional[str] = None, mime: Optional[str] = None) -> Dict[str, Any]:
        src = Path(src)
        with src.open("rb") as fh:
            return self.add_stream(iter(lambda: fh.read(CHUNK), b""), name or src.name, mime)

    def _record(self, digest: str, name: str, size: int, mime: Optional[str]) -> Dict[str, Any]:
        if mime is None:
            mime = mimetypes.guess_type(name or "")[0] or "application/octet-stream"
        existing = self.db.get("files", digest)
        if existing is None:
            row = {
                "id": digest,
                "name": name or digest,
                "size": size,
                "mime": mime,
                "created": now(),
                "refs": 0,
            }
            self.db.insert("files", row)
            return row
        return existing

    # ------------------------------------------------------------------- read
    def meta(self, file_id: str) -> Optional[Dict[str, Any]]:
        return self.db.get("files", file_id)

    def read(self, file_id: str) -> bytes:
        return self.path_for(file_id).read_bytes()

    def open(self, file_id: str) -> BinaryIO:
        return self.path_for(file_id).open("rb")

    def iter_content(self, file_id: str, offset: int = 0, length: Optional[int] = None) -> Iterator[bytes]:
        """Stream a file, or the byte range ``[offset, offset+length)`` of it."""
        remaining = length
        with self.open(file_id) as fh:
            if offset:
                fh.seek(offset)
            while remaining is None or remaining > 0:
                want = CHUNK if remaining is None else min(CHUNK, remaining)
                block = fh.read(want)
                if not block:
                    return
                if remaining is not None:
                    remaining -= len(block)
                yield block

    def line_ranges(self, file_id: str, lines_per_range: int) -> List[Tuple[int, int, int]]:
        """Split a text file into ``(offset, length, first_line_index)`` ranges
        of ``lines_per_range`` lines each, scanning it once without loading it."""
        out: List[Tuple[int, int, int]] = []
        per = max(1, int(lines_per_range))
        start = 0
        first_line = 0
        count = 0
        pos = 0
        with self.open(file_id) as fh:
            for raw in fh:
                pos += len(raw)
                count += 1
                if count == per:
                    out.append((start, pos - start, first_line))
                    start = pos
                    first_line += count
                    count = 0
        if pos > start:
            out.append((start, pos - start, first_line))
        return out

    def list(self, limit: int = 200, offset: int = 0) -> List[Dict[str, Any]]:
        return self.db.select(
            "files", "ORDER BY created DESC LIMIT ? OFFSET ?", (limit, offset)
        )

    def response(self, file_id: str, range_header: Optional[str] = None) -> Any:
        """A ``StreamingResponse`` for the file, honouring ``Range: bytes=a-b``
        (HTTP 206) so workers can fetch a slice of a large input."""
        from fastapi.responses import StreamingResponse

        meta = self.meta(file_id)
        if meta is None:
            return None
        size = int(meta.get("size") or 0)
        name = str(meta.get("name") or file_id).replace('"', "")
        headers = {
            "Content-Disposition": 'attachment; filename="%s"' % name,
            "Accept-Ranges": "bytes",
        }
        media = meta.get("mime") or "application/octet-stream"
        rng = parse_range(range_header, size)
        if rng is None:
            headers["Content-Length"] = str(size)
            return StreamingResponse(self.iter_content(file_id), media_type=media, headers=headers)
        start, end = rng
        if start >= size or end < start:
            from fastapi.responses import Response

            return Response(status_code=416, headers={"Content-Range": "bytes */%d" % size})
        headers["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
        headers["Content-Length"] = str(end - start + 1)
        return StreamingResponse(
            self.iter_content(file_id, start, end - start + 1),
            status_code=206,
            media_type=media,
            headers=headers,
        )

    # ----------------------------------------------------------------- delete
    def delete(self, file_id: str) -> bool:
        meta = self.meta(file_id)
        if meta is None:
            return False
        try:
            self.path_for(file_id).unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            log.warning("could not remove file bytes for %s", file_id)
        self.db.execute("DELETE FROM files WHERE id=?", (file_id,))
        return True

    def add_ref(self, file_id: str, delta: int = 1) -> None:
        self.db.execute("UPDATE files SET refs = refs + ? WHERE id=?", (delta, file_id))

    # ------------------------------------------------------------------ misc
    def total_size(self) -> int:
        return int(self.db.scalar("SELECT COALESCE(SUM(size),0) FROM files", default=0))

    def copy_to(self, file_id: str, dest: Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.path_for(file_id), dest)
        return dest


def parse_range(header: Optional[str], size: int) -> Optional[Tuple[int, int]]:
    """``bytes=a-b`` / ``bytes=a-`` / ``bytes=-n`` -> inclusive ``(start, end)``."""
    if not header or not header.strip().lower().startswith("bytes="):
        return None
    spec = header.strip()[6:].split(",")[0].strip()
    if "-" not in spec:
        return None
    first, last = spec.split("-", 1)
    try:
        if first == "":
            n = int(last)
            return (max(0, size - n), size - 1)
        start = int(first)
        end = int(last) if last else size - 1
    except ValueError:
        return None
    return (start, min(end, size - 1))
