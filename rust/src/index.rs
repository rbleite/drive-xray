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

/// Each phase 2 row: the original walker entry + computed hashes.
struct HashedEntry {
    entry: RawEntry,
    partial: Option<[u8; 16]>,
    full: Option<[u8; 32]>,
    reused: bool,
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

    // -------- phase 0: snapshot allocation / cleanup --------
    let snap_id = allocate_snapshot(&conn, label, one_fs, skip_cloud,
                                     mode, target_snapshot_id)?;

    // -------- phase 1: walk --------
    let t0 = Instant::now();
    let walk_res = walker::walk(&root_canon, one_fs, skip_cloud)?;
    let n_walked = walk_res.entries.len();
    eprintln!("  walk: {} entries in {:.1}s", n_walked, t0.elapsed().as_secs_f64());

    // -------- phase 2: hash (parallel) --------
    let t1 = Instant::now();
    let hashed = hash_phase(
        &root_canon, walk_res.entries, do_full, reuse_old.as_ref(),
    );
    let n_reused = hashed.iter().filter(|h| h.reused).count();
    eprintln!(
        "  hash: {} candidates in {:.1}s ({} reused)",
        hashed.iter().filter(|h| h.partial.is_some()).count(),
        t1.elapsed().as_secs_f64(),
        n_reused,
    );

    // -------- phase 3: write --------
    let t2 = Instant::now();
    let (total_files, total_dirs, total_size) =
        write_phase(db_path, snap_id, hashed)?;
    eprintln!(
        "  write: {} files / {} dirs / {} bytes in {:.1}s",
        total_files, total_dirs, total_size, t2.elapsed().as_secs_f64()
    );

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

fn hash_phase(
    root: &Path,
    entries: Vec<RawEntry>,
    do_full: bool,
    reuse: Option<&ReuseCache>,
) -> Vec<HashedEntry> {
    entries
        .into_par_iter()
        .map(|e| {
            // Dirs / symlinks / errors: nothing to hash.
            if e.is_dir || e.is_symlink || e.error.is_some() {
                return HashedEntry { entry: e, partial: None, full: None, reused: false };
            }
            let size = e.size.unwrap_or(0);
            let mut partial: Option<[u8; 16]> = None;
            let mut full: Option<[u8; 32]> = None;
            let mut reused = false;

            // 1) Try cache (matches on size + mtime within 1s — covers
            //    HFS+ 1s precision vs APFS 1ns).
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

            // 2) Compute partial if missing.
            if partial.is_none() {
                let abs: PathBuf = if e.rel_path == "." {
                    root.to_path_buf()
                } else {
                    root.join(&e.rel_path)
                };
                partial = hash::partial(&abs, size).ok();
            }

            // 3) Compute full only if --full and not already there.
            if do_full && full.is_none() {
                let abs: PathBuf = if e.rel_path == "." {
                    root.to_path_buf()
                } else {
                    root.join(&e.rel_path)
                };
                full = hash::full(&abs).ok();
            }

            HashedEntry { entry: e, partial, full, reused }
        })
        .collect()
}

fn write_phase(
    db_path: &Path,
    snap_id: i64,
    hashed: Vec<HashedEntry>,
) -> Result<(i64, i64, i64)> {
    let mut conn = db::open_db(db_path)?;
    let tx = conn.transaction()?;
    let mut parent_id_by_rel: HashMap<String, i64> = HashMap::new();
    let mut path_id_cache: HashMap<String, i64> = HashMap::new();
    let mut total_files: i64 = 0;
    let mut total_dirs: i64 = 0;
    let mut total_size: i64 = 0;

    {
        let mut stmt = tx.prepare(
            r#"INSERT INTO entries (snapshot_id, rel_path, path_id, parent_id, is_dir, size, mtime, partial_hash, full_hash, is_symlink, error, inode, device) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"#,
        )?;

        for h in hashed.iter() {
            let e = &h.entry;
            let path_id = db::intern_path(&tx, &e.rel_path, &mut path_id_cache)?;
            let parent_id: Option<i64> = e.parent_rel.as_ref()
                .and_then(|p| parent_id_by_rel.get(p).copied());
            let inode = e.inode.map(i64_wrap);
            let device = e.device.map(i64_wrap);
            let size = e.size.map(|s| s as i64);
            let partial = h.partial.as_ref().map(|b| b.to_vec());
            let full = h.full.as_ref().map(|b| b.to_vec());

            stmt.execute(params![
                snap_id, e.rel_path, path_id, parent_id, e.is_dir as i64,
                size, e.mtime, partial, full,
                e.is_symlink as i64, e.error, inode, device,
            ])?;
            let row_id = tx.last_insert_rowid();
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
    Ok((total_files, total_dirs, total_size))
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
    let root = PathBuf::from(&drv.0);
    if !root.is_dir() {
        anyhow::bail!("root {} not mounted or not a directory", root.display());
    }
    let one_fs = drv.2.map(|v| v != 0).unwrap_or(true);
    let skip_cloud = drv.3.map(|v| v != 0).unwrap_or(true);
    let cache = build_reuse_cache(&conn)?;
    let sid = db::latest_snapshot_id(&conn)?;
    Ok((root, drv.1, one_fs, skip_cloud, cache, sid))
}
