"""Cleanup v2 — running a cleanup plan from inside the app.

These tests guard the destructive path, so they assert what must NOT happen at
least as hard as what must: one copy per group is always kept, hardlinks are
never acted on, a changed file is skipped rather than deleted, an unmounted
drive aborts before touching anything, and nothing outside the drive root can
be reached.
"""
from __future__ import annotations

import os
import shutil
import sqlite3

import pytest

from conftest import dx_py

from drive_xray import (
    build_cleanup_plan,
    execute_cleanup_plan,
    execute_file_action,
    generate_cleanup_script,
    render_cleanup_script,
)


@pytest.fixture()
def dup_drive(tmp_path):
    """A drive with: one real duplicate pair, and one group that is only
    hardlinks (same inode) and must therefore be left alone."""
    drv = tmp_path / "drive"
    (drv / "sub").mkdir(parents=True)
    (drv / "orig.bin").write_bytes(b"A" * 1_500_000)
    shutil.copy(drv / "orig.bin", drv / "sub" / "copy.bin")
    (drv / "hl.bin").write_bytes(b"B" * 1_200_000)
    os.link(drv / "hl.bin", drv / "sub" / "hl2.bin")

    db = tmp_path / "c.db"
    assert dx_py("index", str(drv), "--db", str(db), "--label", "T").returncode == 0
    assert dx_py("dedupe", str(db), "--min-size", "1000000").returncode == 0
    return drv, db


def test_plan_skips_hardlink_only_groups(dup_drive):
    _, db = dup_drive
    plan = build_cleanup_plan(db, 1_000_000, "shortest", "quarantine")
    assert plan["n_actions"] == 1, "the hardlink-only group frees nothing"
    acted = [a["rel_path"] for g in plan["groups"] for a in g["actions"]]
    assert acted == ["sub/copy.bin"]


def test_script_and_plan_cannot_drift(dup_drive):
    """The script the user downloads is rendered from the plan the app runs."""
    _, db = dup_drive
    for action in ("quarantine", "delete"):
        for strategy in ("shortest", "oldest", "newest", "alphabetical"):
            plan = build_cleanup_plan(db, 1_000_000, strategy, action)
            assert render_cleanup_script(plan).count("\n") > 0
            # the standalone generator must agree with the plan's action count
            script = generate_cleanup_script(db, 1_000_000, strategy=strategy,
                                             action=action)
            n_cmds = sum(1 for l in script.splitlines()
                         if l.startswith(("rm ", "mv ")))
            assert n_cmds == plan["n_actions"]


def test_quarantine_keeps_one_copy_and_spares_hardlinks(dup_drive, tmp_path):
    drv, db = dup_drive
    plan = build_cleanup_plan(db, 1_000_000, "shortest", "quarantine")
    plan["quarantine_dir"] = str(tmp_path / "q")   # keep the test self-contained
    res = execute_cleanup_plan(plan, db_path=str(db))

    assert res["ok"] == 1 and not res["errors"] and not res["aborted"]
    assert res["freed_bytes"] == 1_500_000
    assert (drv / "orig.bin").exists(), "the kept copy must survive"
    assert not (drv / "sub" / "copy.bin").exists(), "the duplicate must move"
    # hardlink pair untouched — deleting either frees nothing
    assert (drv / "hl.bin").exists() and (drv / "sub" / "hl2.bin").exists()
    # moved, not destroyed, under the script's naming scheme
    moved = list((tmp_path / "q").iterdir())
    assert len(moved) == 1 and moved[0].name.startswith("g0")
    assert moved[0].stat().st_size == 1_500_000


def test_delete_removes_only_the_duplicate(dup_drive):
    drv, db = dup_drive
    plan = build_cleanup_plan(db, 1_000_000, "shortest", "delete")
    res = execute_cleanup_plan(plan, db_path=str(db))
    assert res["ok"] == 1
    assert (drv / "orig.bin").exists()
    assert not (drv / "sub" / "copy.bin").exists()
    assert (drv / "hl.bin").exists() and (drv / "sub" / "hl2.bin").exists()


def test_file_changed_since_indexing_is_skipped(dup_drive):
    """A stale index must never cause the wrong file to be destroyed."""
    drv, db = dup_drive
    plan = build_cleanup_plan(db, 1_000_000, "shortest", "delete")
    (drv / "sub" / "copy.bin").write_bytes(b"C" * 99)   # size no longer matches

    res = execute_cleanup_plan(plan, db_path=str(db))
    assert res["ok"] == 0
    assert len(res["skipped"]) == 1
    assert "size_mismatch" in res["skipped"][0]["reason"]
    assert (drv / "sub" / "copy.bin").exists(), "changed file must survive"


def test_missing_file_is_skipped_not_an_error(dup_drive):
    drv, db = dup_drive
    plan = build_cleanup_plan(db, 1_000_000, "shortest", "delete")
    (drv / "sub" / "copy.bin").unlink()
    res = execute_cleanup_plan(plan, db_path=str(db))
    assert res["ok"] == 0 and not res["errors"]
    assert res["skipped"][0]["reason"] == "not_found"


def test_unmounted_drive_aborts_before_touching_anything(dup_drive, tmp_path):
    drv, db = dup_drive
    plan = build_cleanup_plan(db, 1_000_000, "shortest", "delete")
    plan["root_path"] = str(tmp_path / "not-mounted")
    res = execute_cleanup_plan(plan, db_path=str(db))
    assert res["aborted"] and res["ok"] == 0
    assert (drv / "sub" / "copy.bin").exists(), "nothing may be touched"


def test_cannot_act_outside_the_drive_root(dup_drive, tmp_path):
    drv, db = dup_drive
    outsider = tmp_path / "precious.txt"
    outsider.write_text("do not touch\n")
    r = execute_file_action(str(outsider), "delete", root_path=drv, db_path=str(db))
    assert not r["ok"] and "path_outside_root" in r["error"]
    assert outsider.exists()


def test_progress_callback_reports_every_step(dup_drive, tmp_path):
    _, db = dup_drive
    plan = build_cleanup_plan(db, 1_000_000, "shortest", "quarantine")
    plan["quarantine_dir"] = str(tmp_path / "q")
    seen: list[tuple[int, int]] = []
    execute_cleanup_plan(plan, progress_cb=lambda d, t, r: seen.append((d, t)),
                         db_path=str(db))
    assert seen[0][0] == 0 and seen[-1][0] == seen[-1][1] == plan["n_actions"]


def test_execution_is_audited(dup_drive, tmp_path, monkeypatch):
    import drive_xray as dx
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr(dx, "AUDIT_LOG", audit)
    _, db = dup_drive
    plan = dx.build_cleanup_plan(db, 1_000_000, "shortest", "quarantine")
    plan["quarantine_dir"] = str(tmp_path / "q")
    dx.execute_cleanup_plan(plan, db_path=str(db))
    assert audit.exists(), "every action must leave an audit trail"
    assert "sub/copy.bin" in audit.read_text()
