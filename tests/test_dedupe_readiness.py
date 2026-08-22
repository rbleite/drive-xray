"""Why a cross-drive duplicate search found nothing.

cross_dedupe() drops a drive silently when its .db will not open or has no
snapshot, and it compares partial hashes without checking they came from the
same algorithm. Every one of those produces an empty result indistinguishable
from "you have no duplicates" — which is the worst possible way for a search to
fail, because the answer looks like an answer.

These tests build each of those situations for real and assert the reason is
named.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from conftest import dx_py

from drive_xray import cross_dedupe, dedupe_readiness


@pytest.fixture()
def two_drives(tmp_path):
    """Two drives sharing one 2 MB file, plus one file unique to A."""
    a, b = tmp_path / "A" / "docs", tmp_path / "B" / "backup"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    shared = b"\xab" * (2 * 1024 * 1024)
    (a / "shared.bin").write_bytes(shared)
    (b / "shared.bin").write_bytes(shared)
    (a / "only-a.bin").write_bytes(b"\xcd" * (2 * 1024 * 1024))

    da, db_ = tmp_path / "a.db", tmp_path / "b.db"
    assert dx_py("index", str(tmp_path / "A"), "--db", str(da),
                 "--label", "DriveA").returncode == 0
    assert dx_py("index", str(tmp_path / "B"), "--db", str(db_),
                 "--label", "DriveB").returncode == 0
    return [(da, "DriveA"), (db_, "DriveB")]


def test_a_healthy_pair_has_nothing_to_report(two_drives):
    r = dedupe_readiness(two_drives)
    assert r["can_compare"] is True
    assert r["reasons"] == []
    assert r["usable"] == 2
    # and the search itself really does find the shared file
    assert len(cross_dedupe(two_drives)) == 1


def test_a_db_that_will_not_open_is_named(two_drives, tmp_path):
    """cross_dedupe swallows this in an `except Exception: continue`."""
    junk = tmp_path / "broken.db"
    junk.write_bytes(b"not a database at all")
    r = dedupe_readiness(two_drives + [(junk, "Broken")])
    assert any("Broken" in why and "could not be opened" in why
               for why in r["reasons"])


def test_an_index_with_no_snapshot_is_named(two_drives, tmp_path):
    """An interrupted index leaves a .db with tables but no snapshot. The
    search skips it without a word."""
    empty = tmp_path / "empty.db"
    from drive_xray import open_db
    open_db(empty).close()                      # schema, but nothing indexed
    r = dedupe_readiness(two_drives + [(empty, "NeverFinished")])
    assert any("NeverFinished" in why and "no snapshot" in why
               for why in r["reasons"])


def test_a_drive_below_the_size_threshold_is_named(tmp_path):
    """Raising the threshold past everything is an easy way to get an empty
    result and blame the tool."""
    root = tmp_path / "small"
    root.mkdir()
    (root / "tiny.txt").write_bytes(b"x" * 100)
    db = tmp_path / "s.db"
    assert dx_py("index", str(root), "--db", str(db),
                 "--label", "Tiny").returncode == 0
    r = dedupe_readiness([(db, "Tiny")], min_size=1024 * 1024)
    assert any("Tiny" in why and "size threshold" in why for why in r["reasons"])
    assert r["can_compare"] is False


def test_one_usable_drive_is_not_a_comparison(two_drives, tmp_path):
    r = dedupe_readiness(two_drives[:1])
    assert r["can_compare"] is False
    assert any("at least 2" in why for why in r["reasons"])


def test_mismatched_hash_versions_are_named(two_drives):
    """The silent one. Hashes made by different algorithms never match, so the
    search completes normally, reports nothing, and gives no hint why."""
    _, (db_b, _label) = two_drives[0], two_drives[1]
    conn = sqlite3.connect(db_b)
    conn.execute("UPDATE drive SET hash_version=1")
    conn.commit()
    conn.close()

    r = dedupe_readiness(two_drives)
    assert r["can_compare"] is False
    assert any("versions differ" in why for why in r["reasons"])
    assert r["hash_versions"] == [1, 2]


def test_the_reason_is_given_even_when_every_drive_reads_fine(two_drives):
    """A version mismatch is the case where nothing looks wrong: both drives
    readable, both with snapshots, plenty of eligible files."""
    conn = sqlite3.connect(two_drives[1][0])
    conn.execute("UPDATE drive SET hash_version=1")
    conn.commit()
    conn.close()

    r = dedupe_readiness(two_drives)
    assert all(d["readable"] and d["has_snapshot"] and d["eligible"] > 0
               for d in r["drives"]), "nothing about the drives looks wrong"
    assert r["reasons"], "yet there must still be an explanation"


def test_readiness_reads_no_files(two_drives, tmp_path):
    """It has to be cheap enough to run every time the panel is opened, so it
    must never touch the drive itself — only the index."""
    import drive_xray as dx

    def _explode(*a, **k):
        raise AssertionError("readiness must not hash or read files")

    original_partial, original_full = dx.partial_hash, dx.full_hash
    dx.partial_hash, dx.full_hash = _explode, _explode
    try:
        dedupe_readiness(two_drives)
    finally:
        dx.partial_hash, dx.full_hash = original_partial, original_full
