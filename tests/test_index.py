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


def test_python_doctor_detects_empty_snapshot_metadata(tmp_path):
    """Doctor must fail when snapshot metadata says files exist but entries are empty."""
    import sys; sys.path.insert(0, str(Path(__file__).parent.parent))
    from drive_xray import doctor_db

    db = tmp_path / "bad.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE drive (label TEXT, root_path TEXT, hash_version INTEGER);
        CREATE TABLE snapshots (id INTEGER PRIMARY KEY, taken_at TEXT, label TEXT, total_files INTEGER, total_dirs INTEGER, total_size INTEGER);
        CREATE TABLE paths (id INTEGER PRIMARY KEY, rel_path TEXT);
        CREATE TABLE entries (id INTEGER PRIMARY KEY, snapshot_id INTEGER, path_id INTEGER, is_dir INTEGER, is_symlink INTEGER DEFAULT 0, size INTEGER, partial_hash BLOB);
        CREATE INDEX idx_snap_parent ON entries(snapshot_id);
        CREATE INDEX idx_snap_size_partial ON entries(snapshot_id,size,partial_hash);
        CREATE INDEX idx_full ON entries(partial_hash);
        CREATE INDEX idx_snap_inode ON entries(snapshot_id);
        CREATE INDEX idx_snap_path_id ON entries(snapshot_id,path_id);
    """)
    conn.execute("INSERT INTO drive VALUES ('bad', '/', 2)")
    conn.execute("INSERT INTO snapshots VALUES (1, '2026-01-01T00:00:00', 'bad', 10, 2, 1000)")
    conn.commit(); conn.close()

    r = doctor_db(db)
    assert not r["ok"]
    assert any(c["name"] == "entry_counts" and not c["ok"] for c in r["checks"])


# ── symlinks ──────────────────────────────────────────────────────────────────

def test_symlink_indexed_not_followed(tmp_path):
    """Symlinks must be stored with is_symlink=1 and no hash.
    Symlinks to directories must not be followed — their contents stay absent."""
    d = tmp_path / "drive"
    d.mkdir()

    (d / "real.txt").write_text("hello\n")
    (d / "link.txt").symlink_to(d / "real.txt")  # valid symlink to a file

    sub = d / "subdir"
    sub.mkdir()
    (sub / "data.bin").write_bytes(b"\xca\xfe" * 50)
    (d / "linkdir").symlink_to(sub)              # symlink to a directory — must NOT be followed

    db = tmp_path / "sym.db"
    r = dx_py("index", str(d), "--db", str(db), "--label", "sym")
    assert r.returncode == 0, r.stderr

    sid = _sid(db)

    # file symlink: indexed with is_symlink=1, no hash
    syms = _query(db,
        "SELECT rel_path, partial_hash FROM entries WHERE snapshot_id=? AND is_symlink=1",
        sid)
    sym_names = {row[0] for row in syms}
    assert "link.txt" in sym_names, "file symlink must be indexed with is_symlink=1"
    for _, ph in syms:
        assert ph is None, "symlinks must not have partial_hash"

    # dir symlink itself is indexed as is_symlink=1
    linkdir_row = _query(db,
        "SELECT is_symlink FROM entries WHERE snapshot_id=? AND rel_path='linkdir'", sid)
    assert linkdir_row and linkdir_row[0][0] == 1, "directory symlink must have is_symlink=1"

    # real.txt hashed independently of its symlink
    real = _query(db,
        "SELECT partial_hash FROM entries WHERE snapshot_id=? AND rel_path='real.txt'", sid)
    assert real and real[0][0] is not None, "real file must be hashed"

    # nothing under linkdir/ must appear — directory symlink must not be followed
    file_paths = {row[0] for row in _query(db,
        "SELECT rel_path FROM entries WHERE snapshot_id=? AND is_dir=0", sid)}
    assert not any(p.startswith("linkdir") for p in file_paths), \
        f"symlinked directory was followed — found: {[p for p in file_paths if p.startswith('linkdir')]}"

    # metadata total_files counts only hashed non-symlink files: real.txt + subdir/data.bin = 2
    meta = _query(db, "SELECT total_files FROM snapshots WHERE id=?", sid)[0][0]
    assert meta == 2, f"expected 2 hashed files in snapshot metadata, got {meta}"


# ── refresh / hash-cache ──────────────────────────────────────────────────────

def test_refresh_reuses_hashes(tmp_drive, tmp_path):
    """Refresh must reuse partial_hash for unchanged files and recompute only
    for files that changed (size or mtime shifted)."""
    db = tmp_path / "ref.db"
    r = dx_py("index", str(tmp_drive), "--db", str(db), "--label", "t")
    assert r.returncode == 0, r.stderr

    sid = _sid(db)
    before = {row[0]: row[1] for row in _query(db,
        "SELECT rel_path, hex(partial_hash) FROM entries "
        "WHERE snapshot_id=? AND is_dir=0 AND partial_hash IS NOT NULL", sid)}

    # Modify one file so its mtime changes
    (tmp_drive / "alpha.txt").write_text("completely different content\n")

    r2 = dx_py("refresh", str(db))
    assert r2.returncode == 0, r2.stderr

    # Refresh reports how many entries it reused from the cache
    assert "reusing" in r2.stderr, \
        f"refresh must report cache reuse in stderr; got: {r2.stderr!r}"

    sid2 = _sid(db)  # refresh overwrites the same snapshot
    after = {row[0]: row[1] for row in _query(db,
        "SELECT rel_path, hex(partial_hash) FROM entries "
        "WHERE snapshot_id=? AND is_dir=0 AND partial_hash IS NOT NULL", sid2)}

    # Modified file must get a fresh hash
    assert before.get("alpha.txt") != after.get("alpha.txt"), \
        "modified file must get a new hash after refresh"

    # Unchanged files must keep the same hash (cache was reused, not recomputed)
    for path in ("beta.txt", "dup_a.bin", "dup_b.bin"):
        assert before.get(path) == after.get(path), \
            f"{path} hash changed despite file being unchanged — cache not reused"


# ── schema migration ──────────────────────────────────────────────────────────

def test_schema_migration_v4_to_v5(tmp_path):
    """open_db() must transparently upgrade a v4 database (no paths table,
    no path_id column) to v5 (path interning with integer path_id join)."""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent.parent))

    db = tmp_path / "v4.db"

    # Build a minimal v4 schema manually — no paths table, no path_id column
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE drive (
            id INTEGER PRIMARY KEY, label TEXT, root_path TEXT NOT NULL,
            indexed_at TEXT NOT NULL, total_files INTEGER, total_dirs INTEGER,
            total_size INTEGER, hash_version INTEGER
        );
        CREATE TABLE snapshots (
            id INTEGER PRIMARY KEY, taken_at TEXT NOT NULL, label TEXT,
            total_files INTEGER, total_dirs INTEGER, total_size INTEGER,
            hash_version INTEGER
        );
        CREATE TABLE entries (
            id INTEGER PRIMARY KEY,
            snapshot_id INTEGER NOT NULL,
            rel_path TEXT NOT NULL,
            parent_id INTEGER,
            is_dir INTEGER NOT NULL,
            size INTEGER, mtime REAL, partial_hash BLOB, full_hash BLOB,
            is_symlink INTEGER DEFAULT 0, error TEXT, inode INTEGER, device INTEGER
        );
        CREATE UNIQUE INDEX idx_snap_path ON entries(snapshot_id, rel_path);
        INSERT INTO snapshots (taken_at, label, hash_version)
            VALUES ('2024-01-01T00:00:00', 'test', 2);
        INSERT INTO drive (label, root_path, indexed_at, hash_version)
            VALUES ('test', '/tmp/test', '2024-01-01T00:00:00', 2);
        INSERT INTO entries (snapshot_id, rel_path, parent_id, is_dir, size)
            VALUES (1, '.', NULL, 1, NULL);
        INSERT INTO entries (snapshot_id, rel_path, parent_id, is_dir, size)
            VALUES (1, 'subdir', 1, 1, NULL);
        INSERT INTO entries (snapshot_id, rel_path, parent_id, is_dir, size)
            VALUES (1, 'subdir/file.txt', 2, 0, 42);
    """)
    conn.commit()
    conn.close()

    from drive_xray import open_db
    conn2 = open_db(db)

    # v5: paths table must exist
    tables = {r[0] for r in conn2.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "paths" in tables, "migration must create the paths table"

    # v5: entries.path_id column must exist and be fully populated
    cols = {r[1] for r in conn2.execute("PRAGMA table_info(entries)")}
    assert "path_id" in cols, "migration must add path_id column to entries"

    null_pids = conn2.execute(
        "SELECT COUNT(*) FROM entries WHERE path_id IS NULL").fetchone()[0]
    assert null_pids == 0, f"{null_pids} entries still have NULL path_id after migration"

    # v5: integer-based index exists; old text-based index dropped
    indexes = {r[0] for r in conn2.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_snap_path_id" in indexes, "v5 index idx_snap_path_id must exist after migration"
    assert "idx_snap_path" not in indexes, \
        "old text index idx_snap_path must be dropped after v5 migration"

    conn2.close()
