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


# ── searching by kind ────────────────────────────────────────────────────────

def test_the_categories_come_from_the_auto_tag_rules(monkeypatch):
    """Not a second taxonomy: the same rules that drive auto-tagging, so a
    category the user adds to auto_tags.yaml is searchable at once."""
    import drive_xray as dx
    from drive_xray import known_kinds
    assert {"vídeo", "genómica", "NGS", "jogos"} <= set(known_kinds())

    monkeypatch.setattr(dx, "get_auto_tag_rules",
                        lambda: [(frozenset({"xyz"}), "inventado")])
    assert known_kinds() == ["inventado"]
    assert dx.parse_query("kind:inventado")["exts"] == {"xyz"}


def test_an_unknown_kind_lists_the_real_ones():
    with pytest.raises(QueryError, match="unknown kind"):
        parse_query("kind:banana")
    try:
        parse_query("kind:banana")
    except QueryError as exc:
        assert "vídeo" in str(exc), "the error must say what IS available"


def test_accents_are_not_required_to_type(searchable):
    """`genómica` must be reachable as `genomica` — nobody should have to
    produce the accent to run a search."""
    from drive_xray import parse_query as pq
    assert pq("kind:genomica")["exts"] == pq("kind:genómica")["exts"]
    assert pq("kind:VIDEO")["exts"] == pq("kind:vídeo")["exts"]


@pytest.fixture()
def mixed_drive(tmp_path):
    """One drive holding several kinds at once."""
    root = tmp_path / "drive"
    (root / "seq").mkdir(parents=True)
    (root / "filmes").mkdir()
    (root / "roms").mkdir()
    (root / "seq" / "run1.fastq.gz").write_bytes(b"x" * 4096)
    (root / "seq" / "aligned.bam").write_bytes(b"x" * 4096)
    (root / "seq" / "notas.txt").write_bytes(b"x" * 10)
    (root / "filmes" / "filme.mkv").write_bytes(b"x" * 4096)
    (root / "roms" / "jogo.nsp").write_bytes(b"x" * 4096)
    db = tmp_path / "d.db"
    assert dx_py("index", str(root), "--db", str(db), "--label", "Mixed"
                 ).returncode == 0
    return [(db, "Mixed")]


def test_a_kind_selects_only_that_kind(mixed_drive):
    res = search_drives(mixed_drive, "kind:NGS", check_mounted=False)
    names = sorted(Path(h["path"]).name for h in res["hits"])
    assert names == ["aligned.bam", "run1.fastq.gz"]


def test_compound_extensions_are_understood(mixed_drive):
    """fastq.gz must count as NGS, not as an archive — the taxonomy treats it
    as one extension and the search has to agree."""
    ngs = search_drives(mixed_drive, "kind:NGS", check_mounted=False)
    assert any(h["path"].endswith("run1.fastq.gz") for h in ngs["hits"])


def test_kinds_combine_with_the_rest_of_the_query(mixed_drive):
    res = search_drives(mixed_drive, "kind:jogos", check_mounted=False)
    assert [Path(h["path"]).name for h in res["hits"]] == ["jogo.nsp"]
    none = search_drives(mixed_drive, "kind:jogos >1GB", check_mounted=False)
    assert none["hits"] == []


def test_several_kinds_widen_the_search(mixed_drive):
    res = search_drives(mixed_drive, "kind:vídeo kind:jogos",
                        check_mounted=False)
    assert sorted(Path(h["path"]).name for h in res["hits"]) == \
        ["filme.mkv", "jogo.nsp"]


def test_a_kind_alone_is_a_valid_search(mixed_drive):
    """The picker in the UI sends only a kind, with no typed text."""
    res = search_drives(mixed_drive, "kind:imagens", check_mounted=False)
    assert res["hits"] == [] and res["total"] == 0     # none present, no error


# ── spacing around a comparison ──────────────────────────────────────────────
#
# `*.mkv > 1GB` returned nothing on a drive full of large .mkv files. The
# splitter cuts on whitespace, so it arrived as three terms and the last two
# fell through to the name filter: the search looked for files whose name
# contains ">" AND contains "1gb", found none, and reported no results.
#
# Only one of the four reasonable spellings worked. The other three were not
# errors -- they silently meant something else, which is the worst way for a
# search to fail, because zero looks like an answer.

EQUIVALENT = ["*.mkv >1GB", "*.mkv > 1GB", "*.mkv>1GB",
              "*.mkv size>1GB", "*.mkv size > 1GB", "*.mkv size> 1GB"]


@pytest.mark.parametrize("q", EQUIVALENT)
def test_every_spelling_of_a_size_comparison_means_the_same(q):
    f = parse_query(q)
    assert f["size_min"] == 1024 ** 3 + 1, q
    assert f["names"] == ["*.mkv"], f"stray tokens leaked into the name filter: {f['names']}"


def test_a_spaced_date_comparison_is_not_read_as_a_size():
    """The nastiest form of the bug: `modified >= 2024` left `modified` as a
    name and applied 2024 as a SIZE, so it silently searched for files over
    2024 bytes named "modified"."""
    spaced = parse_query("modified >= 2024")
    tight = parse_query("modified>=2024")
    assert spaced["mtime_min"] == tight["mtime_min"]
    assert spaced["size_min"] is None
    assert spaced["names"] == []


def test_an_operator_with_nothing_after_it_is_an_error():
    """It used to become a name search for '>'."""
    with pytest.raises(QueryError):
        parse_query("*.mkv >")


def test_a_quoted_phrase_containing_an_operator_stays_a_name():
    """Quoting means literal. Without this, `"report > final"` was read as a
    comparison on a field called `report`."""
    f = parse_query('"relatorio > final"')
    assert f["names"] == ["relatorio > final"]
    assert f["size_min"] is None


def test_spacing_does_not_disturb_the_other_fields():
    f = parse_query("drive:8Tb type:file *.mkv > 20GB")
    assert f["drives"] == ["8tb"]
    assert f["is_dir"] == 0
    assert f["names"] == ["*.mkv"]
    assert f["size_min"] == 20 * 1024 ** 3 + 1


# ── email ────────────────────────────────────────────────────────────────────

@pytest.fixture()
def mail_drive(tmp_path):
    """Saved mail in the forms Outlook and Apple Mail leave behind."""
    root = tmp_path / "drive"
    files = {
        "Outlook/arquivo 2015.pst": 4096,
        "Outlook/cache.ost": 4096,
        "Outlook Mac/export.olm": 4096,
        "Outlook 15 Profiles/Data/Messages/x.olk15Message": 100,
        "Mail/V10/INBOX.mbox/Messages/123.emlx": 100,
        "Mail/V10/INBOX.mbox/Messages/124.partial.emlx": 100,
        "Takeout/All mail Including Spam and Trash.mbox": 4096,
        "soltos/recibo.eml": 100,
        "soltos/reuniao.MSG": 100,
        "soltos/notas.txt": 100,
    }
    for rel, size in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
    db = tmp_path / "m.db"
    assert dx_py("index", str(root), "--db", str(db), "--label", "Mail"
                 ).returncode == 0
    return [(db, "Mail")]


def test_email_is_a_kind():
    from drive_xray import known_kinds
    assert "email" in known_kinds()


def test_kind_email_finds_outlook_and_apple_mail(mail_drive):
    res = search_drives(mail_drive, "kind:email", check_mounted=False)
    names = sorted(Path(h["path"]).name for h in res["hits"])
    assert names == sorted([
        "arquivo 2015.pst", "cache.ost", "export.olm", "x.olk15Message",
        "123.emlx", "124.partial.emlx",
        "All mail Including Spam and Trash.mbox",
        "recibo.eml", "reuniao.MSG",
        # the Apple Mail mailbox FOLDER, matched by its name -- the useful
        # hit, since it holds thousands of .emlx
        "INBOX.mbox",
    ]), names


def test_kind_email_combines_with_size(mail_drive):
    """The everyday question: where are the big Outlook archives?"""
    res = search_drives(mail_drive, "kind:email >1KB", check_mounted=False)
    names = sorted(Path(h["path"]).name for h in res["hits"])
    assert names == sorted(["arquivo 2015.pst", "cache.ost", "export.olm",
                            "All mail Including Spam and Trash.mbox"]), names


def test_email_has_its_own_line_in_the_composition(mail_drive):
    from drive_xray import drive_composition
    rep = drive_composition(mail_drive)
    by_kind = rep["drives"][0]["by_kind"]
    assert by_kind["email"]["bytes"] == 4 * 4096 + 5 * 100


# ── spaces between a field and its value ─────────────────────────────────────
#
# Reported from the app: `kind: email` failed with "unknown kind ''". The
# splitter cut on the space, leaving `kind:` with no value and `email` as a
# name to search for.

@pytest.mark.parametrize("q", ["kind: email", "kind :email", "kind : email",
                               "KIND: email"])
def test_a_field_and_its_value_may_be_spaced(q):
    assert parse_query(q)["exts"] == parse_query("kind:email")["exts"]


def test_spacing_works_for_the_other_fields_too():
    f = parse_query('drive: 8Tb path: "HD Movies" type: folder')
    assert f["drives"] == ["8tb"]
    assert f["paths"] == ["hd movies"]
    assert f["is_dir"] == 1


def test_only_real_fields_are_rejoined():
    """`relatorio: final` is not glued into `relatorio:final`; it still
    reaches parse_query as two terms, and `relatorio:` is reported as an
    unknown field exactly as it was before."""
    from drive_xray import _split_terms
    assert _split_terms("relatorio: final") == ["relatorio:", "final"]
    with pytest.raises(QueryError, match="unknown field 'relatorio'"):
        parse_query("relatorio: final")


def test_a_field_with_nothing_after_it_says_so():
    with pytest.raises(QueryError, match="needs a value"):
        parse_query("kind:")


# ── an old auto_tags.yaml must not freeze the categories ─────────────────────
#
# The user's screenshot listed the known kinds without `jogos` or `email`:
# their auto_tags.yaml was written by `dx auto-tags --init` before either
# existed, and the file used to replace the built-in rules entirely.

@pytest.fixture()
def rules_file(tmp_path, monkeypatch):
    import drive_xray as dx
    path = tmp_path / "auto_tags.yaml"
    monkeypatch.setattr(dx, "AUTO_TAGS_YAML_PATH", path)
    return path


def test_an_old_rules_file_still_gets_the_new_categories(rules_file):
    from drive_xray import known_kinds
    rules_file.write_text(
        "NGS: [bam, fastq]\nvídeo: [mkv, mp4]\narquivo: [zip]\n",
        encoding="utf-8")
    kinds = known_kinds()
    assert {"email", "jogos"} <= set(kinds), kinds
    assert parse_query("kind: email")["exts"] >= {"pst", "olm", "emlx"}


def test_the_file_still_overrides_a_category_it_names(rules_file):
    rules_file.write_text("vídeo: [mkv]\n", encoding="utf-8")
    assert parse_query("kind:vídeo")["exts"] == {"mkv"}


def test_an_empty_list_turns_a_builtin_category_off(rules_file):
    from drive_xray import known_kinds
    rules_file.write_text("jogos: []\nvídeo: [mkv]\n", encoding="utf-8")
    assert "jogos" not in known_kinds()


def test_an_extension_the_file_moves_is_not_claimed_twice(rules_file):
    """Moving iso from jogos to arquivo must not leave iso in jogos as well."""
    from drive_xray import get_auto_tag_rules
    rules_file.write_text("arquivo: [zip, iso]\n", encoding="utf-8")
    owners = [tag for exts, tag in get_auto_tag_rules() if "iso" in exts]
    assert owners == ["arquivo"], owners


def test_the_files_own_categories_come_first(rules_file):
    from drive_xray import known_kinds
    rules_file.write_text("meus: [xyz]\n", encoding="utf-8")
    assert known_kinds()[0] == "meus"


def test_a_broken_rules_file_falls_back_to_the_defaults(rules_file):
    from drive_xray import DEFAULT_AUTO_TAG_RULES, get_auto_tag_rules
    rules_file.write_text("this: [is: not: yaml", encoding="utf-8")
    assert get_auto_tag_rules() == DEFAULT_AUTO_TAG_RULES


def test_the_other_mac_mail_files_are_email_too(tmp_path):
    """Outlook for Mac keeps the full message in .olk15MsgSource and its
    attachments in .olk15MsgAttach; Apple Mail keeps some attachments as
    .emlxpart; Thunderbird keeps a .msf index beside each mailbox -- and the
    mailbox itself, which has no extension, is recognised by where it is."""
    root = tmp_path / "drive"
    files = [
        "Outlook 15 Profiles/Main Profile/Data/Message Sources/0T/a.olk15MsgSource",
        "Outlook 15 Profiles/Main Profile/Data/Message Attachments/0T/b.olk15MsgAttach",
        "Mail/V10/X/INBOX.mbox/Y/Data/Messages/7.2.emlxpart",
        "Thunderbird/Profiles/p/Mail/Local Folders/Inbox.msf",
        "Thunderbird/Profiles/p/Mail/Local Folders/Inbox",      # the mailbox
        "notes/mail.nsf",
    ]
    for rel in files:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 100)
    db = tmp_path / "mac.db"
    assert dx_py("index", str(root), "--db", str(db), "--label", "Mac"
                 ).returncode == 0
    res = search_drives([(db, "Mac")], "kind:email type:file", check_mounted=False)
    names = sorted(Path(h["path"]).name for h in res["hits"])
    assert names == sorted(["a.olk15MsgSource", "b.olk15MsgAttach",
                            "7.2.emlxpart", "Inbox.msf", "Inbox",
                            "mail.nsf"]), names


# ── folders the walk cannot open ─────────────────────────────────────────────
#
# On macOS ~/Library/Mail needs Full Disk Access. Without it the walk could
# not open the folder and dropped its contents silently, so a Mac's own
# mailbox was absent from its index and `kind:email` found nothing there.

def test_the_warning_names_the_folders_and_explains_macos():
    from drive_xray import unreadable_warning
    msg = unreadable_warning(["Library/Mail", "Library/Safari"],
                             platform="darwin")
    assert "2 folder(s)" in msg and "Library/Mail" in msg
    assert "Full Disk Access" in msg
    assert "Full Disk Access" not in unreadable_warning(["x"], platform="linux")


def test_a_long_list_is_cut_short():
    from drive_xray import unreadable_warning
    msg = unreadable_warning([f"d{i}" for i in range(9)], platform="linux")
    assert "9 folder(s)" in msg and msg.rstrip().endswith("...")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_indexing_reports_a_folder_it_could_not_read(tmp_path):
    root = tmp_path / "home"
    locked = root / "Library" / "Mail"
    (locked / "V10").mkdir(parents=True)
    (locked / "V10" / "1.emlx").write_bytes(b"x")
    (root / "ok.txt").write_bytes(b"y")
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK | os.X_OK):
            pytest.skip("running as root: nothing is unreadable")
        db = tmp_path / "h.db"
        r = dx_py("index", str(root), "--db", str(db), "--label", "H")
    finally:
        locked.chmod(0o755)
    assert r.returncode == 0, r.stderr
    assert "could not be read" in r.stderr and "Library/Mail" in r.stderr, r.stderr


# ── mailboxes without an extension ───────────────────────────────────────────
#
# Thunderbird keeps each folder as a plain mbox file named "Inbox", "Sent" ...;
# Eudora for Mac named its mailboxes "In", "Out"; an Apple Mail export keeps
# its messages in a file called "mbox". No extension rule can see them, so
# they are recognised by the folders they live in.

@pytest.fixture()
def legacy_mail(tmp_path):
    root = tmp_path / "drive"
    files = {
        "Library/Thunderbird/Profiles/ab.default/Mail/Local Folders/Inbox": 5000,
        "Library/Thunderbird/Profiles/ab.default/Mail/Local Folders/Inbox.msf": 100,
        "Library/Thunderbird/Profiles/ab.default/Mail/Local Folders/Inbox.sbd/CCMAR": 5000,
        "VAIO/AppData/Roaming/Thunderbird/Profiles/x.default/ImapMail/mail.ualg.pt/INBOX": 5000,
        "Old Mac/Eudora Folder/Mail Folder/In": 5000,
        "Export/Inbox.mbox/mbox": 5000,
        "VAIO/AppData/Local/Packages/microsoft.windowscommunicationsapps_8wekyb3d8bbwe/LocalState/HxStore.hxd": 5000,
        # extensionless files that are NOT mail
        "Documents/Mail/README": 5000,
        "Projects/LICENSE": 5000,
    }
    for rel, size in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
    db = tmp_path / "legacy.db"
    assert dx_py("index", str(root), "--db", str(db), "--label", "Old"
                 ).returncode == 0
    return [(db, "Old")]


def test_extensionless_mailboxes_are_found_by_kind(legacy_mail):
    res = search_drives(legacy_mail, "kind:email type:file", check_mounted=False)
    names = sorted(Path(h["path"]).name for h in res["hits"])
    assert names == sorted(["Inbox", "Inbox.msf", "CCMAR", "INBOX", "In",
                            "mbox", "HxStore.hxd"]), names


def test_other_extensionless_files_are_not_mail(legacy_mail):
    res = search_drives(legacy_mail, "kind:email", check_mounted=False)
    paths = [h["path"] for h in res["hits"]]
    assert not any(p.endswith(("README", "LICENSE")) for p in paths), paths


def test_extensionless_mailboxes_combine_with_size(legacy_mail):
    res = search_drives(legacy_mail, "kind:email >1KB type:file",
                        check_mounted=False)
    assert "Inbox.msf" not in [Path(h["path"]).name for h in res["hits"]]
    assert "In" in [Path(h["path"]).name for h in res["hits"]]


def test_extensionless_mailboxes_count_as_email_in_the_composition(legacy_mail):
    from drive_xray import drive_composition
    by_kind = drive_composition(legacy_mail)["drives"][0]["by_kind"]
    # 5 mailboxes and HxStore.hxd at 5000 bytes each, plus the 100-byte .msf
    assert by_kind["email"]["bytes"] == 6 * 5000 + 100
    assert by_kind["outros"]["bytes"] == 2 * 5000          # README, LICENSE


# ── system folders: only on the disk Windows runs from ───────────────────────

def test_system_folders_are_excluded_on_the_running_windows_drive():
    from drive_xray import SYSTEM_EXCLUDE_DIRS, system_excludes_for
    assert system_excludes_for("C:\\", platform="nt",
                               system_drive="C:") == SYSTEM_EXCLUDE_DIRS
    assert "AppData" in system_excludes_for("c:\\Users\\r", platform="nt",
                                            system_drive="C:")


@pytest.mark.parametrize("root, platform", [
    ("E:\\", "nt"),                      # a backup drive on the PC
    ("D:\\Backups\\VAIO", "nt"),
    ("/Volumes/4TbMyBook", "posix"),     # the same drive on the Mac
    ("/", "posix"),
])
def test_appdata_is_not_excluded_anywhere_else(root, platform):
    from drive_xray import system_excludes_for
    got = system_excludes_for(root, platform=platform, system_drive="C:")
    assert "AppData" not in got and got == ["node_modules"]


def test_the_note_names_skipped_folders_and_flags_appdata():
    from drive_xray import excluded_note
    msg = excluded_note(["VAIO/Users/rleite/AppData", "build"])
    assert "2 folder(s)" in msg and "VAIO/Users/rleite/AppData" in msg
    assert "Outlook" in msg
    assert "Outlook" not in excluded_note(["node_modules"])


def test_indexing_says_which_folders_the_exclusion_list_skipped(tmp_path):
    from drive_xray import set_exclusions
    root = tmp_path / "backup"
    (root / "Users" / "r" / "AppData" / "Local").mkdir(parents=True)
    (root / "Users" / "r" / "AppData" / "Local" / "x.ost").write_bytes(b"x")
    (root / "ok.txt").write_bytes(b"y")
    db = tmp_path / "b.db"
    set_exclusions(db, ["AppData"])
    r = dx_py("index", str(root), "--db", str(db), "--label", "B")
    assert r.returncode == 0, r.stderr
    assert "Users/r/AppData" in r.stderr and "Outlook" in r.stderr, r.stderr


def test_the_app_seeds_system_folders_through_system_excludes_for():
    """The app used to add SYSTEM_EXCLUDE_DIRS to every index; it must go
    through the system-disk check, or backups lose their AppData again."""
    src = (Path(__file__).resolve().parent.parent / "app.py").read_text(
        encoding="utf-8")
    assert "system_excludes_for(expanded)" in src
    assert "_existing + SYSTEM_EXCLUDE_DIRS" not in src
