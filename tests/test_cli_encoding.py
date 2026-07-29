"""CLI output must survive a non-UTF-8 console.

Windows consoles default to the ANSI code page (cp1252), which cannot encode
the box-drawing and arrow characters this CLI prints ('↳', '→', '−', '─', '⚠').
Printing one raised UnicodeEncodeError and killed the command outright: on
Windows `dedupe` died on its first hardlink note, and `diff`/`doctor` on their
summary rules. `main()` now forces UTF-8 on stdout/stderr.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import DX_PY

REPO = Path(__file__).parent.parent

# characters the CLI prints that do NOT exist in cp1252
NON_CP1252 = "↳→−─⚠"


def _run_cp1252(*args) -> subprocess.CompletedProcess:
    """Run the CLI as a Windows console would: a legacy single-byte code page."""
    env = os.environ.copy()
    env["DRIVE_XRAY_NO_REGISTRY"] = "1"
    env["PYTHONIOENCODING"] = "cp1252"
    return subprocess.run([*DX_PY, *args], capture_output=True, text=True, env=env)


@pytest.fixture()
def hardlink_db(tmp_path):
    """A drive with a hardlink pair — enough to make `dedupe` print '↳'."""
    drv = tmp_path / "drive"
    (drv / "sub").mkdir(parents=True)
    (drv / "a.bin").write_bytes(b"A" * 1_200_000)
    os.link(drv / "a.bin", drv / "sub" / "a_link.bin")
    db = tmp_path / "e.db"
    r = _run_cp1252("index", str(drv), "--db", str(db), "--label", "E")
    assert r.returncode == 0, r.stderr
    return db


def test_the_characters_really_are_unencodable_in_cp1252():
    """Guards the premise: if these ever became encodable the tests below
    would pass for the wrong reason."""
    for ch in NON_CP1252:
        with pytest.raises(UnicodeEncodeError):
            ch.encode("cp1252")


def test_dedupe_survives_a_legacy_console(hardlink_db):
    r = _run_cp1252("dedupe", str(hardlink_db), "--min-size", "1000000")
    assert "UnicodeEncodeError" not in r.stderr
    assert r.returncode == 0, r.stderr


def test_doctor_survives_a_legacy_console(hardlink_db):
    r = _run_cp1252("doctor", str(hardlink_db))
    assert "UnicodeEncodeError" not in r.stderr
    assert r.returncode == 0, r.stderr


def test_diff_survives_a_legacy_console(hardlink_db):
    assert _run_cp1252("snapshot", "take", str(hardlink_db)).returncode == 0
    r = _run_cp1252("diff", str(hardlink_db))
    assert "UnicodeEncodeError" not in r.stderr
    assert r.returncode == 0, r.stderr
