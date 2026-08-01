"""The cleanup script must be runnable on the machine that generated it.

A plan's paths are resolved for the current machine, so a .sh full of `E:\\...`
paths is as useless as a .ps1 full of `/Volumes/...` ones. The renderer picks
the dialect from the platform, and both dialects come out of one walk over the
plan so they cannot drift apart.

The PowerShell side is where the sharp edges are, and each has a test below:
quoting (a mis-escaped name could act on the wrong file), `-LiteralPath`
(`[ ]` is a wildcard class to Remove-Item, so `IMG[1].jpg` would silently not
match), and the BOM (Windows PowerShell 5.1 mangles accented names without it).
"""
from __future__ import annotations

import os
import re
import shutil

import pytest

import drive_xray as dx
from conftest import dx_py

# names chosen to break naive quoting: a wildcard class, an apostrophe, a
# PowerShell variable sigil, a backtick (its escape char), and an accent
NASTY = "IMG[1] o'brien $x `tick ção.bin"


@pytest.fixture()
def plan(tmp_path):
    drv = tmp_path / "drive"
    (drv / "sub").mkdir(parents=True)
    (drv / "keep.bin").write_bytes(b"K" * 1_200_000)
    # the copy that gets ACTED ON carries the nasty name — 'shortest' keeps
    # the top-level path, so this is the one that lands in a quoted line
    shutil.copy(drv / "keep.bin", drv / "sub" / NASTY)
    db = tmp_path / "f.db"
    assert dx_py("index", str(drv), "--db", str(db), "--label", "F").returncode == 0
    assert dx_py("dedupe", str(db), "--min-size", "1000000").returncode == 0
    return dx.build_cleanup_plan(db, 1_000_000, "shortest", "quarantine")


def _ps_unquote(literal: str) -> str:
    """Decode a PowerShell single-quoted literal the way PowerShell does:
    nothing is expanded and '' is the only escape. Used to prove round-trip."""
    assert literal.startswith("'") and literal.endswith("'"), literal
    return literal[1:-1].replace("''", "'")


# ---------- dialect selection ----------

def test_default_flavor_follows_the_platform():
    assert dx.default_script_flavor() == ("powershell" if os.name == "nt" else "bash")


def test_suffix_matches_the_flavor():
    assert dx.cleanup_script_suffix("powershell") == ".ps1"
    assert dx.cleanup_script_suffix("bash") == ".sh"


def test_unknown_flavor_is_refused(plan):
    with pytest.raises(ValueError):
        dx.render_cleanup_script(plan, flavor="fish")


# ---------- both dialects describe the same plan ----------

def test_both_dialects_cover_every_action(plan):
    sh = dx.render_cleanup_script(plan, flavor="bash")
    ps = dx.render_cleanup_script(plan, flavor="powershell")
    assert plan["n_actions"] > 0
    assert len([l for l in sh.splitlines() if l.startswith("mv ")]) == plan["n_actions"]
    assert len([l for l in ps.splitlines() if l.startswith("Move-Item")]) == plan["n_actions"]


# ---------- PowerShell sharp edges ----------

def test_powershell_always_uses_literalpath(plan):
    """-Path would treat [ ] as a wildcard class and silently match nothing."""
    ps = dx.render_cleanup_script(plan, flavor="powershell")
    acting = [l for l in ps.splitlines()
              if l.startswith(("Move-Item", "Remove-Item"))]
    assert acting
    for line in acting:
        assert "-LiteralPath" in line, line
        assert not re.search(r"-Path\s+'", line), line


def test_nasty_name_round_trips_through_powershell_quoting(plan):
    ps = dx.render_cleanup_script(plan, flavor="powershell")
    line = next(l for l in ps.splitlines() if l.startswith("Move-Item"))
    literal = re.search(r"-LiteralPath ('(?:[^']|'')*')", line).group(1)
    assert _ps_unquote(literal).endswith(NASTY), _ps_unquote(literal)


def test_apostrophe_is_doubled_not_backslashed(plan):
    """Backslash is not an escape inside PowerShell '...' — using one would
    end the string early and change which file the line acts on."""
    ps = dx.render_cleanup_script(plan, flavor="powershell")
    line = next(l for l in ps.splitlines() if l.startswith("Move-Item"))
    assert "o''brien" in line
    assert "\\'" not in line


def test_starts_with_a_bom_so_ps51_reads_utf8(plan):
    ps = dx.render_cleanup_script(plan, flavor="powershell")
    assert ps.startswith("\ufeff")
    assert ps.encode("utf-8").startswith(b"\xef\xbb\xbf")
    assert "ção" in ps, "accented file names must survive"


def test_powershell_decoration_is_ascii(plan):
    """Only file names should be non-ASCII: the furniture ('↳', '─') buys no
    value and would be the first thing to break on a mis-decoded console."""
    ps = dx.render_cleanup_script(plan, flavor="powershell")
    for ch in "↳─·":
        assert ch not in ps


def test_bash_keeps_its_shebang_and_powershell_has_none(plan):
    assert dx.render_cleanup_script(plan, flavor="bash").startswith("#!/usr/bin/env bash")
    assert "#!" not in dx.render_cleanup_script(plan, flavor="powershell")
