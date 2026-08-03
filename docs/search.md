# Finding things across drives

Every other view answers *"what is on this drive?"*. This one answers
*"where is that thing?"* — across every x-ray at once, **including drives that
are unplugged**. That is the whole point of an index: the answer does not
require the disk.

```bash
dx find "STP*" type:folder
dx find "*.mkv" ">20GB"
dx find "*.bam" "modified<2024" "drive:8Tb"
```

Or the **🔎 Procurar / Find** tab in the UI.

Results give the drive, the path inside it, size, date — and whether that drive
is plugged in right now, which is what you actually want to know before walking
to the drawer.

## The query language

Terms combine with **AND**: every one must hold.

### Name

| | |
|---|---|
| `STP*` | name starts with STP |
| `*.mkv` | name ends in `.mkv` |
| `relatorio` | name *contains* that text |
| `name:STP*` | the same, said explicitly |

A term containing `*`, `?` or `[` is a glob anchored to the **whole name**;
anything else is a substring. Both ignore case, including accented characters —
`*ção*` and `*ÇÃO*` find the same files.

Matching is on the **name**, not the path. `STP*` finds a folder called
`STP_projects`, not every file inside it. Use `path:` for that.

### Type, size, date, place

| | |
|---|---|
| `type:folder` / `type:file` | one or the other |
| `>20GB` `<100MB` `>=1.5TB` | size (binary units: 1 GB = 1024 MB) |
| `size:700MB` | exactly that size |
| `modified<2024` | changed **before** 2024 began |
| `modified>2024` | changed **after** 2024 ended |
| `modified>=2024-06` | from June 2024 onwards |
| `modified:2024` | at some point during 2024 |
| `drive:8Tb` | only that drive |
| `path:"HD Movies"` | path contains this text |

Dates accept `2024`, `2024-06` or `2024-06-15`. A period is treated as a range,
which is why `<` and `>` sit outside it and `>=` / `<=` reach inside: `>2024`
means 2025 onwards, `>=2024` includes 2024 itself.

Quote anything containing spaces: `path:"HD Movies"`.

## Notes

- Only the **latest snapshot** of each drive is searched.
- Results are biggest-first, and capped (500 in the UI, `--limit` on the CLI).
  A truncated result says so — it never pretends to be the whole answer.
- A `.db` that cannot be read is reported and skipped; the other drives are
  still searched.
- Nothing here opens a file. Everything comes from what indexing already
  recorded, so it works with every drive unplugged.

## Speed

On a synthetic 400,000-entry index, every query above returns in **under 0.4 s**.
Size, date and type filters run in SQLite; name globs are refined in Python
afterwards, because `LIKE` cannot express them and its case rules do not cover
accented characters.
