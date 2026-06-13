"""Tests for indexing correctness — Python and Rust must agree."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from conftest import dx_py, dx_rust, DX_RUST


def _query(db: Path, sql: str, *args):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _sid(db: Path) -> int:
    return _query(db, "SELECT id FROM snapshots ORDER BY id DESC LIMIT 1")[0][0]


# ── basic index ──────────────────────────────────────────────────────────────

def test_index_creates_db(tmp_drive, tmp_path):
    db = tmp_path / "out.db"
    r = dx_py("index", str(tmp_drive), "--db", str(db))
    assert r.returncode == 0
    assert db.exists()


def test_index_file_count(indexed_db, tmp_drive):
    sid = _sid(indexed_db)
    files = _query(indexed_db, "SELECT COUNT(*) FROM entries WHERE snapshot_id=? AND is_dir=0", sid)[0][0]
    # 6 files: alpha, beta, dup_a, dup_b, dup_c, unique.log, hardlink_a
    # hardlink shares inode with dup_a — still counted as a separate entry
    assert files == 7, f"expected 7 files, got {files}"


def test_index_dir_count(indexed_db):
    sid = _sid(indexed_db)
    dirs = _query(indexed_db, "SELECT COUNT(*) FROM entries WHERE snapshot_id=? AND is_dir=1", sid)[0][0]
    # root + subdir
    assert dirs == 2, f"expected 2 dirs, got {dirs}"


def test_partial_hashes_computed(indexed_db):
    sid = _sid(indexed_db)
    total = _query(indexed_db, "SELECT COUNT(*) FROM entries WHERE snapshot_id=? AND is_dir=0", sid)[0][0]
    hashed = _query(indexed_db, "SELECT COUNT(*) FROM entries WHERE snapshot_id=? AND is_dir=0 AND partial_hash IS NOT NULL", sid)[0][0]
    assert hashed == total, f"only {hashed}/{total} files have partial_hash"


# ── duplicate detection ───────────────────────────────────────────────────────

def test_duplicate_pair_found(indexed_db):
    """dup_a.bin, dup_b.bin, dup_c.bin all have the same content."""
    sid = _sid(indexed_db)
    rows = _query(
        indexed_db,
        "SELECT partial_hash, COUNT(*) FROM entries "
        "WHERE snapshot_id=? AND is_dir=0 AND partial_hash IS NOT NULL "
        "GROUP BY partial_hash HAVING COUNT(*)>1",
        sid,
    )
    assert len(rows) >= 1, "expected at least one duplicate group by partial_hash"
    counts = [r[1] for r in rows]
    # hardlink shares inode but IS a separate entry — 4 entries (dup_a, dup_b, dup_c, hardlink_a)
    assert max(counts) >= 3, f"largest dup group has {max(counts)}, expected ≥3"


def test_hardlink_dedup_in_dup_groups(indexed_db):
    """Hardlinks share an inode — they must be deduped in duplicate detection.

    Drive has: dup_a.bin, dup_b.bin, dup_c.bin (same content, 1 MB each) +
    hardlink_a.bin (hardlink to dup_a.bin, same inode).
    After inode-dedup there are 3 distinct copies, not 4.
    The duplicate group should have distinct_inodes <= 3 and hardlinks >= 1.
    """
    sid = _sid(indexed_db)
    # Find the partial_hash of the 1 MB dup content
    rows = _query(
        indexed_db,
        "SELECT partial_hash, COUNT(*), COUNT(DISTINCT inode) FROM entries "
        "WHERE snapshot_id=? AND is_dir=0 AND size>=1000000 "
        "GROUP BY partial_hash HAVING COUNT(*)>1",
        sid,
    )
    assert rows, "no large duplicate group found"
    ph, count, distinct_inodes = rows[0]
    assert count == 4, f"expected 4 entries (dup_a, dup_b, dup_c, hardlink_a), got {count}"
    assert distinct_inodes == 3, \
        f"expected 3 distinct inodes (hardlink shares one), got {distinct_inodes}"


# ── Python / Rust equivalence ─────────────────────────────────────────────────

@pytest.mark.skipif(not DX_RUST.exists(), reason="Rust binary not built")
def test_rust_and_python_same_file_count(indexed_db, rust_indexed_db):
    """Python and Rust index must produce the same file count."""
    sid_py   = _sid(indexed_db)
    sid_rust = _sid(rust_indexed_db)

    py_files   = _query(indexed_db,      "SELECT COUNT(*) FROM entries WHERE snapshot_id=? AND is_dir=0", sid_py)[0][0]
    rust_files = _query(rust_indexed_db, "SELECT COUNT(*) FROM entries WHERE snapshot_id=? AND is_dir=0", sid_rust)[0][0]
    assert py_files == rust_files, f"Python={py_files} Rust={rust_files}"


@pytest.mark.skipif(not DX_RUST.exists(), reason="Rust binary not built")
def test_rust_and_python_same_partial_hashes(indexed_db, rust_indexed_db):
    """Partial hashes must be identical between Python and Rust indexes."""
    sid_py   = _sid(indexed_db)
    sid_rust = _sid(rust_indexed_db)

    def rel_hashes(db, sid):
        rows = _query(db,
            "SELECT rel_path, hex(partial_hash) FROM entries "
            "WHERE snapshot_id=? AND is_dir=0 AND partial_hash IS NOT NULL "
            "ORDER BY rel_path", sid)
        return {r[0]: r[1] for r in rows}

    py_hashes   = rel_hashes(indexed_db,      sid_py)
    rust_hashes = rel_hashes(rust_indexed_db, sid_rust)

    assert set(py_hashes.keys()) == set(rust_hashes.keys()), \
        f"file sets differ: {set(py_hashes.keys()) ^ set(rust_hashes.keys())}"

    mismatches = {p: (py_hashes[p], rust_hashes[p])
                  for p in py_hashes if py_hashes[p] != rust_hashes[p]}
    assert not mismatches, f"hash mismatches: {mismatches}"


# ── snapshot diff ─────────────────────────────────────────────────────────────

def test_snapshot_diff_added_file(tmp_drive, tmp_path):
    """A file added between two snapshots shows up in added_count."""
    db = tmp_path / "snap.db"
    r = dx_py("index", str(tmp_drive), "--db", str(db), "--label", "t")
    assert r.returncode == 0, r.stderr

    (tmp_drive / "new_file.txt").write_text("brand new\n")
    r2 = dx_py("snapshot", "take", str(db))
    assert r2.returncode == 0, r2.stderr

    import sys; sys.path.insert(0, str(Path(__file__).parent.parent))
    from drive_xray import diff_snapshots
    snaps = _query(db, "SELECT id FROM snapshots ORDER BY id")
    assert len(snaps) == 2, f"expected 2 snapshots, got {len(snaps)}"
    diff = diff_snapshots(db, from_id=snaps[0][0], to_id=snaps[1][0])

    assert diff["added_count"] >= 1, f"expected added_count>=1, got {diff}"
    # top_growth or top_count should mention the new file
    mentioned = [t[0] for t in diff.get("top_growth", []) + diff.get("top_count", [])]
    assert any("new_file.txt" in m for m in mentioned), \
        f"new_file.txt not mentioned in diff summary: {diff}"


def test_snapshot_diff_removed_file(tmp_drive, tmp_path):
    """A file removed between two snapshots shows up in removed_count."""
    db = tmp_path / "snap.db"
    dx_py("index", str(tmp_drive), "--db", str(db), "--label", "t")

    (tmp_drive / "alpha.txt").unlink()
    dx_py("snapshot", "take", str(db))

    import sys; sys.path.insert(0, str(Path(__file__).parent.parent))
    from drive_xray import diff_snapshots
    snaps = _query(db, "SELECT id FROM snapshots ORDER BY id")
    diff = diff_snapshots(db, from_id=snaps[0][0], to_id=snaps[1][0])

    assert diff["removed_count"] >= 1, f"expected removed_count>=1, got {diff}"


# ── doctor ────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not DX_RUST.exists(), reason="Rust binary not built")
def test_doctor_passes_on_good_db(rust_indexed_db):
    r = dx_rust("doctor", str(rust_indexed_db))
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(not DX_RUST.exists(), reason="Rust binary not built")
def test_doctor_detects_missing_index(rust_indexed_db):
    """Drop an index and doctor should report a failure."""
    conn = sqlite3.connect(rust_indexed_db)
    conn.execute("DROP INDEX IF EXISTS idx_snap_size_partial")
    conn.commit()
    conn.close()
    # Reopen to flush WAL — Rust open_db sets WAL mode so pages may still be in WAL
    conn2 = sqlite3.connect(rust_indexed_db)
    conn2.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn2.close()
    r = dx_rust("doctor", str(rust_indexed_db))
    assert r.returncode == 1, f"doctor should exit 1 when index missing\n{r.stdout}"
    assert "idx_snap_size_partial" in r.stdout, r.stdout
