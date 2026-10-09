"""The `\\\\?\\` prefix the Rust engine stored as a Windows drive root.

Rust's canonicalize returns `\\\\?\\E:\\` on Windows, and dx < 1.6.1 wrote that
into drive.root_path, where the Python engine writes `E:\\` for the same
drive. The app showed it, _migrate_windows_seps did not recognise it as a
Windows root, and every f"{root}/{rel}" built from it opted out of Win32 path
normalisation.

The same case table as util::tests::strip_verbatim_cases in the Rust engine:
the two must agree, or a db repaired by one engine is re-broken by the other.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import drive_xray as dx

CASES = [
    (r"\\?\E:" + "\\", "E:\\"),
    (r"\\?\E:", "E:\\"),
    (r"\\?\e:\Films\Old", r"e:\Films\Old"),
    (r"\\?\UNC\nas\share", r"\\nas\share"),
    (r"\\?\UNC\nas\share\x", r"\\nas\share\x"),
    (r"\\?\unc\nas\share", r"\\nas\share"),
    # left alone: the prefix is what makes these work
    (r"\\?\UNC\nas", r"\\?\UNC\nas"),
    (r"\\?\Volume{0b1e}" + "\\", r"\\?\Volume{0b1e}" + "\\"),
    (r"\\?\E:\CON", r"\\?\E:\CON"),
    (r"\\?\E:\lpt1.txt", r"\\?\E:\lpt1.txt"),
    (r"\\?\E:\trailing.", r"\\?\E:\trailing."),
    (r"\\?\E:\trailing ", r"\\?\E:\trailing "),
    (r"\\?\E:\a/b", r"\\?\E:\a/b"),
    # ordinary names that merely look reserved
    (r"\\?\E:\COM0", r"E:\COM0"),
    (r"\\?\E:\CONSOLE", r"E:\CONSOLE"),
    # not verbatim at all
    ("E:\\", "E:\\"),
    ("/Volumes/X", "/Volumes/X"),
    (r"\\nas\share", r"\\nas\share"),
    ("", ""),
]


@pytest.mark.parametrize("given, want", CASES)
def test_strip_verbatim(given, want):
    assert dx.strip_verbatim(given) == want


def test_a_path_over_max_path_keeps_its_prefix():
    """Past 260 characters the prefix is what lets Windows open it at all."""
    long = "\\\\?\\E:\\" + "a" * 300
    assert dx.strip_verbatim(long) == long


def _db_with_root(tmp_path: Path, root: str) -> Path:
    db = tmp_path / "old.db"
    conn = dx.open_db(db)
    conn.execute(
        "INSERT INTO drive (label, root_path, indexed_at, total_files,"
        " total_dirs, total_size) VALUES ('4TbMyBook', ?, '2026-01-01', 0, 0, 0)",
        (root,))
    conn.commit()
    conn.close()
    return db


def _stored(db: Path) -> str:
    conn = dx.open_db(db)
    try:
        return conn.execute("SELECT root_path FROM drive").fetchone()[0]
    finally:
        conn.close()


def test_opening_an_index_written_by_old_dx_repairs_the_root(tmp_path):
    """The user's 4TbMyBook, exactly as it arrived on the Mac."""
    db = _db_with_root(tmp_path, "\\\\?\\E:\\")
    assert _stored(db) == "E:\\"


def test_the_repair_lets_the_backslash_migration_see_a_windows_root(tmp_path):
    """_migrate_windows_seps only acts on a root starting with a drive letter.
    Behind the prefix it never recognised one."""
    db = _db_with_root(tmp_path, "\\\\?\\E:\\")
    raw = sqlite3.connect(db)
    raw.execute("INSERT INTO snapshots (id, taken_at) VALUES (1, '2026-01-01')")
    pid = raw.execute("INSERT INTO paths (parent_id, segment, full_path)"
                      " VALUES (NULL, 'Films\\x.mkv', 'Films\\x.mkv')").lastrowid
    raw.execute("INSERT INTO entries_core (snapshot_id, path_id, is_dir, size)"
                " VALUES (1, ?, 0, 10)", (pid,))
    raw.commit()
    raw.close()
    conn = dx.open_db(db)
    try:
        got = conn.execute("SELECT full_path FROM paths"
                           " WHERE full_path LIKE 'Films%'").fetchone()[0]
    finally:
        conn.close()
    assert got == "Films/x.mkv"


@pytest.mark.parametrize("root", ["/Volumes/4TbMyBook", "E:\\", r"\\nas\media"])
def test_ordinary_roots_are_left_alone(tmp_path, root):
    db = _db_with_root(tmp_path, root)
    assert _stored(db) == root


def test_resolve_root_drops_the_prefix_without_the_migration(tmp_path):
    """A reader that opened the file read-only never ran the migration."""
    conn = dx.open_db(tmp_path / "x.db")
    try:
        got = dx.resolve_root(conn, "\\\\?\\E:\\", candidates=[])
    finally:
        conn.close()
    assert str(got) == str(Path("E:\\"))
