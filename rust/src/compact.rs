//! VACUUM + WAL checkpoint. Mirrors `compact_db` in `drive_xray.py`.
//! Also runs the v3→v4 migration via `open_db` if needed.

use crate::db;
use crate::util::human;
use anyhow::Result;
use rusqlite::Connection;
use std::path::Path;

pub fn compact(db_path: &Path) -> Result<()> {
    let before = file_size(db_path);
    let before_wal = file_size(&wal_path(db_path));

    // Step 1: open + migrate, commit and close so no locks remain.
    let conn = db::open_db(db_path)?;
    drop(conn);

    // Step 2: fresh autocommit connection for checkpoint + VACUUM.
    eprintln!("  checkpoint + vacuum...");
    let conn = Connection::open(db_path)?;
    // Autocommit mode: VACUUM cannot run inside a transaction.
    conn.execute_batch("PRAGMA wal_checkpoint(TRUNCATE); VACUUM;")?;
    drop(conn);

    let after = file_size(db_path);
    let after_wal = file_size(&wal_path(db_path));
    let saved = (before as i64 + before_wal as i64) - (after as i64 + after_wal as i64);
    eprintln!(
        "  {} + {} (wal) → {} + {} (wal)  [{}{}]",
        human(before as f64),
        human(before_wal as f64),
        human(after as f64),
        human(after_wal as f64),
        if saved >= 0 { "-" } else { "+" },
        human(saved.unsigned_abs() as f64),
    );
    Ok(())
}

fn file_size(p: &Path) -> u64 {
    std::fs::metadata(p).map(|m| m.len()).unwrap_or(0)
}

fn wal_path(db: &Path) -> std::path::PathBuf {
    let s = format!("{}-wal", db.display());
    std::path::PathBuf::from(s)
}
