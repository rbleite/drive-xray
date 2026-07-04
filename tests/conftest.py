"""Shared fixtures for drive-xray tests."""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

DX_RUST = REPO / "rust" / "target" / "aarch64-apple-darwin" / "release" / "dx"
DX_PY   = [sys.executable, str(REPO / "drive_xray.py")]


def _test_env() -> dict:
    env = os.environ.copy()
    env["DRIVE_XRAY_NO_REGISTRY"] = "1"
    return env


def dx_rust(*args) -> subprocess.CompletedProcess:
    """Run the Rust dx binary; skip test if binary not built."""
    if not DX_RUST.exists():
        pytest.skip(f"Rust binary not found at {DX_RUST} — run bash build_rust.sh")
    return subprocess.run([str(DX_RUST), *args], capture_output=True, text=True, env=_test_env())


def dx_py(*args) -> subprocess.CompletedProcess:
    return subprocess.run([*DX_PY, *args], capture_output=True, text=True, env=_test_env())


@pytest.fixture()
def tmp_drive(tmp_path: Path) -> Path:
    """A small synthetic drive with known content."""
    d = tmp_path / "drive"
    d.mkdir()

    # unique files
    (d / "alpha.txt").write_text("hello world\n")
    (d / "beta.txt").write_text("different content\n")

    # duplicate pair (exact content)
    dup_content = b"duplicate bytes " * 64 * 1024  # 1 MB
    (d / "dup_a.bin").write_bytes(dup_content)
    (d / "dup_b.bin").write_bytes(dup_content)

    # sub-directory with its own duplicate
    sub = d / "subdir"
    sub.mkdir()
    (sub / "dup_c.bin").write_bytes(dup_content)
    (sub / "unique.log").write_text("log entry\n")

    # hardlink: same inode as dup_a.bin — must NOT count as wasted space
    hl_target = d / "dup_a.bin"
    hl_link   = d / "hardlink_a.bin"
    os.link(hl_target, hl_link)

    return d


@pytest.fixture()
def indexed_db(tmp_drive: Path, tmp_path: Path) -> Path:
    """Python-indexed DB of tmp_drive."""
    db = tmp_path / "test.db"
    r = dx_py("index", str(tmp_drive), "--db", str(db), "--label", "test")
    assert r.returncode == 0, r.stderr
    return db


@pytest.fixture()
def rust_indexed_db(tmp_drive: Path, tmp_path: Path) -> Path:
    """Rust-indexed DB of tmp_drive."""
    db = tmp_path / "test_rust.db"
    r = dx_rust("index", str(tmp_drive), "--db", str(db), "--label", "test")
    assert r.returncode == 0, r.stderr
    return db
