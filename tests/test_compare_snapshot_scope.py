"""Cross-drive comparison must compare the CURRENT state of each drive.

The UI's pair comparison used to read `entries` without a snapshot_id filter,
so every file was seen once per snapshot: matches inflated by (snapshots in A ×
snapshots in B) and "only in A" by the number of snapshots in A. A drive on the
default retention (~22 snapshots) reported hundreds of times the real figures,
and the UI disagreed with the `dx compare` CLI on the same two drives.
"""
from __future__ import annotations

from collections import defaultdict

from conftest import dx_py

from drive_xray import latest_snapshot_id, open_db

# the query the UI runs, mirroring app.py's pair comparison
SQL = (
    "SELECT size, partial_hash, rel_path, full_hash FROM entries"
    " WHERE snapshot_id=? AND is_dir=0 AND size >= ?"
    "   AND partial_hash IS NOT NULL"
)


def _compare(db_a, db_b, min_size=0):
    """Returns (matches, only_in_a) the way the UI counts them."""
    cb = open_db(db_b)
    index = defaultdict(list)
    for size, partial, rel, fh in cb.execute(SQL, (latest_snapshot_id(cb), min_size)):
        index[(size, partial)].append((rel, fh))
    cb.close()

    ca = open_db(db_a)
    matches = only_a = 0
    for size, partial, rel, fh in ca.execute(SQL, (latest_snapshot_id(ca), min_size)):
        hits = index.get((size, partial))
        if not hits:
            only_a += 1
        else:
            matches += len(hits)
    ca.close()
    return matches, only_a


def _drive(root, shared: bytes, extra: bytes | None = None):
    root.mkdir(parents=True)
    (root / "shared.bin").write_bytes(shared)
    if extra is not None:
        (root / "only_here.bin").write_bytes(extra)


def test_comparison_is_unaffected_by_snapshot_count(tmp_path):
    a_root, b_root = tmp_path / "A", tmp_path / "B"
    shared = b"s" * 200_000
    _drive(a_root, shared, extra=b"x" * 200_000)   # 1 shared + 1 only-in-A
    _drive(b_root, shared)                          # 1 shared

    db_a, db_b = tmp_path / "a.db", tmp_path / "b.db"
    assert dx_py("index", str(a_root), "--db", str(db_a), "--label", "A").returncode == 0
    assert dx_py("index", str(b_root), "--db", str(db_b), "--label", "B").returncode == 0

    baseline = _compare(db_a, db_b)
    assert baseline == (1, 1), baseline

    # Piling up snapshots must not change the answer: the comparison is about
    # the drives' current contents, not their history.
    for _ in range(3):
        assert dx_py("snapshot", "take", str(db_a)).returncode == 0
        assert dx_py("snapshot", "take", str(db_b)).returncode == 0

    assert _compare(db_a, db_b) == baseline, (
        "comparison must not scale with the number of snapshots"
    )
