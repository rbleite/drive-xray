"""Schema v7 — de-duplicated paths (Snapshots V2, phase 1).

The path text used to be stored on every entries row, i.e. once per snapshot
per file, even though `paths` already held one row per distinct path. v7 keeps
it only in `paths.full_path`; the physical row store (`entries_core`) has no
rel_path, and a VIEW named `entries` re-exposes the historical columns so
every read query keeps working.
"""
from __future__ import annotations

import sqlite3

from conftest import dx_py

import drive_xray as dx


def _cols(conn, name) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({name})")}


def test_fresh_db_is_v7(tmp_path):
    conn = dx.open_db(tmp_path / "fresh.db")
    kind = conn.execute(
        "SELECT type FROM sqlite_master WHERE name='entries'").fetchone()
    assert kind and kind[0] == "view", "entries must be the compatibility view"
    # the view still presents the historical 14 columns, in order
    view_cols = [r[1] for r in conn.execute("PRAGMA table_info(entries)")]
    assert view_cols == [
        "id", "snapshot_id", "rel_path", "path_id", "parent_id", "is_dir",
        "size", "mtime", "partial_hash", "full_hash", "is_symlink", "error",
        "inode", "device",
    ]
    # ...but the row store does not carry the path text
    assert "rel_path" not in _cols(conn, "entries_core")
    assert "full_path" in _cols(conn, "paths")
    conn.close()


def test_path_text_stored_once_per_distinct_path(tmp_drive, tmp_path):
    """The point of v7: N snapshots of the same tree must not store the path
    text N times."""
    db = tmp_path / "a.db"
    assert dx_py("index", str(tmp_drive), "--db", str(db), "--label", "t").returncode == 0
    for _ in range(3):
        assert dx_py("snapshot", "take", str(db)).returncode == 0

    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT COUNT(*) FROM entries_core").fetchone()[0]
    paths = conn.execute("SELECT COUNT(*) FROM paths").fetchone()[0]
    distinct = conn.execute(
        "SELECT COUNT(DISTINCT rel_path) FROM entries").fetchone()[0]
    conn.close()

    assert rows > paths, "4 snapshots must share one paths row per path"
    assert paths == distinct, "one paths row per distinct path, no duplicates"


def test_view_serves_rel_path_from_paths(tmp_drive, tmp_path):
    """Reads through the view must return exactly what the old column did."""
    db = tmp_path / "b.db"
    assert dx_py("index", str(tmp_drive), "--db", str(db), "--label", "t").returncode == 0
    conn = sqlite3.connect(db)
    via_view = sorted(r[0] for r in conn.execute(
        "SELECT rel_path FROM entries WHERE rel_path != '.'"))
    via_paths = sorted(r[0] for r in conn.execute(
        "SELECT p.full_path FROM entries_core c JOIN paths p ON p.id = c.path_id"
        " WHERE p.full_path != '.'"))
    conn.close()
    assert via_view == via_paths
    assert "alpha.txt" in via_view and "subdir/unique.log" in via_view


def test_v6_db_migrates_without_data_loss(tmp_drive, tmp_path):
    """A pre-v7 db (entries as a real table, rel_path populated) must migrate
    on open with every row and every path preserved."""
    db = tmp_path / "c.db"
    assert dx_py("index", str(tmp_drive), "--db", str(db), "--label", "t").returncode == 0

    # rebuild a v6-shaped db from the v7 one: a real `entries` table carrying
    # rel_path, and `paths` without the materialized text
    raw = sqlite3.connect(db)
    before = sorted(raw.execute(
        "SELECT rel_path, is_dir, COALESCE(size,-1) FROM entries").fetchall())
    raw.executescript("""
        DROP VIEW entries;
        CREATE TABLE entries (
            id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL,
            rel_path TEXT NOT NULL, path_id INTEGER, parent_id INTEGER,
            is_dir INTEGER NOT NULL, size INTEGER, mtime REAL,
            partial_hash BLOB, full_hash BLOB, is_symlink INTEGER DEFAULT 0,
            error TEXT, inode INTEGER, device INTEGER);
        INSERT INTO entries SELECT c.id, c.snapshot_id, p.full_path, c.path_id,
            c.parent_id, c.is_dir, c.size, c.mtime, c.partial_hash, c.full_hash,
            c.is_symlink, c.error, c.inode, c.device
            FROM entries_core c JOIN paths p ON p.id = c.path_id;
        DROP TABLE entries_core;
        UPDATE paths SET full_path = NULL;
    """)
    raw.commit()
    raw.close()

    conn = dx.open_db(db)          # triggers _migrate_to_v7
    after = sorted(conn.execute(
        "SELECT rel_path, is_dir, COALESCE(size,-1) FROM entries").fetchall())
    kind = conn.execute(
        "SELECT type FROM sqlite_master WHERE name='entries'").fetchone()[0]
    conn.close()

    assert kind == "view"
    assert after == before, "migration must preserve every row verbatim"


def test_migration_is_idempotent(tmp_drive, tmp_path):
    db = tmp_path / "d.db"
    assert dx_py("index", str(tmp_drive), "--db", str(db), "--label", "t").returncode == 0
    conn = dx.open_db(db)
    n1 = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    conn.close()
    for _ in range(3):
        conn = dx.open_db(db)
        assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == n1
        conn.close()
