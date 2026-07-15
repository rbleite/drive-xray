"""Tests for cross-platform root resolution (resolve_root).

A drive indexed on macOS stores root_path like "/Volumes/MyDisk"; on Windows
the same disk mounts at "E:\\" (and on Linux at /media/<user>/MyDisk).
resolve_root() must find the volume at its NEW mount point by matching the
top-level entries recorded in the latest snapshot.
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from conftest import dx_py

from drive_xray import resolve_root, _stored_subpaths


def _conn(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(db)


def _index(root: Path, db: Path) -> None:
    r = dx_py("index", str(root), "--db", str(db), "--label", "test")
    assert r.returncode == 0, r.stderr


def test_existing_root_returned_unchanged(tmp_drive, tmp_path):
    db = tmp_path / "a.db"
    _index(tmp_drive, db)
    conn = _conn(db)
    assert resolve_root(conn, str(tmp_drive)) == tmp_drive
    conn.close()


def test_resolves_volume_at_new_mount_point(tmp_drive, tmp_path):
    """Simulates mac→windows: the stored root no longer exists, but the same
    content is mounted under a different name elsewhere."""
    db = tmp_path / "a.db"
    _index(tmp_drive, db)

    mounts = tmp_path / "mounts"
    mounts.mkdir()
    new_mount = mounts / "E"
    shutil.move(str(tmp_drive), str(new_mount))

    conn = _conn(db)
    resolved = resolve_root(conn, str(tmp_drive),
                            candidates=[new_mount])
    conn.close()
    assert resolved == new_mount


def test_rejects_unrelated_volume(tmp_drive, tmp_path):
    """A mounted volume with different content must NOT be matched."""
    db = tmp_path / "a.db"
    _index(tmp_drive, db)
    shutil.rmtree(tmp_drive)

    other = tmp_path / "other_disk"
    other.mkdir()
    (other / "something_else.txt").write_text("nope\n")

    conn = _conn(db)
    resolved = resolve_root(conn, str(tmp_drive), candidates=[other])
    conn.close()
    assert resolved == tmp_drive  # falls back to the stored (unmounted) root


def test_prefers_candidate_with_matching_content(tmp_drive, tmp_path):
    db = tmp_path / "a.db"
    _index(tmp_drive, db)

    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "alpha.txt").write_text("x")  # 1 top-level name in common

    new_mount = tmp_path / "real"
    shutil.move(str(tmp_drive), str(new_mount))

    conn = _conn(db)
    resolved = resolve_root(conn, str(tmp_drive),
                            candidates=[decoy, new_mount])
    conn.close()
    assert resolved == new_mount


def test_resolves_subfolder_index(tmp_path):
    """Index root was a folder INSIDE the volume (/Volumes/X/Backups) — on
    the new machine it must resolve to <mount>/Backups."""
    vol = tmp_path / "Volumes" / "X"
    sub = vol / "Backups"
    sub.mkdir(parents=True)
    (sub / "keep.txt").write_text("data\n")
    (sub / "photos").mkdir()
    (sub / "photos" / "p1.jpg").write_bytes(b"jpg")

    db = tmp_path / "a.db"
    _index(sub, db)

    mounts = tmp_path / "mnt"
    mounts.mkdir()
    new_mount = mounts / "xdisk"
    shutil.move(str(vol), str(new_mount))
    # make _stored_subpaths applicable by faking the stored root shape:
    # the actual stored root is tmp_path/Volumes/X/Backups which no longer
    # exists; candidates get the subpath re-applied via /Volumes pattern.
    stored = "/Volumes/X/Backups"

    conn = _conn(db)
    resolved = resolve_root(conn, stored, candidates=[new_mount])
    conn.close()
    assert resolved == new_mount / "Backups"


def test_stored_subpaths_patterns():
    assert _stored_subpaths("/Volumes/X") == []
    assert _stored_subpaths("/Volumes/X/Backups/2020") == ["Backups/2020"]
    assert _stored_subpaths("E:\\Backups") == ["Backups"]
    assert _stored_subpaths("E:\\") == []
    assert _stored_subpaths("/media/rleite/X/sub") == ["X/sub", "sub"]
    assert _stored_subpaths("/run/media/rleite/X/sub") == ["sub"]
    assert _stored_subpaths("/Users/rleite") == []
