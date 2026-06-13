//! drive-xray — Rust port of the Python CLI core (`drive_xray.py`).
//!
//! V1 goals (see DESIGN.md): bit-exact `.db` compatibility with the Python
//! implementation, drop-in CLI surface, faster walker/hashing.
//!
//! Modules are stubs until each sprint lands. The order of population
//! follows the sprint plan in DESIGN.md.

pub mod cli;
pub mod compact;
pub mod compare;
pub mod cross_dedupe;
pub mod db;
pub mod doctor;
pub mod dup_groups;
pub mod ext_breakdown;
pub mod dedupe;
pub mod export;
pub mod hash;
pub mod index;
pub mod snapshot;
pub mod util;
pub mod walker;
pub mod cleanup;

/// Library entry called by `main.rs`. Parses CLI args and dispatches.
pub fn run() -> anyhow::Result<()> {
    cli::dispatch()
}

/// Bumped only when the partial_hash algorithm changes. v2 = head+middle+tail.
/// Must stay in sync with `HASH_VERSION` in `drive_xray.py`.
pub const HASH_VERSION: i64 = 2;

/// Size of head/middle/tail chunks for the partial hash. Matches Python
/// `PARTIAL_CHUNK = 64 * 1024`.
pub const PARTIAL_CHUNK: usize = 64 * 1024;

/// Read chunk size when computing full hashes. Matches Python `READ_CHUNK`.
pub const READ_CHUNK: usize = 1024 * 1024;
