# How it works

1. **Hybrid indexing** — partial hash (head + middle + tail × 64 KB,
   BLAKE2b 128) is constant-time per file. Full hash (BLAKE2b 256) is
   computed **only** on duplicate candidates. On 30 TB this saves
   hours vs. "hash everything".
2. **Snapshots** — each `dx snapshot take` creates an immutable
   record. `dx diff #2 #5` shows growth and shrink per folder between
   two points in time.
3. **Schema v5 with path interning** — every directory name lives once
   in the `paths` table. On a 1.4 M-file drive this cuts the `.db`
   from ~1 GB down to ~650 MB.
4. **macOS-aware** — `-x` (firmlinks), `--skip-cloud`, 64-bit inode
   handling on exFAT/NTFS without overflow.

Full documentation: [`reference.md`](reference.md).
Rust engine architecture: [`rust/DESIGN.md`](../rust/DESIGN.md).


For the full command reference see [reference.md](reference.md);
for the Rust engine's architecture see
[`rust/DESIGN.md`](../rust/DESIGN.md).

## Benchmark

Tested on Apple Silicon (M2 Pro), Apple SSD:

| Workload | Python | Rust + mimalloc | Speedup |
|---|---:|---:|---:|
| 5,284 files / 750 MB | 1.45 s | 0.13 s | **11.5 ×** |
| 2,000 files / 10 MB (50 dup groups) | 180 ms | 30 ms | **6 ×** |

Real-world: 1.4 M files / 5.2 TB external drive → `.db` 648 MB,
indexing in ~14 min on Rust.

