# Changelog

All notable changes to drive-xray are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- Cross-OS refresh of exFAT drives re-hashed every file: exFAT stores
  local time, so the same untouched file can appear shifted by whole
  hours between macOS and Windows (timezone/DST interpretation), which
  silently disabled all hash reuse. The reuse compare in both engines
  now also accepts mtime differences that are whole-hour multiples
  (±2s) within ±26h — the same trade-off as rsync's `--modify-window`
  on FAT drives. The base tolerance widens from 1s to 2s (FAT/exFAT
  timestamp granularity). Size must still match exactly.

### Added
- One-click "ignore system folders" exclusion set (`Windows`,
  `Program Files`, `Program Files (x86)`, `ProgramData`, `AppData`,
  `node_modules`, `PerfLogs`, `Recovery`, `$WinREAgent`): a default-on
  checkbox when indexing a new drive (seeded into the .db before the
  indexer starts, honoured by both engines) and a button in the
  Exclusions panel for existing drives.

## [1.4.0] — 2026-07-15

### Fixed
- Drives indexed on one OS are now recognized when mounted on another
  (e.g. indexed on macOS at `/Volumes/MyDisk`, plugged into Windows as
  `E:\` or into Linux as `/media/<user>/MyDisk`). Both engines resolve
  the stored `root_path` to the volume's current mount point by matching
  the snapshot's content against mounted volumes (content fingerprint —
  no reliance on volume labels). A match requires a majority of the
  snapshot's top-level names (min 2 when there are 2+) AND at least one
  of the largest indexed files present at its exact rel_path + byte
  size, so generic folder names can't cause a false positive. The
  stored path itself is validated the same way when it still exists —
  a different disk that took over `E:\` or `/Volumes/Name` is detected
  and the real volume is found elsewhere. With no confident match the
  drive is treated as not mounted, exactly as before. Applies to
  refresh, snapshot, dedupe, export, cleanup/backup scripts,
  verify-integrity and the UI's mounted-drive checks (`resolve_root` in
  `drive_xray.py`, `db::resolve_root` in Rust).
- Python indexing on Windows stored `rel_path` with `\` while the Rust
  engine always used `/`, making those `.db` files non-portable (and
  invisible to the new mount resolution). The Python indexer now always
  stores `/`, and both engines normalize old Windows-rooted `.db` files
  on open (POSIX-rooted dbs are untouched — `\` is a legal filename
  character there).
- The Rust engine detection in the UI now validates candidates with
  `dx --version`, also looks in `/opt/homebrew/bin`, `/usr/local/bin`
  and next to `app.py` (Finder/Dock launches often lack Homebrew in
  PATH, which made the engine flip between runs), and surfaces the
  fallback reason in the sidebar.
- The concurrent index/refresh guard now works on Windows (it shelled
  out to `ps`, which doesn't exist there; uses CIM via PowerShell).
- UI defaults that assumed macOS (`/Volumes/`) are now platform-aware
  (index path suggestion and backup target).

### Added
- Persistent operation log + live status: index/refresh/snapshot output is
  tee'd to `<drive>.db.log`, so closing the browser tab no longer loses the
  log. The app shows the last progress line next to the ⏳ badge in the
  sidebar and a reviewable "📜 Log da operação" panel on the drive page
  (also while the drive is busy/locked). The `.log` sidecar is removed
  together with the drive's `.db`.
- Stale-engine warning: the sidebar now warns when the `dx` binary version
  differs from the app version (an old `dx.exe` silently misses features
  like exclusions, checkpointing and cross-OS mount resolution).
- `setup_shortcuts.ps1` (Windows): creates Desktop + Start Menu launch
  shortcuts for drive-xray, optional launch-at-login (`-Startup`), and
  `-Remove` to undo. Auto-detects a sibling
  [media-catalog](https://github.com/rbleite/media-catalog) checkout and
  creates its shortcuts too (its `run.bat` uses port 8503, so both apps
  can run simultaneously).
- CI: `pytest` job on ubuntu-latest and windows-latest running the
  Python test suite (previously only `cargo test` + parity ran in CI).

### Added
- GitHub Actions: `tests` workflow runs `cargo test` on macos-15-intel
  (Intel; replaces the retired macos-13 image), macos-14 (Apple Silicon)
  and windows-latest on every push and PR, plus a separate
  `parity` job that sets up a Python venv to run the cross-implementation
  parity tests.
- GitHub Actions: `release` workflow auto-builds the universal binary
  (`lipo` of arm64 + x86_64), tarballs it, computes SHA-256, and
  publishes a GitHub Release on any `v*.*.*` tag push.
- `CONTRIBUTING.md` with bug-report template and dev setup instructions.

## [1.0.0] — 2026-06-05

First public release. The full feature set is documented in
[README.md](README.md); this section lists what landed in v1.0 organized
by category.

### Added — core engine
- Python implementation (`drive_xray.py`) with full CLI surface.
- Optional Rust implementation (`rust/`) as a drop-in CLI replacement,
  ~10× faster on `index` and ~6× faster on `dedupe`. `.db` files are
  byte-for-byte identical between engines (verified by parity tests).
- BLAKE2b hashing with two-stage strategy: partial hash (head + middle +
  tail × 64 KB, 16 bytes) for fast collision filtering, full hash
  (32 bytes) computed only on size+partial matches.
- Folder Merkle hash for detecting identical directory subtrees.
- Schema v5 with **path interning** (a.k.a. Tier 3): every directory
  name lives once in a `paths` table, referenced by `entries.path_id`.
  Cuts ~20–25% off `.db` size on large drives.
- Idempotent migrations from v1/v2/v3/v4 schemas; old `.db` files open
  transparently and migrate forward on first read.

### Added — features
- Snapshots: `dx snapshot take`, `list`, `prune`, `diff`. Default
  retention: keep last 10 + 12 monthly.
- `dx refresh` (overwrites latest snapshot in place) and `dx compact`
  (`VACUUM` + `wal_checkpoint`).
- Cross-drive comparison: `dx compare a.db b.db` works offline (neither
  drive needs to be mounted).
- Export: CSV via stdlib `csv`, XLSX via `openpyxl` (Python) /
  `rust_xlsxwriter` (Rust).
- Assisted cleanup: `dx cleanup` generates a shell script you review
  before running. Four keep-strategies (shortest, oldest, newest,
  alphabetical) and two actions (quarantine to
  `~/.drive-xray-quarantine/`, or `rm`). Never deletes on its own.
- Hardlink awareness everywhere: hardlinks count as one physical file
  for "wasted space" math, and dedupe output tags them explicitly.

### Added — UI
- Streamlit UI (`app.py`) with PT/EN bilingual surface and tabs for
  Summary, Duplicates, TreeMap (Plotly), History (snapshot diff), and
  Compare across drives.
- Auto-detects the Rust binary when present; sidebar shows
  `engine: 🦀 Rust` or `engine: 🐍 Python`.
- macOS `.app` launcher via `build_app.sh`. Multi-resolution `.icns`
  built from `assets/icon.png` via `iconutil`.

### Added — macOS niceties
- `--one-filesystem` / `-x` doesn't traverse APFS firmlinks, preventing
  double-counting between `/` and `/System/Volumes/Data`.
- `--skip-cloud` skips iCloud Drive, OneDrive (including
  `OneDrive - <tenant>`), Google Drive, Dropbox, Box, MEGA, Tresorit,
  pCloud, Proton Drive, and the whole `~/Library/CloudStorage/` hub.
- u64 → i64 wrap for inodes/device IDs (exFAT, NTFS, and some APFS
  volumes report values >2⁶³−1 that would otherwise overflow
  SQLite INTEGER).

### Added — quality
- 32 tests green across four buckets:
  - 17 lib unit tests (hash, walker, util, snapshot prune)
  - 6 db_parity tests (schema migrations, mtime IEEE 754, idempotence)
  - 3 parity tests (Python ↔ Rust byte-exact)
  - 5 snapshot_flow tests (take, list, prune, diff)
  - 1 full_cycle test (every CLI subcommand in user order)
- `PRAGMA busy_timeout=5000` on every connection to avoid spurious
  `database is locked` errors when the UI and a CLI subprocess race.
- `mimalloc` as the global allocator in the Rust binary.

### Added — distribution
- Universal Mach-O binary (arm64 + x86_64) at 3.9 MB.
- Homebrew tap at https://github.com/rbleite/homebrew-tap
  (`brew tap rbleite/tap && brew install drive-xray`).
- Apache-2.0 license + `NOTICE` file listing every third-party
  dependency, ensuring the attribution chain stays intact in any
  derivative work.

### Engine performance benchmark
On Apple Silicon (M2 Pro), Apple SSD:

| Workload | Python | Rust + mimalloc | Speedup |
|---|---:|---:|---:|
| 5,284 files / 750 MB | 1.45 s | 0.13 s | **11.5×** |
| 2,000 files / 10 MB (50 dup groups) | 180 ms | 30 ms | **6×** |
| 1.4 M files / 5.2 TB external (real) | ~hours | ~14 min | — |

[Unreleased]: https://github.com/rbleite/drive-xray/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/rbleite/drive-xray/releases/tag/v1.0.0
