# drive-xray-rs — design

This document captures the decisions for porting the Python CLI core
(`drive_xray.py`) to Rust as a drop-in replacement binary called `dx`.
The Streamlit UI in Python stays unchanged — it already calls the CLI
as a subprocess for `index`, `refresh`, `compact`, `snapshot`. The
Rust binary therefore needs to:

1. Produce **bit-exact-compatible `.db` files** (same schema v4, same
   BLAKE2b hashes, same Merkle digests).
2. Match the **CLI surface** of the Python script (subcommands, flags,
   stdout format) so the Streamlit integration keeps working.
3. Migrate older `.db` files (v1/v2/v3 → v4) on first open, identically
   to the Python implementation.

If those three are met, the swap is invisible to the user and to the
UI; only the time changes.

## Why Rust at all

The Python implementation is bottlenecked on three things:

1. **Walker throughput** — `os.walk` is single-threaded, doesn't
   overlap stat with anything else. ~50–100 k files/s on Apple
   Silicon SSD.
2. **Hashing throughput** — `hashlib.blake2b` is single-threaded and
   GIL-bound for orchestration. Reading + hashing 1 GB takes ~3.5 s.
3. **Snapshot multiplication** — each new snapshot pays the walker
   cost again, even if hashes are reused for the inner files.

On a 30 TB NAS with weekly snapshots, the first `index` is 4–8 h and
each `snapshot take` is 30–90 min depending on churn. With the Rust
port (parallel walker + SIMD BLAKE2b + no GIL), we target:

- **Walker**: 3–5× faster (jwalk parallel directory descent).
- **Hashing**: 2–3× faster (blake2b_simd on aarch64 NEON / x86 AVX2).
- **Snapshot cost**: the reuse path becomes essentially free because
  the bottleneck disappears.

Conservative end-to-end target: **5× on `index`, 2× on `snapshot take`**.
On a 30 TB drive that's 4 h → ~50 min, 60 min → ~30 min.

## What stays in Python

- Streamlit UI (`app.py`) — no perf issue, the heavy work is delegated.
- TreeMap rendering — plotly is fine.
- Diff aggregation for display (the data flows from the `.db` anyway).
- Cleanup script generation — single-shot, no perf benefit from porting.

The Rust crate **could** later expose PyO3 bindings so the UI consumes
hot functions (`dup_file_groups`, `treemap_rows`) directly without a
subprocess. That's Phase B in the README roadmap; not in V1.

## Crate selection

| Concern | Crate | Why |
|---|---|---|
| SQLite | `rusqlite` 0.32 | Bundled SQLite, `params!` macro, FromSql/ToSql for BLOBs, prepared statements. |
| Partial+full hash | `blake2b_simd` 1.0 | SIMD BLAKE2b that matches Python `hashlib.blake2b` byte-for-byte. Critical for `.db` compatibility. |
| Walker | `jwalk` 0.8 | Parallel directory descent (Rayon-backed), drop-in for `walkdir`. ~3× faster on multi-core. |
| Parallelism | `rayon` 1.10 | Used implicitly by jwalk; explicit for hashing pool. |
| CLI | `clap` 4 (derive) | Idiomatic subcommand parsing, matches Python `argparse` structure. |
| Errors | `anyhow` 1 + `thiserror` 1 | `anyhow::Result` in binary, `thiserror` for library errors crossing crate boundaries. |
| Progress | `indicatif` 0.17 | Stderr progress matching the Python style (e.g. `691309 files  264.45 GB  (2646/s)`). |
| Time | `chrono` 0.4 (serde feature off) | ISO timestamps `%Y-%m-%dT%H:%M:%S` to stay byte-identical to Python’s `time.strftime`. |
| CSV export | `csv` 1.3 | Trivial. |
| XLSX export | `rust_xlsxwriter` 0.79 | Pure Rust, writes XLSX 1.0 / OfficeOpenXML. Equivalent to Python’s `openpyxl`. |
| Inline hex display | `hex` 0.4 | For dedupe/diff prints. |

No async runtime. Everything is sync + rayon. SQLite is intrinsically
single-writer; async buys nothing here.

## Module layout

```
rust/
  Cargo.toml
  src/
    main.rs          # binary entry, delegates to lib::run_cli
    lib.rs           # pub modules, re-exports
    cli.rs           # clap definitions, dispatch
    db.rs            # rusqlite open, schema, migrations v1→v4
    hash.rs          # partial / full / Merkle (BLAKE2b)
    walker.rs        # jwalk + filters (one_fs, skip_cloud)
    index.rs         # index/refresh/snapshot pipelines
    snapshot.rs      # take/list/diff/prune
    dedupe.rs        # fill_full_hashes + compute_dir_hashes
    compare.rs       # cross-db match
    export.rs        # CSV/XLSX writers
    cleanup.rs       # shell script generator
    compact.rs       # VACUUM + WAL checkpoint
    util.rs          # human(), _i64() wrap, basename helpers
  tests/
    parity.rs        # compares against the Python script
```

## Pipeline for index/snapshot

The naive choice "parallel walker + parallel hash + serial writer"
runs into a parent_id ordering problem (subdirs need their parent's
rowid). The pragmatic V1 pipeline is **three explicit phases**:

```
PHASE 1 — Walk (single thread, jwalk synchronous mode)
  Emit per-entry metadata into a Vec<RawEntry>:
    rel_path, parent_rel, kind (dir|symlink|file|err),
    size, mtime, inode, dev, error
  Apply filters: -x (cross-fs prune), --skip-cloud, SKIP_DIR_NAMES.
  For files only, record `path_to_hash: PathBuf` for phase 2.

PHASE 2 — Hash (rayon par_iter on the file subset)
  For each file:
    if reuse_old.contains_key(rel_path) && (size, mtime) match:
       reuse cached partial/full
    else:
       compute partial = blake2b(size||head||middle||tail)
       if --full: compute full = blake2b(whole)
  Result written back into the corresponding RawEntry by index.

PHASE 3 — Write (single thread, rusqlite transaction)
  Iterate RawEntry in original walk order.
  Maintain HashMap<String, i64> parent_id_by_rel.
  For each entry, look up parent rowid, INSERT, save own rowid if dir.
  Batch the INSERTs inside a single transaction; commit every 50 000
  rows for progress feedback.
```

Memory cost: ~250 bytes/entry × 5 M entries = ~1.25 GB peak. For larger
drives we can stream in chunks of, say, 500 k entries per phase loop.
V1 ships the simple non-streaming version.

## Schema + migration parity

We port the exact same schema constants and `_migrate_to_v3` /
`_migrate_to_v4` SQL. Migrations live in `db.rs` and are gated on the
result of `PRAGMA table_info(entries)` exactly like Python.

Validation strategy: parity test that

1. Builds an empty `.db` with Python, opens with Rust → schema v4 active.
2. Builds an empty `.db` with Rust, opens with Python → same.
3. Synthesizes a v3 `.db` with sqlite3 directly, opens with each →
   resulting tables identical (row count, column types, indexes).

## Hash byte-exactness

Constants must match:

```rust
const PARTIAL_CHUNK: usize = 64 * 1024;
const HASH_VERSION:  i64   = 2;
```

Partial hash inputs, in order:
1. `size.to_le_bytes()` (8 bytes, little-endian)
2. If `size <= 3 * PARTIAL_CHUNK`: the whole file.
3. Else: first 64 KiB, then 64 KiB from `size/2 - 32 KiB`, then last 64 KiB.

Empty file (`size == 0`): only the size header is hashed (skip the
file open entirely, matching Python behavior).

Output: 16-byte digest, stored as BLOB.

Full hash: BLAKE2b-256 of the whole file, 32-byte BLOB.

Merkle hash for directories (same algorithm as
`compute_dir_hashes` in Python):

```
for each child sorted by name:
  if child.is_dir:
    h.update(b"D"); h.update(child.name); h.update(child.full_hash)
  else:
    h.update(b"F"); h.update(child.name); h.update(child.full_hash)
digest = h.finalize().bytes  # 32 bytes
```

A directory whose subtree has any unresolved hash (NULL or error) gets
NULL — same as Python's recursive None propagation.

## inode/device int64 wrap

Macros for SQLite INTEGER overflow on exFAT / NTFS / some APFS:

```rust
fn i64_wrap(n: u64) -> i64 {
    n as i64  // wraps automatically in Rust without overflow check
}
```

`u64::MAX as i64 == -1`, matching the Python `_i64()` shipped in
v4. Verified by the same unit-test table.

## CLI surface (clap)

```
dx index <root> [--db PATH] [--label NAME] [--full] [-x] [--skip-cloud]
dx refresh <db> [--full]
dx snapshot take <db> [--full] [--no-prune] [--keep-last N] [--keep-monthly M]
dx snapshot list <db>
dx prune <db> [--keep-last N] [--keep-monthly M]
dx diff <db> [--from N] [--to M] [--top N]
dx dedupe <db> [--min-size BYTES] [--files-only|--dirs-only]
dx compare <db_a> <db_b> [--min-size BYTES]
dx export <db> <out> [--min-size BYTES] [--format csv|xlsx]
dx cleanup <db> [--strategy …] [--action …] [--min-size BYTES] [-o FILE]
dx compact <db>
```

Defaults match the Python defaults; help strings borrowed verbatim.
The binary name is **`dx`** (not `drive-xray-rs`). Once installed it
shadows the Python alias.

## Streamlit integration unchanged

`app.py` already invokes `sys.executable drive_xray.py <subcmd>`. To
swap in the Rust binary:

```python
SCRIPT = Path(__file__).parent / "drive_xray.py"
# becomes
DX_BIN = shutil.which("dx") or str(Path(__file__).parent / "drive_xray.py")
# then either dispatches to dx if available or falls back to Python.
```

This will be a 5-line change in `app.py` once the Rust binary exists
and passes parity. The UI gains speed transparently.

## Progress

| Sprint | Status |
|---|---|
| 1. Bootstrap + schema parity | ✅ **done** |
| 2. Hash + walk + index | ✅ **done** (11.5× over Python) |
| 3. Snapshot family | ✅ **done** |
| 4. Read ops | ✅ **done** |
| 5. Hardening + release | ✅ **done** (parity 3/3, universal binary, app.py integration) |

## Sprints

The plan is sequential because each sprint validates the previous
one's contract.

### Sprint 1 — Bootstrap + schema parity ✅
- `cargo new --bin` + module skeleton.
- `db.rs`: schema constants, `open_db()`, `migrate_to_v3` /
  `migrate_to_v4` ports.
- `tests/db_parity.rs` — 5 integration tests, all green:
  fresh db has v4 schema; v2 db migrates correctly (hex→BLOB, EMPTY
  sentinel, ERR sentinel, parent_id chain, snapshot seed); open is
  idempotent; both migrations are no-ops on already-current schemas.
- Cross-implementation parity verified: `.db` produced by Python
  `dx snapshot take` opens cleanly in Rust and exposes correct counts,
  `latest_snapshot_id`, `hash_version`.
- Release binary: 726 KB (LTO + strip).

### Sprint 2 — Hash + walk + index ✅
- `hash.rs`: `partial`, `full`, `partial_of_bytes`, `merkle`. 7 unit
  tests including golden vectors precomputed via Python — paridade
  byte-a-byte com `hashlib.blake2b`.
- `walker.rs`: DFS sequencial sobre `std::fs::read_dir`, filtros
  `SKIP_DIR_NAMES` / `is_cloud_dir` / `-x`. 6 unit tests cobrindo skip,
  ordem parent-before-child, metadata correta.
- `index.rs`: pipeline 3-fase (walk → rayon par_iter hash → tx commit
  serial), 3 modos (`Fresh` / `Snapshot` / `Refresh`), reuse cache via
  `(size, mtime)`. `refresh_drive` e `snapshot_drive` wrappers expostos.
- CLI: `dx index <root> [--db] [--label] [--full] [-x] [--skip-cloud]`.
- **Benchmark**: 5284 ficheiros / 750 MB (rust target/) →
  **Python 1.45 s vs Rust 0.13 s = 11.5×**. CPU 589 % no Rust (6 cores
  activos via rayon).
- Cross-impl parity validada: `.db` produzida por `dx index` abre em
  Python, `python … dedupe` encontra os duplicados nas linhas que
  Rust escreveu, hashes byte-idênticos para amostra.

### Sprint 3 — Snapshot family ✅
- `snapshot.rs`: `take` (delegates to `index::snapshot_drive` + auto
  prune), `list`, `prune_snapshots` (10 last + 12 monthly), `diff`
  com 3 queries (added / removed / modified) e agregação por pasta
  top-2-levels.
- `print_diff` com mesmo layout do Python (caracteres `−`, `─` U+2212/U+2500).
- CLI: `dx refresh`, `dx snapshot take|list`, `dx prune`, `dx diff`.
- 5 testes integration (`tests/snapshot_flow.rs`): take+list, refresh
  overwrites in place, diff add/remove/modify counts, prune keeps
  last N, diff errors with 1 snapshot.
- Cross-impl: `.db` produzida pela Rust com 2 snapshots → `python …
  diff <db>` dá output idêntico (mesma contagem/bytes/top folders).
- **Side note:** a versão Rust corrige um pequeno bug do Python
  (`size Δ` não mostrava sinal `−` para deltas negativos). Não
  afecta os totais, só a apresentação.
- **Bug encontrado:** Rust string `\<newline>` come os espaços de
  indentação da próxima linha — partiu todas as queries multi-linha
  (`FROM entriesWHERE` syntax error). Corrigido com conversão em
  bulk para raw strings (`r#"..."#`) — 15 strings em 3 ficheiros.
  Adicionado ao DESIGN como gotcha para o resto do port.

### Sprint 4 — Read-only operations ✅
- `compact.rs`: `compact()` — `open_db` (migra v3→v4 se preciso) → close →
  fresh autocommit connection → `PRAGMA wal_checkpoint(TRUNCATE); VACUUM;`.
  Reporta antes/depois com human-formatted sizes.
- `dedupe.rs`:
  - `fill_full_hashes` com **rayon par_iter** sobre candidatos (não
    apenas sequencial como o Python).
  - `compute_dir_hashes` iterativo bottom-up (ORDER BY length(rel_path)
    DESC + memoização em HashMap; sem stack overflow em árvores
    profundas).
  - `duplicate_rows()` — helper canónico partilhado por `export` e
    `cleanup`, hardlink-aware.
  - `dedupe()` CLI replicando o output Python (linhas com `↳ hardlink`,
    summary com "excluding N hardlinks").
- `compare.rs`: build HashMap<(size, partial_hash), Vec<(rel, full)>> de
  B em memória, stream A, classifica matches em `=` (full hash
  confirmado) vs `≈` (provável). Warning ao detectar `hash_version`
  diferente entre as duas .db.
- `export.rs`: CSV via crate `csv`, XLSX via `rust_xlsxwriter` (formato
  com header bold + fill grey + thin border + freeze panes A2 + larguras
  por coluna idênticas ao `openpyxl`). 10 colunas iguais ao Python.
- `cleanup.rs`: shell script generator com 4 estratégias
  (`Shortest`/`Oldest`/`Newest`/`Alphabetical`) e 2 acções
  (`Delete`/`Quarantine`). Quoting bash idêntico (single-quote escape
  com `'\''`), notas de hardlink, summary final. Validado com `bash -n`.
- CLI: 5 subcommandos novos ligados.
- **Cross-impl confirmada**: Rust `dx dedupe` produz o mesmo grupo
  hash/wasted/count que Python `python … dedupe` na mesma `.db`. CSV
  Rust idêntico ao CSV Python (mesmas colunas, mesmos valores).
- **Binário release pós-Sprint 4: 3.7 MB** (cresceu com `rust_xlsxwriter`
  + `csv` mas ainda <10 % do tamanho de um bundle Python equivalente).

### Sprint 5 — Hardening ✅
- `tests/parity.rs`: 3 end-to-end suites que correm Python e Rust
  contra a mesma árvore sintética (com duplicados, hardlinks, ficheiros
  vazios, ficheiros >192 KB, unicode em nomes, symlinks, profundidade
  variada). Asserções: mesmas paths, mesmos `is_dir`/`size`/`partial_hex`/
  `full_hex`/`is_symlink`, mesmas contagens de inode populado.
  - **Bug encontrado e corrigido durante este sprint:** Rust escrevia
    `mtime/inode/device` para o root entry; Python deixa-os NULL. Não
    afecta queries (root é sempre `is_dir=1` e nunca aparece em
    dedupe), mas partia a paridade exata. Ajustado `walker.rs` para
    deixar tudo NULL no root, em conformidade com Python.
- Release builds: `cargo build --release --target aarch64-apple-darwin`
  e `x86_64-apple-darwin`. Universal binary via
  `lipo -create target/{arm64,x86_64}/release/dx -output target/universal/dx`.
  - **Tamanhos**: 3.7 MB arm64, 3.9 MB x86_64, 7.6 MB universal.
- **`app.py` integration**: novo helper `_dx_command_prefix()` que
  resolve a ordem:
    1. `$DRIVE_XRAY_DX` env var (explicit override)
    2. `rust/target/{universal,release}/dx` adjacente
    3. `dx` no PATH
    4. fallback para `[sys.executable, drive_xray.py]`
  `DX_CMD` é o prefixo aplicado a todos os subprocesses
  (`index`/`refresh`/`snapshot`/`compact`). Indicador `🦀 Rust` ou
  `🐍 Python` aparece na sidebar como caption do título.
- Homebrew tap: deferred — depois de um release tag no GitHub.

**Total de testes pós-Sprint 5: 31** (17 lib + 6 db_parity + 3 parity
+ 5 snapshot_flow). Tempo de wall-clock acumulado da port: cerca de
2 dias de trabalho concentrado em vez das ~5 semanas estimadas
originalmente, em larga medida porque a paridade byte-a-byte do
hashing e do schema removeu praticamente todas as discussões de
"comportamento ambíguo".

Total: ~5 weeks of part-time work (entregue em ~2 dias concentrados).

## Feedback notes (post-mortem after Sprints 1-4)

### mtime byte-equivalence (validated, not just claimed)

Concern raised: Python expõe mtime como float (s + sub-segundos), Rust
usa `SystemTime` / `i64 sec + i64 nsec`. Risco: a `.db` v4 escrita pelos
dois pode ter precisões diferentes na coluna `mtime REAL`, partindo
retro-compat.

Verificado empiricamente:

- CPython internamente computa `st_mtime = (double)tv_sec + (double)tv_nsec * 1e-9`.
- A nossa `walker::unix_mtime()` faz literalmente
  `md.mtime() as f64 + md.mtime_nsec() as f64 * 1e-9`.
- A IEEE 754 não tem "ordem" para esta expressão — em ambos os
  caminhos a mesma sequência de FMA produz o mesmo padrão de 8 bytes.
- SQLite `REAL` é exactamente IEEE 754 double, round-trip sem perda.

Smoke test num diretório com 5 ficheiros, indexado pelos dois
binários, comparando `struct.pack("<d", mtime)` por path:
**6 / 6 paths bit-identical**. (O caso que parecia divergir era a
própria `.db` quando guardada dentro do diretório indexado — race com
a escrita Rust/Python e não um bug de precisão.)

Test de regressão permanente:
`tests/db_parity.rs::mtime_storage_matches_cpython_formula`.

### PyO3 vs Tauri (revisão do roadmap)

Argumento do utilizador (concordo): Tauri (Fase C) traz pouco valor
relativo ao trabalho — 90 % do benefício de performance está em
Fases A + B. A UI Streamlit é "boa o suficiente" para o nicho-alvo
(bio / labs / fotógrafos).

Nuance adicional pós-Sprints 1-4: o **modelo de subprocess** que já
existe (Phase A — `app.py` chama `dx index`/`snapshot take`/`compact`
como subprocesso) **resolve sozinho ~95 % do problema de perf** para
operações de longa duração. Medições:

- `dx index` no `rust/target/` (5284 ficheiros): **11.5× sobre Python**
  já testado.
- Operações UI (clicks em "Procurar duplicados", treemap render,
  diff display): SQLite faz a maior parte, em ~1 s em 770 k ficheiros.
  Não há pressão imediata para PyO3.

**Re-ordenação proposta do roadmap:**

1. **Fase A consolidada** (Sprint 5 + uma linha em `app.py`) — passa a
   default usar o binário Rust quando presente. O 90 % da entrega.
2. **Medir** — em vez de assumir, perfilar o UI com a `.db` real do
   utilizador. Se `dup_file_groups` em 5 M files arrastar, então Fase B.
3. **Fase B PyO3 só onde justificado** — hot path identificado por
   medição. Provavelmente `compute_dir_hashes` em snapshots grandes
   e `treemap_rows` em árvores muito profundas.
4. **Fase C Tauri** — só se houver pedido genuíno de distribuição
   `.app` para utilizadores que não querem ver Python/CLI. Para os
   nichos-alvo (técnicos), Streamlit é OK.

Custo escondido do PyO3 que devo flag-ar: o build matrix multiplica-se
por (Python ABI × arch × OS), distribuição via wheels, ABI breaks
quando Python sobe minor. Subprocess é flat (1 binário por arch).

## Out of scope for V1

- **BLAKE3**: would invalidate compat with existing `.db`. Schedule for
  V2 with new `hash_version=3` + UI awareness.
- **PyO3 bindings**: Phase B.
- **Streaming pipeline** (folding walk+hash+write into one): Phase
  A2, once V1 is proven.
- **APFS clone detection** via `clonefile`/`fcntl`: separate feature,
  same priority in Rust as Python.

## How to start (when the user wants to)

```bash
# install rust toolchain (one-time)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source $HOME/.cargo/env
rustup target add aarch64-apple-darwin x86_64-apple-darwin

# build
cd ~/tools/drive-xray/rust
cargo build --release
./target/release/dx --help
```

Then point the Streamlit `app.py` at `target/release/dx` instead of the
Python script (5-line change described above), keep both side-by-side
until parity tests pass for a few weeks, then retire the Python CLI
core.

The Streamlit UI itself stays in Python indefinitely until/unless
Phase C (Tauri) is undertaken.
