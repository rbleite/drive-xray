# Roadmap

| Status | Component |
|---|---|
| ✅ | Hybrid v2 hashing (head + middle + tail) |
| ✅ | Folder Merkle hash |
| ✅ | macOS firmlinks / cloud sync filters |
| ✅ | Schema v5 with path interning (Tier 3) |
| ✅ | Incremental refresh (reuses unchanged hashes) |
| ✅ | Historical snapshots + diff + prune |
| ✅ | TreeMap (Plotly) |
| ✅ | Assisted cleanup (`.sh` script + quarantine) |
| ✅ | Bilingual UI (PT/EN) |
| ✅ | Rust engine (~10× faster) |
| ✅ | macOS `.app` launcher |
| ✅ | Homebrew tap |
| 🔜 | Snapshots V2 — content-addressed (smaller history) |
| 🔜 | APFS clone detection (`clonefile`) |
| 🔜 | In-UI cleanup execution with quarantine |
| ✅ | Search query language (`*.bam >100 GB modified<2024`) |

## Release checklist

Schema and engine ship on different clocks -- Python migrates on first run, the
Rust binary is downloaded by hand. When a migration lands:

1. bump `SCHEMA_VERSION` in `drive_xray.py` (a test fails if you forget);
2. bump `DX_VERSION` **and** `rust/Cargo.toml` together (a test checks they agree);
3. set `MIN_DX_VERSION` to that release, so older binaries are refused rather
   than silently trusted;
4. **cut the release**, or everyone on the Rust engine keeps a binary that
   cannot read what the app now writes.

Step 4 is the one that was missed: v7 landed ten days after v1.4.1 and no
release followed, so every published binary was reading a schema it did not
know.
