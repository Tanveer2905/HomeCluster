"""Saving files into the project's own ``data/downloads`` folder (SPEC §10).

The dashboard's Save buttons ask the coordinator to write the file there itself:
nothing lands in the browser's Downloads folder (or anywhere else outside the
project), and a big video or pooled-storage file never has to fit in the
browser's memory.  The file is written to a ``.part`` file first and gets its
name when complete; an existing name is kept and the new file becomes
``name (1).ext``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, AsyncIterator, BinaryIO, Callable, Dict, Optional

log = logging.getLogger("coordinator.downloads")

INVALID = set('<>:"/\\|?*')
RESERVED = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)} | {"LPT%d" % i for i in range(1, 10)}
MAX_NAME = 150


def safe_name(name: Optional[str], fallback: str = "download") -> str:
    """A file name that is valid on Windows and can only point into the folder."""
    base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join("_" if (ch in INVALID or ord(ch) < 32) else ch for ch in base).strip(" .")
    if not cleaned:
        cleaned = fallback
    if cleaned.split(".", 1)[0].upper() in RESERVED:
        cleaned = "_" + cleaned
    if len(cleaned) > MAX_NAME:
        stem, dot, ext = cleaned.rpartition(".")
        ext = dot + ext if dot and len(ext) <= 10 else ""
        cleaned = cleaned[:MAX_NAME - len(ext)] + ext
    return cleaned


def reveal(path: Path) -> None:
    """Show ``path`` (a file or folder) in the PC's file manager."""
    if os.name == "nt":
        if path.is_file():
            subprocess.Popen('explorer /select,"%s"' % path)
        else:
            os.startfile(str(path))  # type: ignore[attr-defined]
    else:  # pragma: no cover - the coordinator runs on Windows
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, str(path if path.is_dir() else path.parent)])


class Downloads:
    def __init__(self, data: Path) -> None:
        self.dir = Path(data) / "downloads"
        self._naming = threading.Lock()

    # ---------------------------------------------------------------- saving
    async def save(self, name: str, write: Callable[[BinaryIO], Any]) -> Dict[str, Any]:
        """Save a file named like ``name``; ``write(handle)`` fills it (in a thread)."""
        part = self._part()
        try:
            await asyncio.to_thread(self._fill, part, write)
            return await asyncio.to_thread(self._finish, part, name)
        except BaseException:
            self._discard(part)
            raise

    async def save_stream(self, name: str, chunks: AsyncIterator[bytes]) -> Dict[str, Any]:
        """Save a file named like ``name`` from an async stream of blocks."""
        part = self._part()
        try:
            with open(part, "wb") as handle:
                async for block in chunks:
                    await asyncio.to_thread(handle.write, block)
            return await asyncio.to_thread(self._finish, part, name)
        except BaseException:
            self._discard(part)
            raise

    def _part(self) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        return self.dir / (".saving-%s.part" % secrets.token_hex(6))

    @staticmethod
    def _fill(part: Path, write: Callable[[BinaryIO], Any]) -> None:
        with open(part, "wb") as handle:
            write(handle)

    @staticmethod
    def _discard(part: Path) -> None:
        try:
            part.unlink()
        except OSError:
            pass

    def _finish(self, part: Path, name: str) -> Dict[str, Any]:
        wanted = safe_name(name)
        stem, dot, ext = wanted.rpartition(".")
        if not dot or not stem:
            stem, ext = wanted, ""
        with self._naming:
            target = self.dir / wanted
            number = 1
            while target.exists():
                target = self.dir / ("%s (%d)%s" % (stem, number, "." + ext if ext else ""))
                number += 1
            os.replace(part, target)
        size = target.stat().st_size
        log.info("saved %s (%d bytes) to %s", target.name, size, self.dir)
        return {"ok": True, "name": target.name, "path": str(target), "folder": str(self.dir), "size": size}

    # ---------------------------------------------------------------- showing
    def show(self, name: Optional[str] = None) -> Dict[str, Any]:
        """Open the folder, with ``name`` selected when it is there."""
        self.dir.mkdir(parents=True, exist_ok=True)
        target = self.dir / safe_name(name) if name else self.dir
        reveal(target if target.exists() else self.dir)
        return {"ok": True, "folder": str(self.dir)}
