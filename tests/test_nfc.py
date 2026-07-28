"""Cross-OS Unicode normalization (NFC/NFD) regression tests.

macOS presents filenames decomposed (NFD: 'ç' = 'c' + combining cedilla),
Windows and Linux composed (NFC). The same file therefore has different
rel_path bytes depending on the OS that indexed it. drive-xray does NOT
rewrite the stored bytes (so an I/O path reconstructed from rel_path still
opens on normalization-sensitive filesystems like exFAT), but every rel_path
*comparison* folds through nfc() so a Mac↔Windows sync still matches:

  - refresh reuses hashes instead of re-hashing every accented file,
  - snapshot diff does not report phantom delete+add pairs,
  - resolve_root recognizes a volume whose top-level names are accented.
"""
from __future__ import annotations

import sqlite3
import unicodedata
from pathlib import Path

from conftest import dx_py

import drive_xray as dx

WORD = "Investigação"          # has a 'ç' + 'ã' → distinct NFC/NFD byte forms
NFD = lambda s: unicodedata.normalize("NFD", s)
NFC = lambda s: unicodedata.normalize("NFC", s)


def test_word_forms_differ():
    # guards the test itself: if these ever collapse the tests below are moot
    assert NFD(WORD) != NFC(WORD)
    assert NFC(WORD) == WORD


def _flip_stored_to_nfd(db: Path, snapshot_id: int | None = None) -> None:
    """Make the stored paths decomposed (NFD), to simulate a macOS-indexed db.

    Since v7 the path text is interned in `paths` and shared by every snapshot
    that references it, so this mirrors what really happens across a Mac↔
    Windows sync: each OS interns its own spelling of the name, producing a
    SEPARATE paths row (the interning key is (parent_id, segment), and the NFD
    segment differs from the NFC one). When `snapshot_id` is given, only that
    snapshot's rows are repointed to the NFD twins — the other snapshot keeps
    the NFC ones, which is exactly the cross-OS skew the diff must reconcile.
    """
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT id, parent_id, segment, full_path FROM paths"
        " WHERE full_path != '.' ORDER BY LENGTH(full_path)"
    ).fetchall()
    twin: dict[int, int] = {}          # NFC path id -> NFD path id
    for pid, parent, seg, fp in rows:
        if seg == NFD(seg) and fp == NFD(fp):
            continue                   # nothing to decompose
        new_parent = twin.get(parent, parent)
        cur = conn.execute(
            "INSERT INTO paths (parent_id, segment, full_path) VALUES (?,?,?)",
            (new_parent, NFD(seg), NFD(fp)),
        )
        twin[pid] = cur.lastrowid
    if twin:
        q = "UPDATE entries_core SET path_id=? WHERE path_id=?"
        params_tail = ""
        if snapshot_id is not None:
            q += " AND snapshot_id=?"
            params_tail = snapshot_id
        for old, new in twin.items():
            args = (new, old) if snapshot_id is None else (new, old, params_tail)
            conn.execute(q, args)
    conn.commit()
    conn.close()


def test_stored_rel_paths_keep_on_disk_bytes(tmp_path):
    """The index must NOT normalize what it stores — the bytes stay exactly as
    the walk read them (here NFD, as a Mac would present)."""
    vol = tmp_path / "vol"
    (vol / NFD(WORD)).mkdir(parents=True)
    (vol / NFD(WORD) / NFD("relatório.bin")).write_bytes(b"x" * 20_000)
    db = tmp_path / "a.db"
    assert dx_py("index", str(vol), "--db", str(db), "--label", "v").returncode == 0
    conn = sqlite3.connect(db)
    rels = [r[0] for r in conn.execute("SELECT rel_path FROM entries WHERE rel_path != '.'")]
    conn.close()
    assert any(r == NFD(WORD) for r in rels), rels          # kept decomposed
    assert NFC(WORD) not in rels                            # not silently recomposed


def test_refresh_reuses_hashes_across_nfd_nfc(tmp_path):
    """A db indexed with NFD paths, refreshed against NFC on-disk names,
    reuses the cached hashes instead of re-reading every accented file."""
    vol = tmp_path / "vol"
    (vol / NFC(WORD)).mkdir(parents=True)
    (vol / NFC(WORD) / "dados.bin").write_bytes(b"y" * 30_000)
    db = tmp_path / "b.db"
    assert dx_py("index", str(vol), "--db", str(db), "--label", "v").returncode == 0
    _flip_stored_to_nfd(db)                                  # pretend it was a Mac
    r = dx_py("refresh", str(db))
    assert r.returncode == 0, r.stderr
    assert "reused" in r.stderr
    # 0 reused would print "reused 0/..."; we want the accented file carried over
    assert "reused 0/" not in r.stderr, r.stderr


def test_resolve_root_recognizes_accented_volume(tmp_path):
    """resolve_root finds a volume mounted elsewhere even when the stored
    (NFD) top-level names differ in bytes from the on-disk (NFC) ones."""
    vol = tmp_path / "vol"
    (vol / NFC(WORD)).mkdir(parents=True)
    (vol / NFC(WORD) / "big.bin").write_bytes(b"z" * 40_000)
    db = tmp_path / "c.db"
    assert dx_py("index", str(vol), "--db", str(db), "--label", "v").returncode == 0
    _flip_stored_to_nfd(db)
    conn = dx.open_db(db)
    # candidates overrides the scanned mount points; the on-disk vol is NFC
    resolved = dx.resolve_root(conn, str(tmp_path / "gone"), candidates=[vol])
    conn.close()
    assert resolved == vol


def test_diff_reconciles_nfd_nfc_same_file(tmp_path):
    """Two snapshots of the same file, one stored NFD (Mac) and one NFC
    (Windows), must diff clean instead of one delete + one add."""
    vol = tmp_path / "vol"
    (vol / NFC(WORD)).mkdir(parents=True)
    (vol / NFC(WORD) / "paper.bin").write_bytes(b"w" * 5_000)
    db = tmp_path / "d.db"
    assert dx_py("index", str(vol), "--db", str(db), "--label", "v").returncode == 0
    assert dx_py("snapshot", "take", str(db)).returncode == 0
    # flip only the OLDER snapshot to NFD → cross-OS skew between the two
    conn = sqlite3.connect(db)
    s_old = conn.execute("SELECT MIN(snapshot_id) FROM entries").fetchone()[0]
    conn.close()
    _flip_stored_to_nfd(db, snapshot_id=s_old)
    res = dx.diff_snapshots(db)
    assert res["added_count"] == 0, res
    assert res["removed_count"] == 0, res
