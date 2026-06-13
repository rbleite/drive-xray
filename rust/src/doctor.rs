//! `dx doctor <db>` — validates a .db file and reports problems.

use anyhow::Result;
use rusqlite::{params, Connection, OpenFlags};
use std::path::Path;

struct Check {
    name: &'static str,
    ok: bool,
    detail: String,
}

impl Check {
    fn pass(name: &'static str, detail: impl Into<String>) -> Self {
        Self { name, ok: true, detail: detail.into() }
    }
    fn fail(name: &'static str, detail: impl Into<String>) -> Self {
        Self { name, ok: false, detail: detail.into() }
    }
}

pub fn doctor(db_path: &Path) -> Result<bool> {
    // Open read-only — must NOT call open_db() which runs migrations and
    // recreates missing indexes with CREATE INDEX IF NOT EXISTS, hiding problems.
    let conn = Connection::open_with_flags(
        db_path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    let mut checks: Vec<Check> = Vec::new();
    let mut all_ok = true;

    // ── 1. schema version ────────────────────────────────────────────────────
    let has_paths: bool = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='paths'",
            [],
            |r| r.get::<_, i64>(0),
        )
        .unwrap_or(0)
        > 0;

    let has_path_id: bool = if has_paths {
        conn.query_row(
            "SELECT COUNT(*) FROM pragma_table_info('entries') WHERE name='path_id'",
            [],
            |r| r.get::<_, i64>(0),
        )
        .unwrap_or(0)
            > 0
    } else {
        false
    };

    if has_paths && has_path_id {
        checks.push(Check::pass("schema", "v5 (path interning)"));
    } else if has_paths {
        checks.push(Check::fail("schema", "paths table exists but entries.path_id missing — partial v5 migration?"));
    } else {
        checks.push(Check::fail("schema", "pre-v5 schema (no paths table) — run dx to migrate"));
    }

    // ── 2. drive row ─────────────────────────────────────────────────────────
    let drive_row: Option<(String, String, i64)> = conn
        .query_row(
            "SELECT label, root_path, hash_version FROM drive LIMIT 1",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .ok();

    match &drive_row {
        Some((label, root, hv)) => {
            checks.push(Check::pass("drive", format!("label={label:?}  root={root:?}")));
            let expected_hv = crate::HASH_VERSION;
            if *hv == expected_hv {
                checks.push(Check::pass("hash_version", format!("v{hv} (BLAKE2b head+mid+tail)")));
            } else {
                checks.push(Check::fail(
                    "hash_version",
                    format!("v{hv} in DB but binary expects v{expected_hv} — hashes need recompute"),
                ));
            }
        }
        None => {
            checks.push(Check::fail("drive", "no row in drive table"));
        }
    }

    // ── 3. snapshots ─────────────────────────────────────────────────────────
    let snap_count: i64 = conn
        .query_row("SELECT COUNT(*) FROM snapshots", [], |r| r.get(0))
        .unwrap_or(0);
    if snap_count > 0 {
        checks.push(Check::pass("snapshots", format!("{snap_count} snapshot(s)")));
    } else {
        checks.push(Check::fail("snapshots", "no snapshots — run `dx index`"));
    }

    // ── 4. expected indexes ───────────────────────────────────────────────────
    let expected: &[&str] = &[
        "idx_snap_parent",
        "idx_snap_size_partial",
        "idx_full",
        "idx_snap_inode",
        "idx_snap_path_id",
    ];
    let mut stmt = conn.prepare(
        "SELECT name FROM sqlite_master WHERE type='index'",
    )?;
    let found: std::collections::HashSet<String> = stmt
        .query_map([], |r| r.get(0))?
        .filter_map(|r| r.ok())
        .collect();
    let missing: Vec<&str> = expected.iter().copied().filter(|n| !found.contains(*n)).collect();
    if missing.is_empty() {
        checks.push(Check::pass("indexes", format!("{} indexes present", found.len())));
    } else {
        checks.push(Check::fail("indexes", format!("missing: {}", missing.join(", "))));
    }

    // ── 5. orphan entries (path_id not in paths) ──────────────────────────────
    if has_paths && has_path_id {
        let orphans: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM entries WHERE path_id IS NOT NULL \
                 AND path_id NOT IN (SELECT id FROM paths)",
                [],
                |r| r.get(0),
            )
            .unwrap_or(0);
        if orphans == 0 {
            checks.push(Check::pass("orphan_entries", "none"));
        } else {
            checks.push(Check::fail("orphan_entries", format!("{orphans} entries with missing path_id")));
        }
    }

    // ── 6. entry count vs snapshot metadata ───────────────────────────────────
    if snap_count > 0 {
        let sid: i64 = conn
            .query_row("SELECT id FROM snapshots ORDER BY id DESC LIMIT 1", [], |r| r.get(0))?;
        let actual_files: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM entries WHERE snapshot_id=? AND is_dir=0",
                params![sid],
                |r| r.get(0),
            )
            .unwrap_or(0);
        let actual_dirs: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM entries WHERE snapshot_id=? AND is_dir=1",
                params![sid],
                |r| r.get(0),
            )
            .unwrap_or(0);
        let meta_files: i64 = conn
            .query_row("SELECT total_files FROM snapshots WHERE id=?", params![sid], |r| r.get(0))
            .unwrap_or(-1);
        let meta_dirs: i64 = conn
            .query_row("SELECT total_dirs FROM snapshots WHERE id=?", params![sid], |r| r.get(0))
            .unwrap_or(-1);

        let detail = format!(
            "snapshot #{sid}: {actual_files} files / {actual_dirs} dirs (metadata: {meta_files}/{meta_dirs})"
        );
        // Dirs: Python indexer historically skips root dir in metadata — allow ±1.
        // Files: warn if metadata differs >5% from actual (suggests partial/corrupt index).
        let dirs_ok = meta_dirs < 0 || (actual_dirs - meta_dirs).unsigned_abs() <= 1;
        let files_pct_diff = if meta_files > 0 {
            ((actual_files - meta_files).unsigned_abs() as f64 / meta_files as f64 * 100.0) as i64
        } else { 0 };
        let files_ok = meta_files < 0 || files_pct_diff <= 5;
        if files_ok && dirs_ok {
            checks.push(Check::pass("entry_counts", detail));
        } else {
            checks.push(Check::fail("entry_counts", format!("mismatch — {detail}")));
        }

        // ── 7. partial_hash coverage ──────────────────────────────────────────
        let total_files: i64 = actual_files;
        let hashed: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM entries WHERE snapshot_id=? AND is_dir=0 AND partial_hash IS NOT NULL",
                params![sid],
                |r| r.get(0),
            )
            .unwrap_or(0);
        let pct = if total_files > 0 { hashed * 100 / total_files } else { 100 };
        let hash_detail = format!("{hashed}/{total_files} files have partial_hash ({pct}%)");
        if pct >= 95 {
            checks.push(Check::pass("hash_coverage", hash_detail));
        } else if pct >= 50 {
            checks.push(Check::pass("hash_coverage", format!("{hash_detail} — consider re-indexing")));
        } else {
            checks.push(Check::fail("hash_coverage", format!("{hash_detail} — re-index to enable duplicate detection")));
        }
    }

    // ── 8. WAL file ───────────────────────────────────────────────────────────
    let wal = db_path.with_extension("db-wal");
    let wal_exists = wal.exists();
    if !wal_exists {
        checks.push(Check::pass("wal", "no WAL file (clean)"));
    } else {
        let wal_size = std::fs::metadata(&wal).map(|m| m.len()).unwrap_or(0);
        if wal_size < 10_000_000 {
            checks.push(Check::pass("wal", format!("WAL present but small ({} bytes)", wal_size)));
        } else {
            checks.push(Check::fail(
                "wal",
                format!("WAL is large ({:.1} MB) — run `dx compact` before syncing", wal_size as f64 / 1e6),
            ));
        }
    }

    // ── print results ─────────────────────────────────────────────────────────
    let name_w = checks.iter().map(|c| c.name.len()).max().unwrap_or(10);
    println!("\ndx doctor  {}", db_path.display());
    println!("{}", "─".repeat(60));
    for c in &checks {
        let mark = if c.ok { "✓" } else { "✗" };
        if !c.ok { all_ok = false; }
        println!("  {mark}  {:<width$}  {}", c.name, c.detail, width = name_w);
    }
    println!("{}", "─".repeat(60));
    if all_ok {
        println!("  all checks passed\n");
    } else {
        let failures = checks.iter().filter(|c| !c.ok).count();
        println!("  {failures} check(s) failed\n");
    }
    Ok(all_ok)
}
