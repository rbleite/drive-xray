"""Search across x-rays.

The point of the index is that it keeps answering with the drive unplugged, so
every test here builds a real drive, indexes it, and then searches the .db --
never the filesystem. The tree is deliberately awkward: accented names, a
folder and a file sharing a prefix, sizes and dates spread across boundaries.
"""
from __future__ import annotations

import datetime
import os
from pathlib import Path

import pytest
from conftest import dx_py

from drive_xray import QueryError, parse_date_range, parse_query, parse_size, search_drives


# ── the parser ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("512", 512), ("512B", 512),
    ("20GB", 20 * 1024**3), ("20 gb", 20 * 1024**3), ("20G", 20 * 1024**3),
    ("1.5TB", int(1.5 * 1024**4)), ("1,5TB", int(1.5 * 1024**4)),
    ("500MB", 500 * 1024**2), ("100K", 100 * 1024),
])
def test_sizes(text, expected):
    assert parse_size(text) == expected


@pytest.mark.parametrize("bad", ["", "GB", "20 apples", "-5GB", "twenty"])
def test_a_size_that_is_not_a_size_says_so(bad):
    with pytest.raises(QueryError):
        parse_size(bad)


def test_a_year_expands_to_the_whole_year():
    start, end = parse_date_range("2024")
    assert datetime.datetime.fromtimestamp(start).date() == datetime.date(2024, 1, 1)
    assert datetime.datetime.fromtimestamp(end).date() == datetime.date(2025, 1, 1)


def test_december_rolls_into_the_next_year():
    """The month+1 arithmetic is exactly where an off-by-one would hide."""
    start, end = parse_date_range("2024-12")
    assert datetime.datetime.fromtimestamp(start).date() == datetime.date(2024, 12, 1)
    assert datetime.datetime.fromtimestamp(end).date() == datetime.date(2025, 1, 1)


def test_a_day_expands_to_that_day():
    start, end = parse_date_range("2024-06-15")
    assert datetime.datetime.fromtimestamp(end) - \
        datetime.datetime.fromtimestamp(start) == datetime.timedelta(days=1)


@pytest.mark.parametrize("bad", ["2024-13", "2024-02-31", "not-a-date", "24"])
def test_a_date_that_is_not_a_date_says_so(bad):
    with pytest.raises(QueryError):
        parse_date_range(bad)


def test_terms_combine_with_and():
    f = parse_query("*.mkv >20GB type:file")
    assert f["names"] == ["*.mkv"]
    assert f["size_min"] == 20 * 1024**3 + 1
    assert f["is_dir"] == 0


def test_before_a_year_means_before_it_began():
    f = parse_query("modified<2024")
    assert f["mtime_max"] == parse_date_range("2024")[0]


def test_after_a_year_means_after_it_ended():
    """`>2024` must not include 2024 itself, or 'after 2024' returns 2024."""
    f = parse_query("modified>2024")
    assert f["mtime_min"] == parse_date_range("2024")[1]


def test_from_a_year_includes_it():
    f = parse_query("modified>=2024")
    assert f["mtime_min"] == parse_date_range("2024")[0]


def test_during_a_year_is_bounded_both_ends():
    f = parse_query("modified:2024")
    start, end = parse_date_range("2024")
    assert f["mtime_min"] == start and f["mtime_max"] == end


def test_quoted_phrases_survive_the_split():
    f = parse_query('path:"Ricardo/HD Movies" *.mkv')
    assert "ricardo/hd movies" in f["paths"]
    assert f["names"] == ["*.mkv"]


@pytest.mark.parametrize("bad,msg", [
    ("", "empty"),
    ("   ", "empty"),
    ("colour:red", "unknown field"),
    ("type:banana", "file or folder"),
])
def test_a_query_that_cannot_work_explains_itself(bad, msg):
    with pytest.raises(QueryError, match=msg):
        parse_query(bad)


# ── searching a real index ───────────────────────────────────────────────────

@pytest.fixture()
def searchable(tmp_path):
    """A drive with names and sizes chosen to catch sloppy matching."""
    root = tmp_path / "drive"
    (root / "STP_projects" / "sub").mkdir(parents=True)
    (root / "STPfiles").mkdir()
    (root / "Ricardo" / "HD Movies").mkdir(parents=True)
    (root / "Fotos Braga").mkdir()

    (root / "STP_projects" / "notes.txt").write_bytes(b"x" * 10)
    (root / "STP_projects" / "sub" / "STP_report.pdf").write_bytes(b"x" * 2048)
    (root / "STPfiles" / "readme.md").write_bytes(b"x" * 100)
    (root / "Ricardo" / "HD Movies" / "filme.mkv").write_bytes(b"x" * (3 * 1024**2))
    (root / "Ricardo" / "HD Movies" / "pequeno.mkv").write_bytes(b"x" * 512)
    (root / "Fotos Braga" / "Ficheiro Acentuado Ção.jpg").write_bytes(b"x" * 300)
    (root / "nao_stp.txt").write_bytes(b"x" * 50)

    old = datetime.datetime(2019, 3, 1).timestamp()
    os.utime(root / "nao_stp.txt", (old, old))

    db = tmp_path / "drive.db"
    r = dx_py("index", str(root), "--db", str(db), "--label", "TestDrive")
    assert r.returncode == 0, r.stderr
    return [(db, "TestDrive")]


def _paths(res):
    return sorted(h["path"] for h in res["hits"])


def test_a_prefix_glob_matches_the_name_not_the_path(searchable):
    """'STP*' must not match everything *inside* STP_projects/."""
    res = search_drives(searchable, "STP*", check_mounted=False)
    names = {Path(p).name for p in _paths(res)}
    assert names == {"STP_projects", "STPfiles", "STP_report.pdf"}
    assert "notes.txt" not in names          # lives under STP_projects/, not named STP*


def test_narrowing_to_folders(searchable):
    res = search_drives(searchable, "STP* type:folder", check_mounted=False)
    assert {Path(p).name for p in _paths(res)} == {"STP_projects", "STPfiles"}
    assert all(h["is_dir"] for h in res["hits"])


def test_a_bare_word_is_a_substring_of_the_name(searchable):
    res = search_drives(searchable, "report", check_mounted=False)
    assert [Path(p).name for p in _paths(res)] == ["STP_report.pdf"]


def test_matching_ignores_case_including_accents(searchable):
    """Lower-casing has to reach beyond ASCII, or half a Portuguese drive is
    unsearchable."""
    res = search_drives(searchable, "*ção*", check_mounted=False)
    assert [Path(p).name for p in _paths(res)] == ["Ficheiro Acentuado Ção.jpg"]
    upper = search_drives(searchable, "*ÇÃO*", check_mounted=False)
    assert _paths(upper) == _paths(res)
    # and as a bare substring, not just as a glob
    bare = search_drives(searchable, "acentuado", check_mounted=False)
    assert _paths(bare) == _paths(res)


def test_a_glob_is_anchored_to_the_whole_name(searchable):
    """'ção*' means starts-with, so it must NOT match a name that merely
    contains it — otherwise every glob silently behaves as a substring."""
    assert search_drives(searchable, "ção*", check_mounted=False)["hits"] == []


def test_size_filter(searchable):
    res = search_drives(searchable, "*.mkv >1MB", check_mounted=False)
    assert [Path(p).name for p in _paths(res)] == ["filme.mkv"]


def test_size_and_glob_together_exclude_the_small_one(searchable):
    big = search_drives(searchable, "*.mkv", check_mounted=False)
    assert len(big["hits"]) == 2                    # both mkv
    small = search_drives(searchable, "*.mkv <1KB", check_mounted=False)
    assert [Path(p).name for p in _paths(small)] == ["pequeno.mkv"]


def test_date_filter_finds_the_old_file(searchable):
    res = search_drives(searchable, "modified<2020 type:file", check_mounted=False)
    assert [Path(p).name for p in _paths(res)] == ["nao_stp.txt"]


def test_path_filter_scopes_to_a_folder(searchable):
    res = search_drives(searchable, 'path:"HD Movies" type:file',
                        check_mounted=False)
    assert {Path(p).name for p in _paths(res)} == {"filme.mkv", "pequeno.mkv"}


def test_results_say_which_drive(searchable):
    res = search_drives(searchable, "*.mkv", check_mounted=False)
    assert {h["drive"] for h in res["hits"]} == {"TestDrive"}
    assert res["drives"] == ["TestDrive"]


def test_filtering_by_drive_excludes_the_others(searchable):
    assert search_drives(searchable, "*.mkv drive:nosuchdrive",
                         check_mounted=False)["hits"] == []
    assert search_drives(searchable, "*.mkv drive:TestDrive",
                         check_mounted=False)["hits"]


def test_biggest_first(searchable):
    res = search_drives(searchable, "type:file", check_mounted=False)
    sizes = [h["size"] for h in res["hits"]]
    assert sizes == sorted(sizes, reverse=True)


def test_the_limit_is_reported_not_hidden(searchable):
    """A silently truncated result reads as 'that is everything'."""
    res = search_drives(searchable, "type:file", limit=2, check_mounted=False)
    assert len(res["hits"]) == 2
    assert res["truncated"] is True
    assert res["total"] > 2


def test_no_match_is_an_empty_result_not_an_error(searchable):
    res = search_drives(searchable, "zzz_nothing_like_this*", check_mounted=False)
    assert res["hits"] == [] and res["total"] == 0 and res["truncated"] is False


def test_an_unreadable_db_is_reported_and_the_rest_still_search(searchable, tmp_path):
    junk = tmp_path / "broken.db"
    junk.write_bytes(b"this is not sqlite")
    res = search_drives(list(searchable) + [(junk, "Broken")], "*.mkv",
                        check_mounted=False)
    assert res["errors"] and "Broken" in res["errors"][0]
    assert res["hits"], "a broken drive must not sink the whole search"
