//! Three-phase index/snapshot/refresh pipeline:
//!   1. walk  — collect Vec<RawEntry> (single thread, std::fs)
//!   2. hash  — rayon par_iter over the file subset
//!   3. write — single SQLite transaction, parent_id resolved on the fly
//!
//! See DESIGN.md "Pipeline for index/snapshot".

use crate::db;
use crate::hash;
use crate::util::i64_wrap;
use crate::walker::{self, RawEntry};
use crate::HASH_VERSION;
use anyhow::{anyhow, Context, Result};
use chrono::Local;
use rayon::prelude::*;
use rusqlite::{params, Connection};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::Instant;

#[derive(Debug, Clone, Copy)]
pub enum Mode {
    /// Wipe entries + snapshots + drive, start over with a new snapshot.
    Fresh,
    /// Preserve previous snapshots, append a new one.
    Snapshot,
    /// Overwrite the latest snapshot in place.
    Refresh,
}

/// (size, mtime, partial, full) keyed by rel_path. Used by snapshot/refresh
/// to skip hashing for files unchanged by (size, mtime).
pub type ReuseCache = HashMap<String, ReuseEntry>;

#[derive(Debug, Clone)]
pub struct ReuseEntry {
    pub size: u64,
    pub mtime: f64,
    pub partial: Option<[u8; 16]>,
    pub full: Option<[u8; 32]>,
}

/// Build the reuse cache by reading the latest snapshot from `conn`.
pub fn build_reuse_cache(conn: &Connection) -> Result<ReuseCache> {
    let sid = match db::latest_snapshot_id(conn)? {
        Some(s) => s,
        None => return Ok(ReuseCache::new()),
    };
    let mut stmt = conn.prepare(
        r#"SELECT rel_path, size, mtime, partial_hash, full_hash
           FROM entries
           WHERE snapshot_id=? AND is_dir=0 AND error IS NULL
             AND is_symlink=0 AND partial_hash IS NOT NULL"#,
    )?;
    let rows = stmt.query_map([sid], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, Option<i64>>(1)?,
            r.get::<_, Option<f64>>(2)?,
            r.get::<_, Option<Vec<u8>>>(3)?,
            r.get::<_, Option<Vec<u8>>>(4)?,
        ))
    })?;
    let mut cache = ReuseCache::new();
    for row in rows {
        let (rp, size, mtime, partial, full) = row?;
        let size = match size { Some(s) if s >= 0 => s as u64, _ => continue };
        let mtime = match mtime { Some(m) => m, None => continue };
        let partial = partial.and_then(|v| <[u8; 16]>::try_from(v.as_slice()).ok());
        let full = full.and_then(|v| <[u8; 32]>::try_from(v.as_slice()).ok());
        cache.insert(rp, ReuseEntry { size, mtime, partial, full });
    }
    Ok(cache)
}

/// Drop-in port of `index_drive()` from `drive_xray.py`. Returns the
/// snapshot_id that was written into.
pub fn index_drive(
    root: &Path,
    db_path: &Path,
    label: Option<&str>,
    do_full: bool,
    one_fs: bool,
    skip_cloud: bool,
    reuse_old: Option<ReuseCache>,
    mode: Mode,
    target_snapshot_id: Option<i64>,
) -> Result<i64> {
    let root_canon = root.canonicalize()
        .with_context(|| format!("canonicalize {}", root.display()))?;

    eprintln!(
        "indexing {} → {}{}",
        root_canon.display(),
        db_path.display(),
        match (one_fs, skip_cloud) {
            (true, true) => " (one-filesystem, skip-cloud)",
            (true, false) => " (one-filesystem)",
            (false, true) => " (skip-cloud)",
            _ => "",
        }
    );

    let conn = db::open_db(db_path)?;

    // Bulk-load optimization (Fresh / Refresh only): drop the entries indexes
    // so the DELETE below and the INSERTs in write_phase don't pay per-row
    // index maintenance — on a multi-million-row table that dominates runtime.
    // Rebuilt in a single pass after write_phase. Snapshot mode is excluded so
    // we don't rebuild indexes over accumulated history to add one snapshot.
    let bulk_reindex = matches!(mode, Mode::Fresh | Mode::Refresh);
    if bulk_reindex {
        db::drop_entries_indexes(&conn)?;
        // CRITICAL: `entries` has a self-referential FK (parent_id -> id).
        // With foreign_keys=ON, deleting each row triggers a reverse lookup
        // for referencing children; without idx_snap_parent (just dropped)
        // that lookup is a full table scan → the snapshot DELETE below becomes
        // O(n²) on a 4M-row table (hours). We're replacing a whole snapshot
        // (parents and children go together), so FK enforcement adds nothing
        // here — disable it on this connection for the bulk delete. write_phase
        // uses its own connection (FK on) and its inserts check the parent via
        // the always-present PK on entries(id), so they stay both fast & safe.
        conn.pragma_update(None, "foreign_keys", "OFF")?;
    }

    // -------- phase 0: snapshot allocation / cleanup --------
    let snap_id = allocate_snapshot(&conn, label, one_fs, skip_cloud,
                                     mode, target_snapshot_id)?;

    // -------- phase 1: walk --------
    let exclude = db::read_exclusions(&conn).unwrap_or_default();
    let t0 = Instant::now();
    let walk_res = walker::walk(&root_canon, one_fs, skip_cloud, &exclude)?;
    let n_walked = walk_res.entries.len();
    let stats = &walk_res.stats;
    eprintln!("  walk: {} entries in {:.1}s (firmlinks_skipped={}, crossed={}, cloud_skipped={}, excluded={})",
        n_walked, t0.elapsed().as_secs_f64(),
        stats.firmlinks_skipped, stats.crossed, stats.cloud_skipped, stats.excluded);

    // -------- phase 2: write the file tree FIRST (no hashes yet) --------
    // Persisting the structure right after the walk means a long scan
    // interrupted during hashing (the slow, I/O-bound phase on big/slow disks)
    // keeps every file already walked, instead of losing hours of work to a
    // single final commit.
    let entries = walk_res.entries;
    let t2 = Instant::now();
    let (row_ids, total_files, total_dirs, total_size) =
        write_structure_phase(db_path, snap_id, &entries)?;
    eprintln!(
        "  write-structure: {} files / {} dirs / {} bytes in {:.1}s",
        total_files, total_dirs, total_size, t2.elapsed().as_secs_f64()
    );

    // -------- phase 3: hash (parallel) + UPDATE incrementally --------
    // Hashes are computed in parallel and written back in committed batches, so
    // a crash leaves a usable partial snapshot. A later `refresh` rebuilds its
    // reuse-cache from the rows already hashed (partial_hash IS NOT NULL) and
    // skips re-reading them — effectively resuming where it stopped.
    let t1 = Instant::now();
    let (n_hashed, n_reused) = hash_update_phase(
        db_path, &root_canon, &entries, &row_ids, do_full, reuse_old.as_ref(),
    )?;
    eprintln!(
        "  hash: {} candidates in {:.1}s ({} reused)",
        n_hashed, t1.elapsed().as_secs_f64(), n_reused,
    );

    // -------- rebuild indexes (paired with the earlier drop) --------
    if bulk_reindex {
        let t_idx = Instant::now();
        // execute_batch runs CREATE INDEX IF NOT EXISTS from SCHEMA_V5,
        // rebuilding the entries indexes in one bulk pass over the final rows.
        conn.execute_batch(db::SCHEMA_V5)?;
        eprintln!("  reindex: entries indexes rebuilt in {:.1}s",
                  t_idx.elapsed().as_secs_f64());
    }

    // -------- final: update drive row + snapshot totals --------
    let conn2 = db::open_db(db_path)?;
    conn2.execute(
        "UPDATE snapshots SET total_files=?, total_dirs=?, total_size=? WHERE id=?",
        params![total_files, total_dirs, total_size, snap_id],
    )?;
    let stamp = Local::now().format("%Y-%m-%dT%H:%M:%S").to_string();
    let label_final = label.unwrap_or_else(|| {
        root_canon.file_name()
            .and_then(|s| s.to_str()).unwrap_or("drive")
    });
    match mode {
        Mode::Fresh => {
            conn2.execute(
                r#"INSERT INTO drive (label, root_path, indexed_at, total_files, total_dirs, total_size, hash_version, opt_one_fs, opt_skip_cloud) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"#,
                params![label_final, root_canon.to_string_lossy(), stamp,
                         total_files, total_dirs, total_size, HASH_VERSION,
                         one_fs as i64, skip_cloud as i64],
            )?;
        }
        Mode::Snapshot | Mode::Refresh => {
            conn2.execute(
                r#"UPDATE drive SET indexed_at=?, total_files=?, total_dirs=?, total_size=?, hash_version=?, opt_one_fs=?, opt_skip_cloud=?"#,
                params![stamp, total_files, total_dirs, total_size,
                         HASH_VERSION, one_fs as i64, skip_cloud as i64],
            )?;
        }
    }
    // Refresh query-planner statistics after bulk inserts so subsequent queries
    // (dedupe, diff, doctor) pick optimal index paths without a manual ANALYZE.
    conn2.execute_batch("PRAGMA optimize")?;
    Ok(snap_id)
}

fn allocate_snapshot(
    conn: &Connection,
    label: Option<&str>,
    one_fs: bool,
    skip_cloud: bool,
    mode: Mode,
    target_snapshot_id: Option<i64>,
) -> Result<i64> {
    let stamp = Local::now().format("%Y-%m-%dT%H:%M:%S").to_string();
    match mode {
        Mode::Fresh => {
            conn.execute("DELETE FROM entries", [])?;
            conn.execute("DELETE FROM snapshots", [])?;
            conn.execute("DELETE FROM drive", [])?;
            conn.execute(
                r#"INSERT INTO snapshots (taken_at, label, hash_version, opt_one_fs, opt_skip_cloud) VALUES (?, ?, ?, ?, ?)"#,
                params![stamp, label, HASH_VERSION,
                         one_fs as i64, skip_cloud as i64],
            )?;
            Ok(conn.last_insert_rowid())
        }
        Mode::Snapshot => {
            conn.execute(
                r#"INSERT INTO snapshots (taken_at, label, hash_version, opt_one_fs, opt_skip_cloud) VALUES (?, ?, ?, ?, ?)"#,
                params![stamp, label, HASH_VERSION,
                         one_fs as i64, skip_cloud as i64],
            )?;
            Ok(conn.last_insert_rowid())
        }
        Mode::Refresh => {
            let sid = match target_snapshot_id {
                Some(s) => Some(s),
                None => db::latest_snapshot_id(conn)?,
            };
            match sid {
                Some(s) => {
                    conn.execute("DELETE FROM entries WHERE snapshot_id=?", params![s])?;
                    conn.execute(
                        r#"UPDATE snapshots SET taken_at=?, label=?, hash_version=?, opt_one_fs=?, opt_skip_cloud=? WHERE id=?"#,
                        params![stamp, label, HASH_VERSION,
                                 one_fs as i64, skip_cloud as i64, s],
                    )?;
                    Ok(s)
                }
                None => {
                    // refresh with no existing snapshot — fall back to fresh
                    conn.execute("DELETE FROM entries", [])?;
                    conn.execute("DELETE FROM drive", [])?;
                    conn.execute(
                        r#"INSERT INTO snapshots (taken_at, label, hash_version, opt_one_fs, opt_skip_cloud) VALUES (?, ?, ?, ?, ?)"#,
                        params![stamp, label, HASH_VERSION,
                                 one_fs as i64, skip_cloud as i64],
                    )?;
                    Ok(conn.last_insert_rowid())
                }
            }
        }
    }
}

/// Phase 3: hash file entries in parallel and write the hashes back with
/// `UPDATE … WHERE id=?`, processing in chunks so each committed batch is a
/// durable checkpoint. Returns (n_hashed, n_reused). Dirs/symlinks/errors carry
/// no hash and are skipped. `row_ids[i]` is the entries.id for `entries[i]`.
fn hash_update_phase(
    db_path: &Path,
    root: &Path,
    entries: &[RawEntry],
    row_ids: &[i64],
    do_full: bool,
    reuse: Option<&ReuseCache>,
) -> Result<(usize, usize)> {
    const CHUNK: usize = 20_000;
    // Indices of the entries that actually need a hash (files only).
    let file_idx: Vec<usize> = (0..entries.len())
        .filter(|&i| {
            let e = &entries[i];
            !e.is_dir && !e.is_symlink && e.error.is_none()
        })
        .collect();

    let mut conn = db::open_db(db_path)?;
    let mut n_hashed = 0usize;
    let mut n_reused = 0usize;

    for chunk in file_idx.chunks(CHUNK) {
        // Compute this chunk's hashes in parallel.
        let computed: Vec<(i64, Option<Vec<u8>>, Option<Vec<u8>>, bool)> = chunk
            .par_iter()
            .map(|&i| {
                let e = &entries[i];
                let size = e.size.unwrap_or(0);
                let mut partial: Option<[u8; 16]> = None;
                let mut full: Option<[u8; 32]> = None;
                let mut reused = false;

                // 1) reuse cache (size + mtime within 1s: HFS+ 1s vs APFS 1ns)
                if let Some(cache) = reuse {
                    if let Some(c) = cache.get(&e.rel_path) {
                        if c.size == size
                            && (c.mtime - e.mtime.unwrap_or(0.0)).abs() < 1.0
                        {
                            partial = c.partial;
                            full = c.full;
                            reused = true;
                        }
                    }
                }
                let abs: PathBuf = if e.rel_path == "." {
                    root.to_path_buf()
                } else {
                    root.join(&e.rel_path)
                };
                if partial.is_none() {
                    partial = hash::partial(&abs, size).ok();
                }
                if do_full && full.is_none() {
                    full = hash::full(&abs).ok();
                }
                (row_ids[i], partial.map(|b| b.to_vec()),
                 full.map(|b| b.to_vec()), reused)
            })
            .collect();

        // Write this chunk back in one committed transaction (a checkpoint).
        let tx = conn.transaction()?;
        {
            let mut stmt = tx.prepare(
                "UPDATE entries SET partial_hash=?, full_hash=? WHERE id=?")?;
            for (rid, partial, full, reused) in &computed {
                if partial.is_some() {
                    n_hashed += 1;
                }
                if *reused {
                    n_reused += 1;
                }
                stmt.execute(params![partial, full, rid])?;
            }
        }
        tx.commit()?;
    }
    Ok((n_hashed, n_reused))
}

/// Phase 2: insert the walked file tree with NULL hashes, committing in
/// batches so the structure is durable before the slow hashing starts. Returns
/// (row_ids aligned to `entries`, total_files, total_dirs, total_size).
///
/// Parent resolution and path interning survive batch boundaries via the in-RAM
/// `parent_id_by_rel` / `path_id_cache`: a parent committed in an earlier batch
/// still resolves for a child in a later one (its row_id is cached, and the row
/// exists so an FK check passes). Entries are inserted parent-before-child (walk
/// order), exactly as before, so the resulting rows are byte-identical to the
/// single-transaction path once phase 3 fills the hashes.
fn write_structure_phase(
    db_path: &Path,
    snap_id: i64,
    entries: &[RawEntry],
) -> Result<(Vec<i64>, i64, i64, i64)> {
    const BATCH: usize = 50_000;
    let mut conn = db::open_db(db_path)?;
    let mut parent_id_by_rel: HashMap<String, i64> = HashMap::new();
    let mut path_id_cache: HashMap<String, i64> = HashMap::new();
    let mut row_ids: Vec<i64> = Vec::with_capacity(entries.len());
    let mut total_files: i64 = 0;
    let mut total_dirs: i64 = 0;
    let mut total_size: i64 = 0;

    let mut start = 0usize;
    while start < entries.len() {
        let end = (start + BATCH).min(entries.len());
        let tx = conn.transaction()?;
        {
            let mut stmt = tx.prepare(
                r#"INSERT INTO entries (snapshot_id, rel_path, path_id, parent_id, is_dir, size, mtime, partial_hash, full_hash, is_symlink, error, inode, device) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"#,
            )?;
            for e in &entries[start..end] {
                let path_id = db::intern_path(&tx, &e.rel_path, &mut path_id_cache)?;
                let parent_id: Option<i64> = e.parent_rel.as_ref()
                    .and_then(|p| parent_id_by_rel.get(p).copied());
                let inode = e.inode.map(i64_wrap);
                let device = e.device.map(i64_wrap);
                let size = e.size.map(|s| s as i64);
                let none: Option<Vec<u8>> = None;   // hashes filled in phase 3

                stmt.execute(params![
                    snap_id, e.rel_path, path_id, parent_id, e.is_dir as i64,
                    size, e.mtime, none, none,
                    e.is_symlink as i64, e.error, inode, device,
                ])?;
                let row_id = tx.last_insert_rowid();
                row_ids.push(row_id);
                if e.is_dir {
                    parent_id_by_rel.insert(e.rel_path.clone(), row_id);
                    if e.parent_rel.is_some() {
                        total_dirs += 1; // count subdirs, not the root entry
                    }
                } else if !e.is_symlink && e.error.is_none() {
                    total_files += 1;
                    total_size += e.size.unwrap_or(0) as i64;
                }
            }
        }
        tx.commit()?;
        start = end;
    }

    Ok((row_ids, total_files, total_dirs, total_size))
}

/// Wrapper used by `dx refresh`.
pub fn refresh_drive(db_path: &Path, do_full: bool) -> Result<i64> {
    let (root, label, one_fs, skip_cloud, reuse, sid) =
        read_drive_and_cache(db_path)?;
    eprintln!(
        "  refresh: reusing {} cached entries from snapshot {:?}",
        reuse.len(), sid
    );
    index_drive(&root, db_path, label.as_deref(), do_full,
                one_fs, skip_cloud, Some(reuse), Mode::Refresh, sid)
}

/// Wrapper used by `dx snapshot take`.
pub fn snapshot_drive(db_path: &Path, do_full: bool) -> Result<i64> {
    let (root, label, one_fs, skip_cloud, reuse, prev_sid) =
        read_drive_and_cache(db_path)?;
    eprintln!(
        "  snapshot: reusing {} cached entries from snapshot {:?}",
        reuse.len(), prev_sid
    );
    index_drive(&root, db_path, label.as_deref(), do_full,
                one_fs, skip_cloud, Some(reuse), Mode::Snapshot, None)
}

fn read_drive_and_cache(
    db_path: &Path,
) -> Result<(PathBuf, Option<String>, bool, bool, ReuseCache, Option<i64>)> {
    let conn = db::open_db(db_path)?;
    let drv: (String, Option<String>, Option<i64>, Option<i64>) = conn
        .query_row(
            "SELECT root_path, label, opt_one_fs, opt_skip_cloud FROM drive LIMIT 1",
            [], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )
        .map_err(|_| anyhow!("db has no drive record — run `index` first"))?;
    // the drive may be mounted somewhere else now (other machine / other OS)
    let root = db::resolve_root(&conn, &drv.0);
    if !root.is_dir() {
        anyhow::bail!("root {} not mounted or not a directory", root.display());
    }
    let one_fs = drv.2.map(|v| v != 0).unwrap_or(true);
    let skip_cloud = drv.3.map(|v| v != 0).unwrap_or(true);
    let cache = build_reuse_cache(&conn)?;
    let sid = db::latest_snapshot_id(&conn)?;
    Ok((root, drv.1, one_fs, skip_cloud, cache, sid))
}
