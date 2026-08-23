"""APFS clones: duplicates that free nothing when you delete them.

`cp -c` and Finder's Duplicate produce a file with its own inode, its own
path, and the original's data blocks. Nothing in stat() tells them apart --
measured on a real APFS volume, a 10 MB original, its clone and a genuine copy
all reported size=10485760 blocks=20480, and `du` said 10M to all three.
Deleting the clone moved df's available column by zero bytes.

The cleanup plan counted such a clone as a full duplicate, so it promised
space that deleting it could not return. That is the one number in the app a
user acts on before running a script against their own files.

These tests fix the arithmetic and the reporting. The probe itself is macOS
only, so the integration tests drive it through a stub -- the real fcntl is
covered by a live test that skips elsewhere.
"""
from __future__ import annotations

import sys

import pytest
from conftest import dx_py

import drive_xray as dx
from drive_xray import build_cleanup_plan, physical_offset, reclaimable_bytes


# ── the arithmetic ───────────────────────────────────────────────────────────

def test_independent_copies_free_all_but_one():
    assert reclaimable_bytes(1000, [10, 20, 30]) == 2000


def test_a_cloned_pair_frees_nothing():
    """The headline case. Two paths, one set of blocks: deleting either one
    releases nothing, because the other still references them."""
    assert reclaimable_bytes(1000, [10, 10]) == 0


def test_a_clone_alongside_a_real_copy_frees_only_the_copy():
    """orig + clone + copy — the exact trio measured on the volume. The old
    arithmetic said 2000; only 1000 is real."""
    assert reclaimable_bytes(1000, [10, 10, 20]) == 1000
    assert 1000 * (3 - 1) == 2000, "what the previous formula would promise"


def test_one_copy_frees_nothing():
    assert reclaimable_bytes(1000, [10]) == 0


def test_an_unknown_offset_makes_the_whole_answer_unknown():
    """Half-measured is worse than unmeasured: it would be reported as fact.
    None forces the caller to say 'upper bound' instead of naming a figure."""
    assert reclaimable_bytes(1000, [10, None, 30]) is None
    assert reclaimable_bytes(1000, [None]) is None
    assert reclaimable_bytes(1000, []) is None


# ── the probe ────────────────────────────────────────────────────────────────

def test_the_probe_is_silent_where_it_does_not_apply(tmp_path):
    """Linux, Windows, and any filesystem without the fcntl. It must return
    None, never raise — this runs inside the cleanup path."""
    f = tmp_path / "x.bin"
    f.write_bytes(b"x" * 4096)
    assert physical_offset(f) is None or isinstance(physical_offset(f), int)


def test_a_missing_file_does_not_raise():
    assert physical_offset("/nonexistent/nowhere/at/all") is None


def test_a_directory_does_not_raise(tmp_path):
    assert physical_offset(tmp_path) is None


@pytest.mark.skipif(sys.platform != "darwin", reason="APFS clones are macOS")
def test_a_real_clone_shares_a_physical_offset(tmp_path):
    """The live version, against the actual filesystem. Skips off macOS, so
    the stubbed tests below carry the logic and this carries the syscall."""
    import subprocess

    orig, clone, copy = (tmp_path / n for n in ("o.bin", "cl.bin", "cp.bin"))
    orig.write_bytes(b"\xa5" * (4 * 1024 * 1024))
    r = subprocess.run(["cp", "-c", str(orig), str(clone)],
                       capture_output=True)
    if r.returncode != 0:
        pytest.skip("filesystem does not support clonefile")
    subprocess.run(["cp", str(orig), str(copy)], check=True)

    o, cl, cp_ = (physical_offset(p) for p in (orig, clone, copy))
    assert o is not None, "the probe failed on a plain APFS file"
    assert cl == o, "a clone must land on the original's blocks"
    assert cp_ != o, "a real copy must not"


# ── the plan ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def three_copies(tmp_path):
    """One 2 MB payload at three paths, indexed with full hashes."""
    root = tmp_path / "drive"
    root.mkdir()
    blob = b"\xd7" * (2 * 1024 * 1024)
    for name in ("a.bin", "b.bin", "c.bin"):
        (root / name).write_bytes(blob)
    db = tmp_path / "d.db"
    assert dx_py("index", str(root), "--db", str(db), "--label", "D",
                 "--full").returncode == 0
    return db, root


def _stub_offsets(monkeypatch, mapping, default=None):
    """Pretend the filesystem reports these physical offsets, keyed by the
    file's basename."""
    def fake(path):
        return mapping.get(str(path).rsplit("/", 1)[-1], default)
    monkeypatch.setattr(dx, "physical_offset", fake)


def test_clones_are_excluded_from_reclaimable_space(monkeypatch, three_copies):
    """a and b share blocks; c is independent. Keeping one of a/b, the plan
    can free exactly one 2 MB file, not two."""
    db, _root = three_copies
    _stub_offsets(monkeypatch, {"a.bin": 4096, "b.bin": 4096, "c.bin": 9999})

    plan = build_cleanup_plan(db, min_size=1024)
    size = plan["groups"][0]["size"]
    assert plan["n_actions"] == 2, "still two files to remove"
    assert plan["total_freeable"] == size, "but only one file's worth of space"
    assert plan["total_logical"] == size * 2, "what it used to claim"
    assert plan["n_clone_actions"] == 1


def test_the_clone_action_names_what_it_shares_with(monkeypatch, three_copies):
    """A total the user cannot break down is not actionable. The per-file
    list is what they read before running the script."""
    db, _root = three_copies
    _stub_offsets(monkeypatch, {"a.bin": 4096, "b.bin": 4096, "c.bin": 9999})

    actions = build_cleanup_plan(db, min_size=1024)["groups"][0]["actions"]
    cloned = [a for a in actions if a["clone_of"]]
    assert len(cloned) == 1
    assert cloned[0]["frees_bytes"] == 0
    assert cloned[0]["clone_of"] in {"a.bin", "b.bin"}
    assert cloned[0]["clone_of"] != cloned[0]["rel_path"], \
        "a file cannot be a clone of itself"


def test_every_copy_a_clone_frees_nothing_at_all(monkeypatch, three_copies):
    """The whole group shares one set of blocks. Deleting two of three files
    returns zero bytes — worth saying rather than silently promising 4 MB."""
    db, _root = three_copies
    _stub_offsets(monkeypatch, {}, default=4096)

    plan = build_cleanup_plan(db, min_size=1024)
    assert plan["n_actions"] == 2, "the files are still redundant paths"
    assert plan["total_freeable"] == 0
    assert plan["n_clone_actions"] == 2


def test_ordinary_duplicates_are_unaffected(monkeypatch, three_copies):
    """The common case must not change: distinct blocks, full credit. If this
    drifts, every non-APFS user's figures silently shrink."""
    db, _root = three_copies
    _stub_offsets(monkeypatch, {"a.bin": 1, "b.bin": 2, "c.bin": 3})

    plan = build_cleanup_plan(db, min_size=1024)
    assert plan["total_freeable"] == plan["total_logical"]
    assert plan["n_clone_actions"] == 0
    assert all(a["clone_of"] is None for a in plan["groups"][0]["actions"])


def test_an_unprobeable_drive_keeps_the_old_figure_and_says_so(
        monkeypatch, three_copies):
    """Linux, Windows, a drive that is no longer mounted. Reporting 0 there
    would be a fabrication in the opposite direction; the honest answer is the
    logical upper bound, labelled."""
    db, _root = three_copies
    _stub_offsets(monkeypatch, {}, default=None)

    plan = build_cleanup_plan(db, min_size=1024)
    assert plan["total_freeable"] == plan["total_logical"]
    assert plan["n_clone_unknown"] == 1
    assert plan["n_clone_actions"] == 0


def test_the_script_says_a_clone_frees_nothing(monkeypatch, three_copies):
    """The script is read by a human before it deletes their files, and it is
    also the artefact that outlives this session."""
    from drive_xray import render_cleanup_script

    db, _root = three_copies
    _stub_offsets(monkeypatch, {"a.bin": 4096, "b.bin": 4096, "c.bin": 9999})

    text = render_cleanup_script(build_cleanup_plan(db, min_size=1024))
    assert "APFS clone of" in text
    assert "frees 0 bytes" in text


def test_a_clean_drive_gets_no_clone_noise(monkeypatch, three_copies):
    """Nothing about clones should appear on filesystems that have none."""
    from drive_xray import render_cleanup_script

    db, _root = three_copies
    _stub_offsets(monkeypatch, {"a.bin": 1, "b.bin": 2, "c.bin": 3})

    text = render_cleanup_script(build_cleanup_plan(db, min_size=1024))
    assert "APFS clone" not in text
    assert "could not be probed" not in text


def test_the_summary_never_promises_more_than_it_can_free(monkeypatch,
                                                          three_copies):
    """The invariant behind all of this, stated once: whatever the
    filesystem, the headline figure is never above the truth."""
    db, _root = three_copies
    for mapping in ({"a.bin": 4096, "b.bin": 4096, "c.bin": 9999},
                    {"a.bin": 1, "b.bin": 2, "c.bin": 3},
                    {}):
        _stub_offsets(monkeypatch, mapping, default=4096 if mapping else 7)
        plan = build_cleanup_plan(db, min_size=1024)
        assert plan["total_freeable"] <= plan["total_logical"]
        assert plan["total_freeable"] == sum(
            a["frees_bytes"] for g in plan["groups"] for a in g["actions"]), \
            "the total must be the sum of the parts the user can see"
