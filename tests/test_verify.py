"""Bit-rot verification: telling corruption apart from an ordinary edit.

`verify_integrity` re-reads every file and compares its hash to the stored one.
A differing hash alone is not evidence of rot, though: a size-preserving edit —
a database, a disk image, a RAW whose metadata was rewritten in place — differs
too. What separates them is whether anything claims to have WRITTEN the file,
which is the mtime.

The tests that matter most here are the ones about not crying wolf. A pass that
flags ordinary saves as corruption exits non-zero on a healthy drive, and
teaches you to ignore the one finding that is real.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from conftest import dx_py

from drive_xray import stale_drives, verify_integrity


@pytest.fixture()
def indexed(tmp_path, monkeypatch):
    """A small drive, indexed, with resolve_root pinned to it."""
    root = tmp_path / "drive"
    (root / "docs").mkdir(parents=True)
    (root / "big.bin").write_bytes(b"A" * (300 * 1024))     # > 3 * 64KB chunks
    (root / "small.txt").write_bytes(b"hello")
    (root / "docs" / "notes.txt").write_bytes(b"notes here")
    db = tmp_path / "drive.db"
    r = dx_py("index", str(root), "--db", str(db), "--label", "TestDrive")
    assert r.returncode == 0, r.stderr

    import drive_xray as dx
    monkeypatch.setattr(dx, "resolve_root", lambda conn, stored, **kw: root)
    return root, db


def _rot(path: Path, data: bytes) -> None:
    """Change the bytes and put the timestamps back — what silent corruption
    looks like from the outside. Size is preserved."""
    st = path.stat()
    assert len(data) == st.st_size, "rot does not change the size"
    path.write_bytes(data)
    os.utime(path, (st.st_atime, st.st_mtime))


# ── the healthy case ─────────────────────────────────────────────────────────

def test_a_clean_drive_reports_nothing(indexed):
    root, db = indexed
    res = verify_integrity(db)
    assert res["root_mounted"] and res["ok"] == res["total"] == 3
    assert res["corrupted"] == [] and res["edited"] == []


def test_an_unmounted_drive_is_not_an_error(indexed, monkeypatch, tmp_path):
    import drive_xray as dx
    _, db = indexed
    monkeypatch.setattr(dx, "resolve_root",
                        lambda conn, stored, **kw: tmp_path / "nowhere")
    res = verify_integrity(db)
    assert res["root_mounted"] is False and res["total"] == 0


# ── rot, and what is not rot ─────────────────────────────────────────────────

def test_silent_corruption_is_caught(indexed):
    root, db = indexed
    _rot(root / "big.bin", b"B" * (300 * 1024))
    res = verify_integrity(db)
    assert [c["rel_path"] for c in res["corrupted"]] == ["big.bin"]
    assert res["edited"] == []
    assert res["ok"] == 2


def test_a_size_preserving_edit_is_not_called_corruption(indexed):
    """The false positive that matters: a database, a disk image, a RAW with
    its metadata rewritten in place. Same length, different bytes — and a
    mtime that proves something wrote it."""
    root, db = indexed
    p = root / "big.bin"
    p.write_bytes(b"C" * (300 * 1024))
    later = time.time() + 120
    os.utime(p, (later, later))
    res = verify_integrity(db)
    assert res["corrupted"] == [], "an ordinary save must not raise the alarm"
    assert [e["rel_path"] for e in res["edited"]] == ["big.bin"]


def test_a_resized_file_is_neither(indexed):
    root, db = indexed
    (root / "small.txt").write_bytes(b"much longer than before")
    res = verify_integrity(db)
    assert res["size_changed"] == 1
    assert res["corrupted"] == [] and res["edited"] == []


def test_a_deleted_file_is_counted_missing(indexed):
    root, db = indexed
    (root / "small.txt").unlink()
    res = verify_integrity(db)
    assert res["missing"] == 1 and res["corrupted"] == []


def test_rot_on_a_drive_read_across_timezones_is_still_called_rot(indexed):
    """exFAT stores local time, so an untouched drive verified on another
    machine reports mtimes shifted by whole hours. Comparing exactly would
    file real rot under `edited` on exactly the drives most likely to have
    it — old external disks that travel between computers."""
    root, db = indexed
    p = root / "big.bin"
    st = p.stat()
    _rot(p, b"D" * (300 * 1024))
    os.utime(p, (st.st_mtime + 3600, st.st_mtime + 3600))
    res = verify_integrity(db)
    assert [c["rel_path"] for c in res["corrupted"]] == ["big.bin"], \
        "a whole-hour shift is a timezone artefact, not evidence of a write"
    assert res["edited"] == []


def test_a_real_save_is_still_a_save_on_such_a_drive(indexed):
    """The tolerance must not swallow genuine writes: a shift that is not a
    whole-hour multiple is a real modification."""
    root, db = indexed
    p = root / "big.bin"
    st = p.stat()
    p.write_bytes(b"E" * (300 * 1024))
    os.utime(p, (st.st_mtime + 5000, st.st_mtime + 5000))    # 1h23m20s
    res = verify_integrity(db)
    assert [e["rel_path"] for e in res["edited"]] == ["big.bin"]
    assert res["corrupted"] == []


# ── sampling versus reading everything ───────────────────────────────────────

def test_full_hashing_catches_what_sampling_reads_past(tmp_path, monkeypatch):
    """The partial hash reads head, middle and tail. A file large enough for
    those to leave gaps can rot in between and pass — which is why --full
    exists, and why offering only the fast screen would be false comfort."""
    import drive_xray as dx
    root = tmp_path / "drive"
    root.mkdir()
    p = root / "sparse.bin"
    p.write_bytes(b"\x00" * (1024 * 1024))               # 1 MB, chunks are 64KB
    db = tmp_path / "d.db"
    assert dx_py("index", str(root), "--db", str(db), "--full").returncode == 0
    monkeypatch.setattr(dx, "resolve_root", lambda conn, stored, **kw: root)

    data = bytearray(p.read_bytes())
    data[400 * 1024] = 0xFF                              # between head and middle
    _rot(p, bytes(data))

    assert verify_integrity(db, full=False)["corrupted"] == [], \
        "sampling cannot see this"
    assert len(verify_integrity(db, full=True)["corrupted"]) == 1, \
        "reading every byte can"


# ── forgotten drives ─────────────────────────────────────────────────────────

def _entry(label, days_ago, now):
    from datetime import datetime
    return {"db": Path(f"{label}.db"), "label": label, "root": f"/{label}",
            "last_indexed": datetime.fromtimestamp(now - days_ago * 86400).isoformat(),
            "exists": True}


def test_a_recently_indexed_drive_is_not_stale(monkeypatch):
    import drive_xray as dx
    now = time.time()
    monkeypatch.setattr(dx, "registry_list", lambda: [_entry("Recent", 5, now)])
    assert stale_drives(days=180, now=now) == []


def test_forgotten_drives_are_surfaced_worst_first(monkeypatch):
    import drive_xray as dx
    now = time.time()
    monkeypatch.setattr(dx, "registry_list", lambda: [
        _entry("Fresh", 10, now), _entry("Drawer", 420, now),
        _entry("Older", 900, now)])
    stale = stale_drives(days=180, now=now)
    assert [d["label"] for d in stale] == ["Older", "Drawer"]
    assert stale[0]["days"] >= 899


def test_a_drive_with_no_usable_date_is_not_guessed_about(monkeypatch):
    """Silence is better than inventing an age for it."""
    import drive_xray as dx
    monkeypatch.setattr(dx, "registry_list", lambda: [
        {"db": Path("a.db"), "label": "Unknown", "root": "/a",
         "last_indexed": "", "exists": True},
        {"db": Path("b.db"), "label": "Garbled", "root": "/b",
         "last_indexed": "not a date", "exists": True}])
    assert stale_drives(days=1, now=time.time()) == []


def test_a_finding_carries_both_dates(indexed):
    """A finding has to be checkable. Without the dates, a report of rot on a
    file you remember editing is just a mystery."""
    root, db = indexed
    _rot(root / "big.bin", b"F" * (300 * 1024))
    c = verify_integrity(db)["corrupted"][0]
    assert c["db_mtime"] and c["disk_mtime"]
    assert abs(c["db_mtime"] - c["disk_mtime"]) < 2


def test_an_edit_exactly_an_hour_later_is_reported_as_rot(indexed):
    """Pinning a known cost of the exFAT tolerance rather than pretending it
    away: a whole-hour shift reads as 'nothing wrote this'. The bias is
    deliberate -- a false alarm costs one look, a missed corruption costs the
    file -- and the dates on the finding are what let you tell them apart."""
    root, db = indexed
    p = root / "big.bin"
    st = p.stat()
    p.write_bytes(b"G" * (300 * 1024))
    os.utime(p, (st.st_mtime + 3600, st.st_mtime + 3600))
    res = verify_integrity(db)
    assert len(res["corrupted"]) == 1
    c = res["corrupted"][0]
    assert round(c["disk_mtime"] - c["db_mtime"]) == 3600, \
        "the dates must expose the whole-hour shift that caused this"
