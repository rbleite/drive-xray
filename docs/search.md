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

### Kind

Rather than remembering extensions, search by what a file *is*:

```bash
dx find "kind:genomica"          # fastq, bam, vcf, bed, fasta …
dx find "kind:jogos >4GB"        # iso, nsp, xci, wbfs …
dx find "kind:email >100MB"      # Outlook .pst/.ost/.olm, Apple Mail .emlx/.mbox …
dx find "kind:video kind:musica" # several kinds widen the search
```

Not every mailbox has an extension. Thunderbird keeps each folder as a file
named just `Inbox`, `Sent` …, and an Apple Mail export keeps its messages in a
file named `mbox` inside the `.mbox` folder. Find those by where they live:

```bash
dx find "path:Library/Mail"                          # Apple Mail
dx find "path:Thunderbird/Profiles"                  # Thunderbird
dx find "path:\"Outlook 15 Profiles\""                # Outlook for Mac
```

The categories are the **same ones that drive auto-tagging** — nanopore, NGS,
variantes, genómica, imagiologia, PDF, documentos, tabelas, apresentações,
email, música, vídeo, imagens, código, scripts, arquivo, base de dados, jogos — so
anything you add to `~/.config/drive-xray/auto_tags.yaml` becomes searchable
the moment you add it. There is no second list to maintain. The file only
overrides the categories it names: a built-in category it does not mention
still applies, so a category added in a later version (`email` arrived in
1.6.2) shows up even if your file is older. To turn a built-in one off, list
it with `[]`.

A field and its value may be separated by spaces: `kind: email`,
`drive : 8Tb` and `kind:email` are the same search.

Accents are optional: `kind:genomica` finds `genómica`. An unknown kind lists
the real ones instead of silently finding nothing. Compound extensions are
understood, so `run.fastq.gz` counts as NGS rather than as an archive.

In the UI there is a picker, so you do not have to type any of this.

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
| `kind:vídeo` | a category (see above) |

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
