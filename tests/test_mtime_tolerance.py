"""Tests for the tolerant mtime compare used by hash reuse on refresh.

exFAT stores local time, so an untouched file can appear shifted by whole
hours when the drive moves between OSes/timezones/DST interpretations —
which used to silently disable all hash reuse on cross-OS refreshes.
"""
from __future__ import annotations

import os
from pathlib import Path

from conftest import dx_py

from drive_xray import _mtimes_equivalent


def test_unit_vectors():
    # plain granularity window (FAT 2s, HFS+ 1s vs APFS 1ns)
    assert _mtimes_equivalent(1000.0, 1000.0)
    assert _mtimes_equivalent(1000.0, 1001.9)
    assert not _mtimes_equivalent(1000.0, 1003.0)
    # exFAT local-time hour shifts (either direction, incl. DST-style ±1h)
    assert _mtimes_equivalent(1000.0, 1000.0 + 3600.0)
    assert _mtimes_equivalent(1000.0 + 3600.0, 1000.0)
    assert _mtimes_equivalent(1000.0, 1000.0 + 5 * 3600.0 + 1.5)
    assert _mtimes_equivalent(1000.0, 1000.0 + 26 * 3600.0)
    # beyond ±26h or off the hour grid: a real modification
    assert not _mtimes_equivalent(1000.0, 1000.0 + 27 * 3600.0)
    assert not _mtimes_equivalent(1000.0, 1000.0 + 1800.0)
    assert not _mtimes_equivalent(1000.0, 1000.0 + 3600.0 + 30.0)


def test_refresh_reuses_hashes_after_hour_shift(tmp_drive, tmp_path):
    """Simulates mac→windows on exFAT: every mtime shifts by exactly +1h.
    The refresh must still reuse every cached hash."""
    db = tmp_path / "t.db"
    r = dx_py("index", str(tmp_drive), "--db", str(db), "--label", "t")
    assert r.returncode == 0, r.stderr

    for p in tmp_drive.rglob("*"):
        if p.is_file() and not p.is_symlink():
            st = p.stat()
            os.utime(p, (st.st_atime, st.st_mtime + 3600.0))

    r = dx_py("refresh", str(db))
    assert r.returncode == 0, r.stderr
    # stderr ends with e.g. "... [reused 7/7 cached hashes]"
    assert "[reused 7/7 cached hashes]" in r.stderr, r.stderr
