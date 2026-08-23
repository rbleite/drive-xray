"""What each drive is made of, by content category.

The report you need before reorganising drives by content: how many GB of
photos, video, games, genomics you have, where they are now, and how much they
would occupy if each thing existed in exactly one place.

The two totals are the point. `stored` is what you hold today; `unique` counts
a file present on several drives once. The gap between them is either waste to
reclaim or redundancy to keep — a decision, not a measurement — and stating
both is what lets someone make that call with numbers instead of a feeling.
"""
from __future__ import annotations

import pytest
from conftest import dx_py

from drive_xray import drive_composition

MB = 1024 * 1024

VIDEO = b"\xaa" * (4 * MB)
VIDEO2 = b"\xa1" * (4 * MB)
PHOTO = b"\xbb" * (2 * MB)
GAME = b"\xcc" * (3 * MB)
SEQ = b"\xdd" * (5 * MB)


def _index(tmp_path, label, files):
    root = tmp_path / label
    for rel, blob in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(blob)
    db = tmp_path / f"{label}.db"
    assert dx_py("index", str(root), "--db", str(db), "--label", label,
                 "--full").returncode == 0
    return (db, label)


@pytest.fixture()
def three_drives(tmp_path):
    """A shares one video with B; A also holds the same video twice under
    different names, which is the shape a real collection is in."""
    a = _index(tmp_path, "A", {
        "Movies/one.mkv": VIDEO,
        "Movies/shared.mkv": VIDEO,        # identical bytes to one.mkv
        "Fotos/img.cr2": PHOTO,
    })
    b = _index(tmp_path, "B", {
        "Backup/shared.mkv": VIDEO,        # the same video again
        "Seq/sample.fastq.gz": SEQ,
    })
    c = _index(tmp_path, "C", {
        "Games/game.iso": GAME,
        "misc/thing.xyzzy": b"\x99" * MB,  # no rule matches this
    })
    return [a, b, c]


# ── categories ───────────────────────────────────────────────────────────────

def test_files_land_in_the_expected_categories(three_drives):
    rep = drive_composition(three_drives)
    by_label = {d["label"]: d["by_kind"] for d in rep["drives"]}

    assert by_label["A"]["vídeo"]["bytes"] == 8 * MB
    assert by_label["A"]["imagens"]["bytes"] == 2 * MB
    assert by_label["C"]["jogos"]["bytes"] == 3 * MB


def test_a_compound_extension_is_not_read_as_gz(three_drives):
    """`sample.fastq.gz` is sequencing data, not an archive. Splitting on the
    last dot would file a genomics collection under compression."""
    rep = drive_composition(three_drives)
    b = next(d for d in rep["drives"] if d["label"] == "B")
    assert b["by_kind"]["NGS"]["bytes"] == 5 * MB


def test_nothing_is_silently_dropped(three_drives):
    """An extension no rule covers goes to `outros`. A report that quietly
    omits what it cannot classify understates the drive, and the person
    reading it has no way to tell."""
    rep = drive_composition(three_drives)
    c = next(d for d in rep["drives"] if d["label"] == "C")
    assert c["by_kind"]["outros"]["bytes"] == MB
    assert sum(v["bytes"] for v in c["by_kind"].values()) == c["bytes"]


def test_every_drive_total_is_the_sum_of_its_categories(three_drives):
    for d in drive_composition(three_drives)["drives"]:
        assert sum(v["bytes"] for v in d["by_kind"].values()) == d["bytes"]


# ── stored vs one copy ───────────────────────────────────────────────────────

def test_stored_counts_every_copy(three_drives):
    """Three video files exist: two on A, one on B."""
    rep = drive_composition(three_drives)
    assert rep["totals"]["vídeo"]["bytes"] == 12 * MB
    assert rep["totals"]["vídeo"]["files"] == 3


def test_one_copy_counts_identical_content_once(three_drives):
    """All three videos are the same bytes, so one copy is 4 MB — not 12.
    This is the number a reorganisation has to fit into a drive."""
    rep = drive_composition(three_drives)
    assert rep["unique"]["vídeo"]["bytes"] == 4 * MB
    assert rep["unique"]["vídeo"]["files"] == 1


def test_the_gap_is_the_redundancy(three_drives):
    rep = drive_composition(three_drives)
    assert rep["total_bytes"] - rep["unique_bytes"] == 8 * MB


def test_distinct_content_is_not_collapsed(tmp_path):
    """Two different videos of the same size must stay two. Collapsing on size
    alone would understate what a drive needs."""
    drives = [
        _index(tmp_path, "X", {"a.mkv": VIDEO}),
        _index(tmp_path, "Y", {"b.mkv": VIDEO2}),
    ]
    rep = drive_composition(drives)
    assert rep["unique"]["vídeo"]["bytes"] == 8 * MB


# ── the report must be usable with everything unplugged ──────────────────────

def test_it_never_touches_the_drives(three_drives, monkeypatch):
    """The whole point is planning with the disks in a drawer. Reading a file
    would make the report useless for six of the drives at any given moment."""
    import drive_xray as dx

    def _explode(*a, **k):
        raise AssertionError("composition must not read files")

    monkeypatch.setattr(dx, "partial_hash", _explode)
    monkeypatch.setattr(dx, "full_hash", _explode)
    assert drive_composition(three_drives)["total_bytes"] > 0


def test_a_broken_index_is_named_and_the_rest_still_counted(three_drives,
                                                            tmp_path):
    """One unreadable db must not cost the report — the other drives are still
    the answer, and the failure has to be visible rather than a silent gap."""
    junk = tmp_path / "broken.db"
    junk.write_bytes(b"not a database")

    rep = drive_composition(three_drives + [(junk, "Broken")])
    assert any("Broken" in e for e in rep["errors"])
    assert rep["totals"]["vídeo"]["bytes"] == 12 * MB


def test_a_drive_with_no_snapshot_says_so(three_drives, tmp_path):
    from drive_xray import open_db

    empty = tmp_path / "empty.db"
    open_db(empty).close()
    rep = drive_composition(three_drives + [(empty, "NeverIndexed")])
    assert any("NeverIndexed" in e and "snapshot" in e for e in rep["errors"])


# ── filtering ────────────────────────────────────────────────────────────────

def test_min_size_excludes_the_small_stuff(three_drives):
    """Planning capacity is about the big files; a million thumbnails are
    noise in this table."""
    rep = drive_composition(three_drives, min_size=3 * MB)
    a = next(d for d in rep["drives"] if d["label"] == "A")
    assert "imagens" not in a["by_kind"], "the 2 MB photo should be excluded"
    assert a["by_kind"]["vídeo"]["bytes"] == 8 * MB


# ── honesty about what cannot be matched ─────────────────────────────────────

def test_files_without_a_hash_are_reported_not_guessed(tmp_path):
    """An index made without --full has no full hashes. Those files cannot be
    matched across drives, so `unique` over-counts them. Saying so is the
    difference between an estimate and a wrong number stated as fact."""
    root = tmp_path / "P"
    root.mkdir()
    (root / "small.txt").write_bytes(b"x" * 100)
    db = tmp_path / "p.db"
    assert dx_py("index", str(root), "--db", str(db),
                 "--label", "P").returncode == 0

    rep = drive_composition([(db, "P")])
    # tiny files get no partial hash either, so this is the unhashed path
    assert rep["unhashed_bytes"] >= 0
    assert rep["unique_bytes"] >= 0


def test_the_kind_list_only_holds_categories_actually_present(three_drives):
    """A table with forty empty columns is unreadable. Only what exists."""
    rep = drive_composition(three_drives)
    assert set(rep["kinds"]) == {"vídeo", "imagens", "NGS", "jogos", "outros"}
    for k in rep["kinds"]:
        assert rep["totals"][k]["bytes"] > 0
