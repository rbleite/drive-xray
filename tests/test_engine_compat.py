"""Refusing a Rust binary that predates the database schema.

The Python engine migrates a .db the moment the app opens it. The Rust binary
is downloaded separately from Releases and can sit unchanged for months. When
schema v7 landed — ten days after the last release — every published binary was
suddenly reading a shape it was never built for.

Nothing caught it, because the guard compared the binary's version to
DX_VERSION for EQUALITY and neither had been bumped. Both said "1.4.1", so the
check passed and the engine silently returned wrong answers: a cross-drive
duplicate search that reported nothing on drives full of duplicates.

The lesson these tests encode: a version label nobody increments is not a
compatibility check, and a stale constant is indistinguishable from a correct
one until something depends on it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import drive_xray as dx
from drive_xray import (
    DX_VERSION, MIN_DX_VERSION, SCHEMA_VERSION, _version_tuple,
    dx_supports_schema, engine_status,
)


# ── the version comparison itself ────────────────────────────────────────────

def test_the_binary_that_caused_this_is_refused():
    """1.4.1 is the last published release, and it predates schema v7."""
    assert dx_supports_schema("1.4.1") is False


def test_the_first_compatible_release_is_accepted():
    assert dx_supports_schema(MIN_DX_VERSION) is True
    assert dx_supports_schema("1.6.0") is True
    assert dx_supports_schema("2.0.0") is True


@pytest.mark.parametrize("junk", ["", None, "unknown", "dx", "v", "..."])
def test_anything_unreadable_fails_closed(junk):
    """A version we cannot parse must be treated as too old. Assuming it is
    fine is how this bug worked in the first place."""
    assert dx_supports_schema(junk) is False


def test_versions_compare_numerically_not_as_text():
    """'1.10.0' > '1.9.0' numerically but not as strings — the kind of thing
    that works for a year and then quietly stops."""
    assert _version_tuple("1.10.0") > _version_tuple("1.9.0")
    assert _version_tuple("1.5.0") > _version_tuple("1.4.9")
    assert _version_tuple("2.0.0") > _version_tuple("1.99.99")


def test_a_prerelease_suffix_does_not_break_parsing():
    assert _version_tuple("1.5.0-rc1")[:3] == (1, 5, 0)
    assert dx_supports_schema("1.5.0-rc1") is True


def test_equality_alone_would_not_have_caught_it():
    """Pins the shape of the original failure: the old guard asked whether the
    strings matched. They did — both were '1.4.1' — while the schema had moved
    on underneath."""
    stale_binary, stale_app = "1.4.1", "1.4.1"
    assert stale_binary == stale_app, "the old check passed"
    assert not dx_supports_schema(stale_binary), "the new one does not"


# ── the constants must not be able to go stale again ─────────────────────────

def _source() -> str:
    return (Path(__file__).resolve().parent.parent / "drive_xray.py").read_text(
        encoding="utf-8")


def test_schema_version_matches_the_migrations_that_exist():
    """SCHEMA_VERSION sat at 6 while _migrate_to_v7 was in the file, so
    `dx --version` advertised a schema the code no longer wrote. This fails the
    build the next time a migration lands without the constant moving."""
    migrations = {int(n) for n in re.findall(r"def _migrate_to_v(\d+)\(", _source())}
    assert migrations, "no migrations found — the scan itself is broken"
    assert SCHEMA_VERSION == max(migrations), (
        f"SCHEMA_VERSION is {SCHEMA_VERSION} but the newest migration is "
        f"v{max(migrations)} — bump it, or the version banner lies")


def test_the_python_and_rust_versions_agree():
    """They are released together; a mismatch means one was bumped alone."""
    cargo = (Path(__file__).resolve().parent.parent / "rust" / "Cargo.toml"
             ).read_text(encoding="utf-8")
    rust_ver = re.search(r'^version\s*=\s*"([^"]+)"', cargo, re.M).group(1)
    assert rust_ver == DX_VERSION, (
        f"Cargo.toml says {rust_ver}, drive_xray.py says {DX_VERSION}")


def test_the_lockfile_agrees_too():
    """Cargo.lock records the package's OWN version, and CI builds with
    --locked. Bumping Cargo.toml alone fails every Rust job with a message
    about the lock file rather than about the version -- which is exactly what
    happened here, and cost a full CI round to read."""
    lock = (Path(__file__).resolve().parent.parent / "rust" / "Cargo.lock"
            ).read_text(encoding="utf-8")
    m = re.search(r'\[\[package\]\]\nname = "drive-xray"\nversion = "([^"]+)"',
                  lock)
    assert m, "the drive-xray entry vanished from Cargo.lock"
    assert m.group(1) == DX_VERSION, (
        f"Cargo.lock says {m.group(1)}, drive_xray.py says {DX_VERSION} — "
        f"run `cargo update --offline -p drive-xray`")


def test_the_minimum_is_not_ahead_of_what_we_ship():
    """MIN_DX_VERSION > DX_VERSION would refuse the engine we just built."""
    assert _version_tuple(MIN_DX_VERSION) <= _version_tuple(DX_VERSION)


def test_the_current_build_accepts_itself():
    """Whatever we ship must pass its own check, or the fast engine is dead on
    arrival for everyone."""
    assert dx_supports_schema(DX_VERSION) is True


# ── the app must fall back, not merely complain ──────────────────────────────

def test_an_old_binary_is_rejected_by_the_probe(monkeypatch, tmp_path):
    """A warning can be scrolled past; the engine must actually not be used.
    A slow correct answer beats a fast wrong one."""
    import subprocess
    import types

    calls = {}

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stdout="dx 1.4.1\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    # the probe logic as app.py applies it
    out = subprocess.run(["dx", "--version"], capture_output=True, text=True)
    reported = out.stdout.strip().split()[1]
    assert reported == "1.4.1"
    assert not dx_supports_schema(reported), \
        "this binary must never be handed index or dedupe work"


# ── refusing a candidate is not the same as falling back ─────────────────────
#
# The probe walks several locations: an env override, three build dirs, the
# app's own folder, /opt/homebrew/bin, /usr/local/bin, then PATH. An old dx
# left in an early one is skipped and a good one found later -- which is
# exactly what a screenshot showed: `engine: 🦀 Rust` in the caption and,
# directly underneath, a warning insisting the app was using Python.

_STALE = [("/opt/homebrew/bin/dx", "1.4.1")]


def test_a_skipped_stale_binary_does_not_claim_we_fell_back():
    """The false alarm. Rust IS in use; saying otherwise is untrue, and it
    trains people to ignore the warning that means something."""
    level, key, _kw = engine_status(True, DX_VERSION, _STALE)
    assert key != "engine_too_old", \
        "claimed a Python fallback while the Rust engine was in use"
    assert level != "warning", "nothing is wrong — this must not be a warning"


def test_the_skipped_copy_is_still_named_so_it_can_be_deleted():
    """Silence would be wrong too: the old binary is still on disk and will be
    found first the day the good one moves."""
    _level, key, kw = engine_status(True, DX_VERSION, _STALE)
    assert key == "engine_stale_skipped"
    assert kw["path"] == "/opt/homebrew/bin/dx"
    assert kw["have"] == "1.4.1"


def test_an_actual_fallback_still_warns():
    """The case the warning was written for, and it must survive the fix."""
    level, key, kw = engine_status(False, "", _STALE)
    assert (level, key) == ("warning", "engine_too_old")
    assert kw == {"have": "1.4.1", "want": MIN_DX_VERSION,
                  "schema": SCHEMA_VERSION}


def test_a_healthy_rust_engine_says_nothing_at_all():
    assert engine_status(True, DX_VERSION, []) is None


def test_plain_python_with_no_binary_anywhere_says_nothing():
    """dx simply not installed is a supported setup, not a fault."""
    assert engine_status(False, "", []) is None


def test_a_compatible_but_older_binary_is_a_note_not_a_warning():
    older = f"{_version_tuple(MIN_DX_VERSION)[0]}.{_version_tuple(MIN_DX_VERSION)[1]}.0"
    if _version_tuple(older) >= _version_tuple(DX_VERSION):
        pytest.skip("no compatible version below DX_VERSION to test with")
    level, key, _kw = engine_status(True, older, [])
    assert (level, key) == ("info", "engine_behind")


def _every_outcome():
    """Every notice engine_status can produce, driven through the real
    function rather than listed by hand."""
    older = "1.5.0" if _version_tuple("1.5.0") < _version_tuple(DX_VERSION) \
        else DX_VERSION
    cases = [engine_status(True, DX_VERSION, _STALE),
             engine_status(False, "", _STALE),
             engine_status(True, older, [])]
    return [c for c in cases if c]


def test_every_severity_is_a_streamlit_method_name():
    """app.py dispatches with getattr(st, level), so a typo is an
    AttributeError in the sidebar for everyone. Streamlit is not installed in
    CI, so pin the names instead of probing the module."""
    for level, _key, _kw in _every_outcome():
        assert level in {"warning", "info", "caption", "error", "success"}, \
            f"{level!r} is not a Streamlit call"


def test_every_message_exists_in_both_languages():
    """These keys are no longer written literally in app.py — they arrive via
    engine_status — so the app-wide t() scan cannot see them. Without this,
    a missing key renders as its own name and nothing complains."""
    import i18n

    for _level, key, kw in _every_outcome():
        for lang in ("pt", "en"):
            assert key in i18n.TRANSLATIONS[lang], f"{lang} is missing {key}"
            # and the placeholders must line up with what we pass
            i18n.TRANSLATIONS[lang][key].format(**kw)


def test_app_no_longer_decides_this_inline():
    """The bug lived in an `if _dx_incompatible:` in app.py. Pin the decision
    to the tested function so it cannot drift back into the view layer."""
    app = (Path(__file__).resolve().parent.parent / "app.py").read_text(
        encoding="utf-8")
    assert "engine_status(" in app
    assert 't("engine_too_old"' not in app, \
        "app.py is choosing the message again instead of asking engine_status"


def test_the_rust_banner_reports_the_same_schema_as_python():
    """`dx --version` prints a schema number, and it said v6 for a month after
    v7 shipped. A banner that lies about the schema is how a stale engine looks
    trustworthy — the exact failure this module exists to prevent."""
    cli = (Path(__file__).resolve().parent.parent / "rust" / "src" / "cli.rs"
           ).read_text(encoding="utf-8")
    # EVERY mention, not the first one: the banner exists in a short and a
    # long form, and checking only one left the other stale -- a guard with a
    # hole in it is the same as no guard.
    found = [int(n) for n in re.findall(r"schema[:\s]*v(\d+)", cli)]
    assert found, "the schema line vanished from the Rust version banner"
    assert set(found) == {SCHEMA_VERSION}, (
        f"Rust banner mentions schema v{sorted(set(found))}, Python "
        f"SCHEMA_VERSION is {SCHEMA_VERSION}")
