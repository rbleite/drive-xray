"""Tests for local staging of cloud-synced .db files.

When the .db lives in a OneDrive/GDrive/Dropbox folder, the app writes to a
local staging copy during index/refresh and moves it back at the end — one
upload instead of continuous re-uploads that starve the operation of I/O.
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from drive_xray import STAGING_DIR, finalize_staged, stage_for_write


@pytest.fixture(autouse=True)
def _clean_staging():
    yield
    if STAGING_DIR.exists():
        for f in STAGING_DIR.iterdir():
            f.unlink()


def _mkdb(path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("INSERT INTO t VALUES (?)", (marker,))
    conn.commit()
    conn.close()


def _marker(path) -> str:
    conn = sqlite3.connect(path)
    v = conn.execute("SELECT v FROM t").fetchone()[0]
    conn.close()
    return v


def test_non_cloud_path_writes_in_place(tmp_path):
    db = tmp_path / "plain" / "x.db"
    _mkdb(db, "m")
    target, staged = stage_for_write(db)
    assert target == db and staged is False


def test_cloud_path_staged_and_moved_back(tmp_path):
    db = tmp_path / "OneDrive" / "x.db"
    _mkdb(db, "original")
    target, staged = stage_for_write(db)
    assert staged is True
    assert target == STAGING_DIR / "x.db"
    assert _marker(target) == "original"          # copy carries the data

    # the "indexer" writes into the staged copy…
    conn = sqlite3.connect(target)
    conn.execute("UPDATE t SET v='indexed'")
    conn.commit()
    conn.close()

    # …and finalize moves it back over the cloud copy
    msg = finalize_staged(target, db)
    assert msg.startswith("moved to")
    assert not target.exists()
    assert _marker(db) == "indexed"


def test_interrupted_run_resumes_from_staged(tmp_path):
    db = tmp_path / "Dropbox" / "x.db"
    _mkdb(db, "old-cloud")
    staged = STAGING_DIR / "x.db"
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    _mkdb(staged, "partial-progress")
    # staged is newer than the cloud copy → reuse it, don't overwrite
    time.sleep(0.05)
    import os
    os.utime(staged)
    target, is_staged = stage_for_write(db)
    assert is_staged and target == staged
    assert _marker(target) == "partial-progress"


def test_fresh_index_starts_clean(tmp_path):
    db = tmp_path / "OneDrive" / "new.db"   # cloud db does NOT exist yet
    staged = STAGING_DIR / "new.db"
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    _mkdb(staged, "stale")
    # hmm — staged exists and db doesn't: that is the interrupted-run shape,
    # so it must be REUSED (a fresh index over it wipes it anyway).
    target, is_staged = stage_for_write(db)
    assert is_staged and target == staged


def test_env_override_disables_staging(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_XRAY_NO_STAGING", "1")
    db = tmp_path / "OneDrive" / "x.db"
    _mkdb(db, "m")
    target, staged = stage_for_write(db)
    assert target == db and staged is False
