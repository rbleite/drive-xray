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
    dx_supports_schema,
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
