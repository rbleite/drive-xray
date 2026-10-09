"""install.ps1 has to survive the way the README delivers it: `irm | iex`.

It did not. The file began with a UTF-8 BOM. Reading a .ps1 from disk strips
the BOM, which is how the installer was checked at the time, but `irm` hands
it to `iex` as the character U+FEFF. The parser does not treat that as
whitespace, so the first comment line became a command named "#" and the
param block became an unknown command "param". The Windows one-liner failed on
line 1 from the day it was written.

These run on every platform in the normal test job, so a BOM or a stray
non-ASCII character is caught on any push -- not only by the slower Windows
end-to-end workflow, and not only on pushes that touch the installer.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parent.parent / "install.ps1"


def test_the_installer_has_no_byte_order_mark():
    """The exact defect. `irm` keeps a BOM; `iex` then chokes on line 1."""
    head = INSTALLER.read_bytes()[:3]
    assert head != b"\xef\xbb\xbf", (
        "install.ps1 starts with a UTF-8 BOM. `irm | iex` will fail on line 1 "
        "with \"The term '﻿#' is not recognized\". Save it without a BOM.")


def test_the_installer_is_pure_ascii():
    """Without a BOM, Windows PowerShell 5.1 reads a .ps1 as the ANSI code
    page, and `irm` decodes the download by whatever charset the server
    declares. ASCII is the only encoding every one of those paths agrees on --
    a curly quote or an em-dash can end a string mid-line."""
    bad = [(n, line) for n, line in
           enumerate(INSTALLER.read_bytes().splitlines(), 1)
           if any(b > 127 for b in line)]
    assert not bad, f"non-ASCII on line(s) {[n for n, _ in bad]}"


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
def test_the_installer_parses_the_way_iex_sees_it():
    """Parse it as `iex` does -- from a string, without stripping anything --
    and require the param block to be recognised. Parsing the FILE would strip
    a BOM and pass, which is precisely how the original check missed it."""
    # the path goes inside the script: `pwsh -Command` joins any trailing
    # arguments onto the command text instead of passing them as $args
    path = str(INSTALLER).replace("'", "''")
    script = r"""
    $s = [Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes('PATH'))
    $e = $null; $t = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseInput(
        $s, [ref]$t, [ref]$e)
    if ($e.Count) { $e | ForEach-Object { Write-Output "ERR $($_.Message)" } }
    Write-Output "PARAM $([bool]$ast.ParamBlock)"
    """.replace("PATH", path)
    out = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "ERR" not in out.stdout, out.stdout
    assert "PARAM True" in out.stdout, (
        "the param block is not recognised when parsed from a string -- "
        "`irm | iex` would treat `param(...)` as an unknown command")
