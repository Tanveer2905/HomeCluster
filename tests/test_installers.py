"""The join installers for other computers (SPEC §3): the Termux / Linux / Mac shell
script, the Windows PowerShell script and the portable Python the PC lends it.

The real one-liners run end to end in test_portable_e2e.py."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from coordinator.static_routes import INSTALL_PS1, INSTALL_SH, portable_python

CA = "-----BEGIN CERTIFICATE-----\nMIIBszCCAVmgAwIBAgIU\n-----END CERTIFICATE-----\n"


def fake_python(root: Path) -> Path:
    """A ``tools/python`` the way scripts/get-python.ps1 leaves it."""
    folder = root / "tools" / "python"
    (folder / "Lib" / "site-packages").mkdir(parents=True)
    (folder / "Scripts").mkdir()
    (folder / "python.exe").write_bytes(b"exe")
    (folder / "python312.dll").write_bytes(b"dll")
    (folder / "python312.zip").write_bytes(b"stdlib")
    (folder / "python312._pth").write_text("python312.zip\n.\nLib\\site-packages\n..\\..\n..\\..\\sdk\n")
    (folder / "Lib" / "site-packages" / "fastapi.py").write_text("")
    (folder / "Scripts" / "pip.exe").write_bytes(b"pip")
    return folder


def test_the_pc_lends_its_interpreter_and_standard_library_only(tmp_path: Path) -> None:
    folder = fake_python(tmp_path / "root")
    data = tmp_path / "data"
    packed, sha = portable_python(tmp_path / "root", data)
    assert packed.parent == data / "tmp"
    with zipfile.ZipFile(packed) as pack:
        assert sorted(pack.namelist()) == ["python.exe", "python312._pth", "python312.dll", "python312.zip"]
        # isolated on the laptop: its own standard library and folder, no project paths
        assert pack.read("python312._pth").decode() == "python312.zip\n.\n"
    assert sha == hashlib.sha256(packed.read_bytes()).hexdigest()
    assert portable_python(tmp_path / "root", data) == (packed, sha)  # packed once
    (folder / "python312.dll").write_bytes(b"a newer dll")             # ... and again after a change
    again, sha2 = portable_python(tmp_path / "root", data)
    assert again != packed and not packed.exists() and sha2 != sha


def test_no_python_to_lend_without_tools_python(tmp_path: Path) -> None:
    assert portable_python(tmp_path, tmp_path / "data") is None


def windows_script(sha: str = "ab" * 32) -> str:
    return (INSTALL_PS1.replace("{URL}", "https://192.168.1.5:8765").replace("{CODE}", "ABCD-EFGH")
            .replace("{SPKI}", "sha256//q1w2e3+/=").replace("{PYZIP}", sha).replace("{CA}", CA))


def test_the_windows_script_is_plain_ascii() -> None:
    # PowerShell reads curl's output in the console's code page
    assert INSTALL_PS1.isascii()


@pytest.mark.skipif(not shutil.which("powershell"), reason="needs Windows PowerShell")
def test_the_windows_script_is_valid_powershell(tmp_path: Path) -> None:
    script = tmp_path / "install.ps1"
    script.write_text(windows_script(), encoding="ascii")
    check = ("$errors = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
             "'%s', [ref]$null, [ref]$errors); if ($errors) { $errors | ForEach-Object { $_.ToString() }; "
             "exit 1 }" % script)
    proc = subprocess.run(["powershell", "-NoProfile", "-Command", check], capture_output=True, text=True,
                          timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_shell_script_finds_python3_and_installs_only_on_termux() -> None:
    assert INSTALL_SH.startswith("#!/bin/sh\n")
    assert "command -v python3 || command -v python" in INSTALL_SH
    assert "*com.termux*) pkg install -y python" in INSTALL_SH
    assert 'exec "$PY" worker.py' in INSTALL_SH


@pytest.mark.skipif(not shutil.which("sh"), reason="needs sh")
def test_the_shell_script_is_valid_sh(tmp_path: Path) -> None:
    script = tmp_path / "install.sh"
    script.write_bytes(INSTALL_SH.replace("{CA}", CA).encode("ascii"))
    proc = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
