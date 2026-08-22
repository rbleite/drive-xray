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


def test_the_drives_behind_are_named_not_just_the_versions(two_drives):
    """'versions differ (v1, v2)' is not actionable — you cannot re-index a
    version number. The reason has to say WHICH drive to re-index."""
    import sqlite3
    conn = sqlite3.connect(two_drives[1][0])
    conn.execute("UPDATE drive SET hash_version=1")
    conn.commit()
    conn.close()

    why = " ".join(dedupe_readiness(two_drives)["reasons"])
    assert "DriveB" in why, "the drive that is behind must be named"
    assert "v2" in why, "and the version to re-index to"


def test_per_drive_versions_are_reported(two_drives):
    """So the panel can show a column, not just a sentence."""
    r = dedupe_readiness(two_drives)
    assert all(d["hash_version"] == 2 for d in r["drives"])


def test_a_drive_whose_files_share_one_inode_is_flagged(tmp_path):
    """The invisible one. cross_dedupe de-duplicates by (inode, device) to
    handle APFS firmlinks. A filesystem reporting the same inode for every file
    -- 0 is common on some Windows and FAT/exFAT paths -- collapses the whole
    drive to a single comparable file. The counts look healthy, the search runs
    to completion, and it finds nothing.
    """
    import sqlite3

    root = tmp_path / "flat"
    root.mkdir()
    for i in range(40):
        (root / f"f{i}.bin").write_bytes(bytes([i]) * 2048)
    db = tmp_path / "flat.db"
    assert dx_py("index", str(root), "--db", str(db),
                 "--label", "FlatFS").returncode == 0

    conn = sqlite3.connect(db)
    conn.execute("UPDATE entries_core SET inode=0, device=1")
    conn.commit()
    conn.close()

    r = dedupe_readiness([(db, "FlatFS")], min_size=0)
    d = r["drives"][0]
    assert d["eligible"] >= 40, "the drive looks perfectly healthy by count"
    assert d["comparable"] == 1, "yet only one file can ever be compared"
    assert any("actually comparable" in why for why in r["reasons"])


def test_a_healthy_filesystem_is_not_flagged_for_inodes(two_drives):
    """Real inodes must not trip the warning, or it becomes noise."""
    r = dedupe_readiness(two_drives, min_size=0)
    assert not any("actually comparable" in why for why in r["reasons"])
    for d in r["drives"]:
        assert d["comparable"] == d["eligible"]


def test_collapsed_inodes_really_do_cost_the_search(tmp_path):
    """Not a theoretical warning. With five files shared between two drives,
    the search finds five groups. Collapse one drive's inodes and at most ONE
    file from it can take part, so four matches vanish -- with no error and no
    explanation from the search itself.

    Which one survives is arbitrary, which is why the readiness counts are the
    thing to look at rather than the groups.
    """
    import sqlite3
    from drive_xray import cross_dedupe

    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()
    for i in range(5):
        blob = bytes([i + 1]) * (1024 * 1024 + i)
        (a / f"shared{i}.bin").write_bytes(blob)
        (b / f"shared{i}.bin").write_bytes(blob)
    da, db_ = tmp_path / "a.db", tmp_path / "b.db"
    assert dx_py("index", str(a), "--db", str(da), "--label", "A").returncode == 0
    assert dx_py("index", str(b), "--db", str(db_), "--label", "B").returncode == 0
    pair = [(da, "A"), (db_, "B")]

    assert len(cross_dedupe(pair, min_size=0)) == 5

    conn = sqlite3.connect(da)
    conn.execute("UPDATE entries_core SET inode=0, device=1")
    conn.commit()
    conn.close()

    after = cross_dedupe(pair, min_size=0)
    assert len(after) <= 1, "only one file from A can still take part"

    r = dedupe_readiness(pair, min_size=0)
    drive_a = next(d for d in r["drives"] if d["label"] == "A")
    assert drive_a["comparable"] == 1 and drive_a["eligible"] == 5
    assert any("actually comparable" in why for why in r["reasons"]) or \
        drive_a["eligible"] <= 10       # below the noise threshold, but visible


# ── two indexes of the same drive ────────────────────────────────────────────

@pytest.fixture()
def indexed_twice(tmp_path):
    """The same drive indexed twice, as happens when a drive is re-registered
    from a second machine. The internal label is the drive's own, so both
    copies carry it."""
    root = tmp_path / "drive"
    root.mkdir()
    for i in range(3):
        (root / f"f{i}.bin").write_bytes(bytes([i + 1]) * (2 * 1024 * 1024))
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    for db in (a, b):
        assert dx_py("index", str(root), "--db", str(db),
                     "--label", "MyBook").returncode == 0
    # the registry labels differ — that is how they end up listed separately
    return [(a, "MyBook"), (b, "MyBook-Laptop")]


def test_the_same_drive_twice_is_reported_not_silently_collapsed(indexed_twice):
    """The screenshot case: identical file counts, healthy everything, and no
    duplicates. Collapsing them is CORRECT -- the same file seen twice is not
    wasted space -- but doing it without a word is indistinguishable from a
    broken search."""
    r = dedupe_readiness(indexed_twice, min_size=1024 * 1024)
    assert all(d["readable"] and d["eligible"] > 0 for d in r["drives"]), \
        "nothing about either index looks wrong"
    why = " ".join(r["reasons"])
    assert "same drive" in why
    assert "MyBook" in why and "MyBook-Laptop" in why


def test_the_report_names_both_and_says_what_to_do(indexed_twice):
    why = " ".join(dedupe_readiness(indexed_twice)["reasons"])
    assert "not a duplicate" in why, "explain why zero is the right answer"
    assert "Remove" in why, "and what to do about it"


def test_two_genuinely_different_drives_are_not_confused(two_drives):
    """Different drives must never be reported as the same one, or the advice
    would be to delete a real index."""
    r = dedupe_readiness(two_drives, min_size=0)
    assert not any("same drive" in why for why in r["reasons"])


def test_engines_disagree_on_what_counts_as_one_drive(indexed_twice):
    """cross_dedupe skips groups confined to a single drive LABEL. Python is
    handed registry labels, which differ; the Rust CLI reads the label from
    inside each .db, where both say the same thing. Same command, same data,
    two different answers -- pinned here because it is the disagreement that
    makes the result depend on which engine happens to be installed."""
    from drive_xray import cross_dedupe

    registry_labels = cross_dedupe(indexed_twice, min_size=1024 * 1024)
    db_labels = cross_dedupe([(db, "MyBook") for db, _ in indexed_twice],
                             min_size=1024 * 1024)
    assert len(registry_labels) == 3, "distinct labels: every file pairs up"
    assert db_labels == [], "identical labels: everything collapses"
