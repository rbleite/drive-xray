"""Tests for local staging of cloud-synced .db files.

When the .db lives in a OneDrive/GDrive/Dropbox folder, the app writes to a
local staging copy during index/refresh and moves it back at the end — one
upload instead of continuous re-uploads that starve the operation of I/O.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from conftest import dx_py

import drive_xray as dx
from drive_xray import (
    STAGING_DIR, finalize_staged, open_db, open_db_readonly, stage_for_write,
    staged_db,
)


@pytest.fixture(autouse=True)
def _clean_staging():
    yield
    if STAGING_DIR.exists():
        for f in STAGING_DIR.iterdir():
            f.unlink()


def _mkdb(path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("INSERT INTO t VALUES (?)", (marker,))
    conn.commit()
    conn.close()


def _marker(path) -> str:
    conn = sqlite3.connect(path)
    v = conn.execute("SELECT v FROM t").fetchone()[0]
    conn.close()
    return v


def test_non_cloud_path_writes_in_place(tmp_path):
    db = tmp_path / "plain" / "x.db"
    _mkdb(db, "m")
    target, staged = stage_for_write(db)
    assert target == db and staged is False


def test_cloud_path_staged_and_moved_back(tmp_path):
    db = tmp_path / "OneDrive" / "x.db"
    _mkdb(db, "original")
    target, staged = stage_for_write(db)
    assert staged is True
    assert target == STAGING_DIR / "x.db"
    assert _marker(target) == "original"          # copy carries the data

    # the "indexer" writes into the staged copy…
    conn = sqlite3.connect(target)
    conn.execute("UPDATE t SET v='indexed'")
    conn.commit()
    conn.close()

    # …and finalize moves it back over the cloud copy
    msg = finalize_staged(target, db)
    assert msg.startswith("moved to")
    assert not target.exists()
    assert _marker(db) == "indexed"


def test_interrupted_run_resumes_from_staged(tmp_path):
    db = tmp_path / "Dropbox" / "x.db"
    _mkdb(db, "old-cloud")
    staged = STAGING_DIR / "x.db"
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    _mkdb(staged, "partial-progress")
    # staged is newer than the cloud copy → reuse it, don't overwrite
    time.sleep(0.05)
    import os
    os.utime(staged)
    target, is_staged = stage_for_write(db)
    assert is_staged and target == staged
    assert _marker(target) == "partial-progress"


def test_fresh_index_starts_clean(tmp_path):
    db = tmp_path / "OneDrive" / "new.db"   # cloud db does NOT exist yet
    staged = STAGING_DIR / "new.db"
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    _mkdb(staged, "stale")
    # hmm — staged exists and db doesn't: that is the interrupted-run shape,
    # so it must be REUSED (a fresh index over it wipes it anyway).
    target, is_staged = stage_for_write(db)
    assert is_staged and target == staged


def test_env_override_disables_staging(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVE_XRAY_NO_STAGING", "1")
    db = tmp_path / "OneDrive" / "x.db"
    _mkdb(db, "m")
    target, staged = stage_for_write(db)
    assert target == db and staged is False


# ═══════════════════════════════════════════════════════════════════════════
# Everything below covers what the four call sites above did NOT.
#
# Staging existed only around the four subprocesses the Streamlit app spawns.
# The whole CLI, the duplicate-confirmation step in both engines, and prune
# all wrote straight into the synced folder.
#
# Worse, so did *reads*: open_db() runs every migration and then
# executescript(SCHEMA), so peeking at a drive's label to build a picker
# opened the file for writing. `dx import-folder` on a synced folder did it to
# every db at once, and opening the cross-dedupe panel did it to every
# registered drive every time.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def cloud(tmp_path, monkeypatch):
    """A folder the cloud-dir detector recognises, plus a private staging dir
    so the test never touches the real ~/.drive-xray-staging."""
    root = tmp_path / "OneDrive" / "dbs"
    root.mkdir(parents=True)
    staging = tmp_path / "staging"
    monkeypatch.setattr(dx, "STAGING_DIR", staging)
    monkeypatch.delenv("DRIVE_XRAY_NO_STAGING", raising=False)
    return root, staging


# ── the context manager ──────────────────────────────────────────────────────

def test_outside_a_cloud_folder_nothing_happens(tmp_path):
    db = tmp_path / "plain.db"
    with staged_db(db) as target:
        assert target == db
    assert not (tmp_path / "staging").exists()


def test_inside_a_cloud_folder_the_write_is_redirected(cloud):
    root, staging = cloud
    db = root / "d.db"
    with staged_db(db) as target:
        assert target != db
        assert staging in target.parents
        target.write_bytes(b"finished")
        assert not db.exists(), "nothing must appear in the cloud folder yet"
    assert db.read_bytes() == b"finished", "and everything must appear at the end"


def test_an_exception_leaves_the_cloud_copy_untouched(cloud):
    """The reason there is no try/finally. A half-written database must never
    reach the folder other machines sync from."""
    root, staging = cloud
    db = root / "d.db"
    db.write_bytes(b"the good one")

    with pytest.raises(RuntimeError):
        with staged_db(db) as target:
            target.write_bytes(b"half written")
            raise RuntimeError("interrupted")

    assert db.read_bytes() == b"the good one"


def test_an_interrupted_run_leaves_something_to_resume_from(cloud):
    """Staging is not a scratch dir. An index killed after six hours must be
    able to carry on, which is also why it cannot live in /tmp — macOS purges
    that after three days."""
    root, staging = cloud
    db = root / "d.db"
    with pytest.raises(RuntimeError):
        with staged_db(db) as target:
            target.write_bytes(b"partial work")
            raise RuntimeError("interrupted")

    leftover = staging / "d.db"
    assert leftover.exists() and leftover.read_bytes() == b"partial work"
    # and the next attempt picks it up rather than starting over
    again, staged = stage_for_write(db)
    assert staged and again.read_bytes() == b"partial work"


def test_nesting_is_a_no_op(cloud):
    """The app stages before spawning a subprocess, and the subprocess stages
    internally too. The inner one must not make a second copy or move the file
    out from under the outer one."""
    root, staging = cloud
    db = root / "d.db"
    with staged_db(db) as outer:
        with staged_db(outer) as inner:
            assert inner == outer, "the inner stage must not redirect again"
            inner.write_bytes(b"done")
        assert outer.exists(), "the inner exit must not move the file away"
    assert db.read_bytes() == b"done"


def test_the_opt_out_still_works(cloud, monkeypatch):
    root, _staging = cloud
    monkeypatch.setenv("DRIVE_XRAY_NO_STAGING", "1")
    db = root / "d.db"
    with staged_db(db) as target:
        assert target == db


# ── reads must not write ─────────────────────────────────────────────────────

def test_open_db_is_a_write_even_when_the_caller_only_reads(tmp_path):
    """Pins the behaviour that made this necessary. open_db() migrates and
    then runs executescript(SCHEMA), so it modifies the file — handing it a
    path that does not exist produces a database rather than an error.

    Asserted this way on purpose: a permissions-based test would pass
    vacuously for root, which is how the CI container runs.
    """
    made = tmp_path / "conjured.db"
    assert not made.exists()
    open_db(made).close()
    assert made.exists(), "open_db did not write — the premise here is wrong"


def test_the_readonly_open_cannot_write(tmp_path):
    root = tmp_path / "d"
    root.mkdir()
    (root / "f.bin").write_bytes(b"x" * 4096)
    db = tmp_path / "d.db"
    assert dx_py("index", str(root), "--db", str(db), "--label", "L").returncode == 0

    conn = open_db_readonly(db)
    assert conn.execute("SELECT label FROM drive LIMIT 1").fetchone()[0] == "L"
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("CREATE TABLE nope(x)")
    conn.close()


def test_the_readonly_open_refuses_to_create_a_file(tmp_path):
    """mode=ro must not conjure an empty db — a typo in a path should be an
    error, not a new file in someone's synced folder."""
    missing = tmp_path / "not-there.db"
    with pytest.raises(sqlite3.OperationalError):
        open_db_readonly(missing)
    assert not missing.exists()


def test_importing_a_folder_does_not_rewrite_the_databases(tmp_path, monkeypatch):
    """`dx import-folder ~/OneDrive/dbs` opened every .db with open_db purely
    to read its label, migrating and rewriting all of them — one upload each,
    for a read."""
    folder = tmp_path / "dbs"
    folder.mkdir()
    for name in ("a", "b"):
        src = tmp_path / name
        src.mkdir()
        (src / "f.bin").write_bytes(b"x" * 4096)
        assert dx_py("index", str(src), "--db", str(folder / f"{name}.db"),
                     "--label", name).returncode == 0

    # Asserted by forbidding the call, not by comparing mtimes: open_db on a
    # db already at the current schema changes no bytes, because every
    # migration is a no-op and the SCHEMA script is all IF NOT EXISTS. An
    # mtime comparison passes whether or not the fix is present -- it did.
    # What is actually wrong is opening it read-write at all, so that is what
    # this forbids.
    def _explode(*a, **k):
        raise AssertionError("import opened a database for writing")

    monkeypatch.setattr(dx, "open_db", _explode)
    got = dx.import_folder(folder)

    assert {r["label"] for r in got} == {"a", "b"}, "it still has to work"


# ── every writer is covered ──────────────────────────────────────────────────

WRITERS = ["index_drive", "dedupe", "refresh_drive", "snapshot_drive",
           "compact_db"]


@pytest.mark.parametrize("name", WRITERS)
def test_each_writer_stages(name, monkeypatch):
    """Calls the public function and asserts it consulted the staging layer.
    A new writer added without staging is the regression this catches — the
    original hole was not a wrong implementation, it was four call sites
    someone remembered and the rest they did not."""
    seen = []

    def spy(db):
        seen.append(Path(db))
        return Path(db), False

    monkeypatch.setattr(dx, "stage_for_write", spy)
    # make the real work fail immediately; we only care that staging ran first
    monkeypatch.setattr(dx, f"_{name}", lambda *a, **k: None)

    fn = getattr(dx, name)
    if name == "index_drive":
        fn(Path("/nope"), Path("/nope/x.db"), "L", False)
    elif name == "dedupe":
        fn(Path("/nope/x.db"), 1, "quick")
    else:
        fn(Path("/nope/x.db"))

    assert seen, f"{name} did not stage its database"


def test_opening_the_dedupe_panel_does_not_rewrite_every_drive(tmp_path,
                                                               monkeypatch):
    """dedupe_readiness runs for every registered drive each time the panel is
    opened. With open_db that was a write per drive, so simply looking at the
    cross-drive duplicates page rewrote the whole synced folder."""
    dbs = []
    for name in ("a", "b"):
        src = tmp_path / name
        src.mkdir()
        (src / "f.bin").write_bytes(bytes([len(name)]) * (2 * 1024 * 1024))
        db = tmp_path / f"{name}.db"
        assert dx_py("index", str(src), "--db", str(db),
                     "--label", name).returncode == 0
        dbs.append((db, name))

    def _explode(*a, **k):
        raise AssertionError("the readiness check opened a db for writing")

    monkeypatch.setattr(dx, "open_db", _explode)
    r = dx.dedupe_readiness(dbs)
    assert r["usable"] == 2, "and it still has to produce an answer"


def test_a_database_it_cannot_read_read_only_still_gets_migrated(tmp_path,
                                                                 monkeypatch):
    """The fallback. An older db has no entries_core, so read-only cannot
    answer; migrating is the only way. Dropping the fallback would report a
    perfectly good drive as broken."""
    src = tmp_path / "d"
    src.mkdir()
    (src / "f.bin").write_bytes(b"x" * (2 * 1024 * 1024))
    db = tmp_path / "d.db"
    assert dx_py("index", str(src), "--db", str(db), "--label", "L").returncode == 0

    calls = []
    real = dx.open_db_readonly

    def _too_old(p):
        calls.append(p)
        raise sqlite3.OperationalError("no such table: entries_core")

    monkeypatch.setattr(dx, "open_db_readonly", _too_old)
    r = dx.dedupe_readiness([(db, "L")])
    assert calls, "it must try read-only first"
    assert r["drives"][0]["readable"], "and fall back rather than give up"
