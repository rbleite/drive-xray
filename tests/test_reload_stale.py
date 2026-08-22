"""Reloading modules whose file changed while the app was running.

Streamlit re-executes the app script on every rerun, but `import` returns
whatever is already in memory. After a self-update with the app still running,
new code runs against old module state.

For code that fails loudly (a missing function → ImportError) this was already
handled for drive_xray. For *data* it fails quietly: a translation key added in
the same update simply is not there, and `t()` falls back to rendering the raw
key name. The app keeps working and looks broken — which is exactly how it was
found, in a screenshot showing `cross_why_empty` where a sentence should be.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from drive_xray import reload_if_stale


@pytest.fixture()
def a_module(tmp_path, monkeypatch):
    """A real importable module we can rewrite on disk."""
    monkeypatch.syspath_prepend(str(tmp_path))
    name = "stale_probe_mod"
    src = tmp_path / f"{name}.py"
    src.write_text("VALUES = {'old': 1}\n", encoding="utf-8")
    sys.modules.pop(name, None)
    mod = __import__(name)
    yield mod, src, name
    sys.modules.pop(name, None)


def test_an_unchanged_module_is_left_alone(a_module):
    mod, _src, _name = a_module
    again = reload_if_stale(mod)
    assert again is mod or again.VALUES == mod.VALUES


def test_a_changed_module_is_picked_up(a_module):
    """The actual failure: data added on disk must become visible."""
    mod, src, _name = a_module
    reload_if_stale(mod)                       # stamp the current mtime
    assert "new" not in mod.VALUES

    time.sleep(0.01)                           # ensure a distinct mtime
    src.write_text("VALUES = {'old': 1, 'new': 2}\n", encoding="utf-8")
    fresh = reload_if_stale(mod)
    assert fresh.VALUES["new"] == 2, "the update on disk never reached memory"


def test_repeated_calls_do_not_keep_reloading(a_module):
    """It runs on every Streamlit rerun, so it must be a cheap no-op when
    nothing changed."""
    mod, src, _name = a_module
    first = reload_if_stale(mod)
    stamp = first._loaded_mtime
    for _ in range(5):
        first = reload_if_stale(first)
    assert first._loaded_mtime == stamp


def test_a_module_with_no_file_is_survivable():
    """Built-ins and namespace packages have no __file__; this must not be the
    thing that takes the app down."""
    import sys as _sys
    assert reload_if_stale(_sys) is _sys


def test_a_deleted_file_does_not_raise(a_module):
    mod, src, _name = a_module
    reload_if_stale(mod)
    src.unlink()
    assert reload_if_stale(mod) is mod


def test_every_translation_key_the_app_asks_for_exists():
    """The screenshot bug in general form: t() falls back to the raw key, so a
    key the app references but i18n never defines renders as its own name and
    nothing complains."""
    import re

    import i18n

    app_src = (Path(__file__).resolve().parent.parent / "app.py").read_text(
        encoding="utf-8")
    used = set(re.findall(r'\bt\(\s*"([a-z0-9_]+)"', app_src))
    assert used, "no t() calls found — the scan itself is broken"

    for lang in ("pt", "en"):
        missing = sorted(k for k in used if k not in i18n.TRANSLATIONS[lang])
        assert not missing, f"{lang} is missing: {missing}"
