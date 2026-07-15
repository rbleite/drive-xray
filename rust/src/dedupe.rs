//! Dedupe operations: fill_full_hashes (rayon over candidates) and
//! compute_dir_hashes (bottom-up Merkle, iterative). Mirrors
//! `fill_full_hashes` and `compute_dir_hashes` in `drive_xray.py`.
//!
//! Also exposes `duplicate_rows()` — the canonical flat-row view used by
//! both `export` (CSV/XLSX) and `cleanup` (script generator).

use crate::db;
use crate::hash;
use crate::util;
use anyhow::Result;
use rayon::prelude::*;
use rusqlite::params;
use std::collections::HashMap;
use std::path::Path;

/// Compute full BLAKE2b for every file that shares (size, partial_hash)
/// with another file in the same snapshot. Returns the count.
pub fn fill_full_hashes(
    db_path: &Path,
    root: &Path,
    min_size: i64,
    snapshot_id: Option<i64>,
) -> Result<usize> {
    let mut conn = db::open_db(db_path)?;
    let sid = match snapshot_id {
        Some(s) => s,
        None => match db::latest_snapshot_id(&conn)? {
            Some(s) => s,
            None => return Ok(0),
        },
    };

    // Candidates: files in groups with COUNT>1 that still have full_hash NULL.
    let candidates: Vec<(i64, String)> = {
        let mut stmt = conn.prepare(
            r#"
            SELECT id, rel_path FROM entries
            WHERE snapshot_id = ?1
              AND is_dir = 0
              AND error IS NULL
              AND size >= ?2
              AND partial_hash IS NOT NULL
              AND full_hash IS NULL
              AND (size, partial_hash) IN (
                SELECT size, partial_hash FROM entries
                WHERE snapshot_id = ?1
                  AND is_dir = 0
                  AND error IS NULL
                  AND size >= ?2
                  AND partial_hash IS NOT NULL
                GROUP BY size, partial_hash HAVING COUNT(*) > 1
              )
            "#,
        )?;
        let rows = stmt.query_map(params![sid, min_size], |r| {
            Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?))
        })?;
        let v: Vec<(i64, String)> = rows.filter_map(Result::ok).collect();
        v
    };
    if candidates.is_empty() {
        return Ok(0);
    }
    eprintln!("  computing full hashes for {} candidate files...", candidates.len());

    // Parallel hashing — the whole reason we ported to Rust.
    let t0 = std::time::Instant::now();
    let hashes: Vec<(i64, Option<Vec<u8>>)> = candidates
        .par_iter()
        .map(|(id, rel)| {
            let abs = root.join(rel);
            let h = hash::full(&abs).ok().map(|b| b.to_vec());
            (*id, h)
        })
        .collect();

    // Single transaction with one prepared statement.
    let tx = conn.transaction()?;
    {
        let mut stmt = tx.prepare("UPDATE entries SET full_hash=? WHERE id=?")?;
        for (id, h) in &hashes {
            stmt.execute(params![h, id])?;
        }
    }
    tx.commit()?;
    eprintln!("    done {} hashes in {:.1}s", candidates.len(), t0.elapsed().as_secs_f64());
    Ok(candidates.len())
}

/// Bottom-up Merkle hash for directories within one snapshot. Mirrors
/// `compute_dir_hashes` in `drive_xray.py` — same input domain
/// (`b"D"|b"F" + name_utf8 + child_full_hash`), same digest_size=32.
pub fn compute_dir_hashes(db_path: &Path, snapshot_id: Option<i64>) -> Result<()> {
    let mut conn = db::open_db(db_path)?;
    let sid = match snapshot_id {
        Some(s) => s,
        None => match db::latest_snapshot_id(&conn)? {
            Some(s) => s,
            None => return Ok(()),
        },
    };

    // Build children map: parent_id → Vec<(name, is_dir, full_hash, my_id)>.
    let mut children: HashMap<i64, Vec<(String, bool, Option<Vec<u8>>, i64)>> =
        HashMap::new();
    {
        let mut stmt = conn.prepare(
            r#"SELECT id, rel_path, parent_id, is_dir, full_hash
               FROM entries WHERE snapshot_id=?"#,
        )?;
        let rows = stmt.query_map([sid], |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, Option<i64>>(2)?,
                r.get::<_, i64>(3)?,
                r.get::<_, Option<Vec<u8>>>(4)?,
            ))
        })?;
        for row in rows {
            let (rid, rel, parent_id, is_dir, fh) = row?;
            if let Some(pid) = parent_id {
                let name = util::basename(&rel).to_string();
                children
                    .entry(pid)
                    .or_default()
                    .push((name, is_dir != 0, fh, rid));
            }
        }
    }

    // Depth-first order: deepest paths first, so each dir sees its
    // descendants already resolved.
    let dir_ids: Vec<i64> = {
        let mut stmt = conn.prepare(
            r#"SELECT id FROM entries WHERE snapshot_id=? AND is_dir=1
               ORDER BY length(rel_path) DESC"#,
        )?;
        let rows = stmt.query_map([sid], |r| r.get::<_, i64>(0))?;
        let v: Vec<i64> = rows.filter_map(Result::ok).collect();
        v
    };

    // resolved: dir_id → optional 32-byte digest. None marks "subtree had
    // an unresolved child" — propagates upward identically to Python.
    let mut resolved: HashMap<i64, Option<[u8; 32]>> = HashMap::new();

    for did in &dir_ids {
        compute_one(*did, &children, &mut resolved);
    }

    // Persist back. We only update the dirs that resolved to Some(_); a
    // None outcome leaves full_hash NULL (same as Python).
    let tx = conn.transaction()?;
    {
        let mut stmt = tx.prepare("UPDATE entries SET full_hash=? WHERE id=?")?;
        for did in &dir_ids {
            let h = resolved.get(did).copied().flatten();
            let blob = h.map(|b| b.to_vec());
            stmt.execute(params![blob, did])?;
        }
    }
    tx.commit()?;
    Ok(())
}

fn compute_one(
    dir_id: i64,
    children: &HashMap<i64, Vec<(String, bool, Option<Vec<u8>>, i64)>>,
    resolved: &mut HashMap<i64, Option<[u8; 32]>>,
) -> Option<[u8; 32]> {
    if let Some(&r) = resolved.get(&dir_id) {
        return r;
    }
    let kids = match children.get(&dir_id) {
        Some(k) if !k.is_empty() => k.clone(),
        _ => {
            resolved.insert(dir_id, None);
            return None;
        }
    };
    let mut sorted = kids;
    sorted.sort_by(|a, b| a.0.cmp(&b.0));

    // Resolve each child's 32-byte digest. Any unresolved → bubble up NULL.
    let mut items: Vec<(String, bool, [u8; 32])> = Vec::with_capacity(sorted.len());
    for (name, is_dir, fh, kid_id) in sorted {
        let digest = if is_dir {
            match compute_one(kid_id, children, resolved) {
                Some(d) => d,
                None => {
                    resolved.insert(dir_id, None);
                    return None;
                }
            }
        } else {
            match fh {
                Some(b) => match <[u8; 32]>::try_from(b.as_slice()) {
                    Ok(arr) => arr,
                    Err(_) => {
                        resolved.insert(dir_id, None);
                        return None;
                    }
                },
                None => {
                    resolved.insert(dir_id, None);
                    return None;
                }
            }
        };
        items.push((name, is_dir, digest));
    }

    let iter = items.iter().map(|(n, d, h)| (n.as_str(), *d, &h[..]));
    let digest = hash::merkle(iter);
    resolved.insert(dir_id, Some(digest));
    Some(digest)
}

// ---------- duplicate_rows: shared helper for export + cleanup ----------

#[derive(Debug, Clone)]
pub struct DupRow {
    pub group_id: usize,
    pub hash_hex: String,
    pub size_bytes: i64,
    pub size_human: String,
    pub group_count: usize,
    pub distinct_inodes: usize,
    pub wasted_bytes: i64,
    pub wasted_human: String,
    pub path: String,
    pub mtime: Option<f64>,
    pub inode: Option<i64>,
    pub device: Option<i64>,
    pub is_hardlink: bool,
}

/// Flat-row representation of all duplicate groups, hardlink-aware.
/// Sort order matches Python: by wasted_bytes DESC, then group_id, then path.
pub fn duplicate_rows(
    db_path: &Path,
    min_size: i64,
    snapshot_id: Option<i64>,
) -> Result<Vec<DupRow>> {
    let conn = db::open_db(db_path)?;
    let sid = match snapshot_id {
        Some(s) => s,
        None => match db::latest_snapshot_id(&conn)? {
            Some(s) => s,
            None => return Ok(Vec::new()),
        },
    };

    let groups: Vec<(Vec<u8>, usize)> = {
        let mut stmt = conn.prepare(
            r#"SELECT full_hash, COUNT(*) FROM entries
               WHERE snapshot_id=? AND is_dir=0 AND full_hash IS NOT NULL
                 AND size >= ?
               GROUP BY full_hash HAVING COUNT(*) > 1"#,
        )?;
        let rows = stmt.query_map(params![sid, min_size], |r| {
            Ok((r.get::<_, Vec<u8>>(0)?, r.get::<_, i64>(1)? as usize))
        })?;
        let v: Vec<(Vec<u8>, usize)> = rows.filter_map(Result::ok).collect();
        v
    };

    let mut rows: Vec<DupRow> = Vec::new();
    for (group_id, (fh, count)) in groups.iter().enumerate() {
        let group_id = group_id + 1;
        let members: Vec<(String, i64, Option<f64>, Option<i64>, Option<i64>)> = {
            let mut stmt = conn.prepare(
                r#"SELECT rel_path, COALESCE(size, 0), mtime, inode, device
                   FROM entries WHERE snapshot_id=? AND full_hash=? AND is_dir=0
                   ORDER BY rel_path"#,
            )?;
            let mr = stmt.query_map(params![sid, fh], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, i64>(1)?,
                    r.get::<_, Option<f64>>(2)?,
                    r.get::<_, Option<i64>>(3)?,
                    r.get::<_, Option<i64>>(4)?,
                ))
            })?;
            let v: Vec<_> = mr.filter_map(Result::ok).collect();
            v
        };
        if members.is_empty() {
            continue;
        }
        let size = members[0].1;

        // distinct (inode, device) pairs — uses 0/None when inode is missing
        // so we don't crash on legacy rows. Matches Python's set logic.
        let mut distinct: std::collections::HashSet<(i64, i64)> =
            std::collections::HashSet::new();
        for (_, _, _, ino, dev) in &members {
            if let (Some(i), Some(d)) = (ino, dev) {
                distinct.insert((*i, *d));
            }
        }
        let d_count = if distinct.is_empty() { *count } else { distinct.len() };
        let wasted = size * (d_count as i64 - 1);
        let hash_hex = hex::encode(fh);

        let mut seen: std::collections::HashSet<(i64, i64)> =
            std::collections::HashSet::new();
        for (rel, _, mtime, ino, dev) in &members {
            let key = match (ino, dev) {
                (Some(i), Some(d)) => Some((*i, *d)),
                _ => None,
            };
            let is_hl = match key {
                Some(k) => !seen.insert(k),
                None => false,
            };
            rows.push(DupRow {
                group_id,
                hash_hex: hash_hex.clone(),
                size_bytes: size,
                size_human: util::human(size as f64),
                group_count: *count,
                distinct_inodes: d_count,
                wasted_bytes: wasted,
                wasted_human: util::human(wasted as f64),
                path: rel.clone(),
                mtime: *mtime,
                inode: *ino,
                device: *dev,
                is_hardlink: is_hl,
            });
        }
    }

    // Sort: largest waste first, then alphabetical within group.
    rows.sort_by(|a, b| {
        b.wasted_bytes
            .cmp(&a.wasted_bytes)
            .then(a.group_id.cmp(&b.group_id))
            .then(a.path.cmp(&b.path))
    });
    Ok(rows)
}

// ---------- CLI dedupe ----------

/// `dx dedupe <db>` — populates hashes and prints duplicate groups.
pub fn dedupe(db_path: &Path, min_size: i64) -> Result<()> {
    let conn = db::open_db(db_path)?;
    let drv: (String, Option<String>) = conn.query_row(
        "SELECT root_path, label FROM drive LIMIT 1",
        [],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    let sid = db::latest_snapshot_id(&conn)?
        .ok_or_else(|| anyhow::anyhow!("no snapshots — run `index` first"))?;
    let root = db::resolve_root(&conn, &drv.0);
    drop(conn);

    if root.is_dir() {
        fill_full_hashes(db_path, &root, min_size, Some(sid))?;
    } else {
        eprintln!(
            "  warning: root {} not mounted — using existing full_hash only",
            root.display()
        );
    }
    compute_dir_hashes(db_path, Some(sid))?;

    let label = drv.1.unwrap_or_default();
    println!("\n=== Duplicate files (drive: {label}, snapshot {sid}) ===");
    let rows = duplicate_rows(db_path, min_size, Some(sid))?;
    let mut total_wasted: i64 = 0;
    let mut total_hardlinks: usize = 0;

    // Group rows by group_id for the printout.
    let mut current_group: Option<usize> = None;
    for row in &rows {
        if Some(row.group_id) != current_group {
            current_group = Some(row.group_id);
            let hl = row.group_count - row.distinct_inodes;
            let hl_tag = if hl > 0 {
                format!(
                    "  ({} hardlink{})",
                    hl,
                    if hl != 1 { "s" } else { "" }
                )
            } else {
                String::new()
            };
            total_wasted += row.wasted_bytes;
            total_hardlinks += hl;
            println!(
                "\n[{}× {}] wasted={}{}  hash={}",
                row.group_count,
                row.size_human,
                row.wasted_human,
                hl_tag,
                &row.hash_hex[..12]
            );
        }
        if row.is_hardlink {
            // first non-hardlink path of same inode would be the "to"; we
            // don't track it here so just tag as hardlink.
            println!("    {}  [↳ hardlink]", row.path);
        } else {
            println!("    {}", row.path);
        }
    }
    if total_hardlinks > 0 {
        println!(
            "\ntotal wasted by duplicate files: {}  (excluding {} hardlinks)",
            util::human(total_wasted as f64),
            total_hardlinks
        );
    } else {
        println!(
            "\ntotal wasted by duplicate files: {}",
            util::human(total_wasted as f64)
        );
    }

    // Duplicate folders.
    let conn = db::open_db(db_path)?;
    let dir_groups: Vec<(Vec<u8>, i64)> = {
        let mut stmt = conn.prepare(
            r#"SELECT full_hash, COUNT(*) FROM entries
               WHERE snapshot_id=? AND is_dir=1 AND full_hash IS NOT NULL
               GROUP BY full_hash HAVING COUNT(*) > 1 ORDER BY COUNT(*) DESC"#,
        )?;
        let rows = stmt.query_map([sid], |r| {
            Ok((r.get::<_, Vec<u8>>(0)?, r.get::<_, i64>(1)?))
        })?;
        let v: Vec<_> = rows.filter_map(Result::ok).collect();
        v
    };
    println!("\n=== Duplicate folders (drive: {label}, snapshot {sid}) ===");
    for (fh, count) in &dir_groups {
        let paths: Vec<String> = {
            let mut stmt = conn.prepare(
                r#"SELECT rel_path FROM entries
                   WHERE snapshot_id=? AND full_hash=? AND is_dir=1"#,
            )?;
            let rows = stmt.query_map(params![sid, fh], |r| r.get::<_, String>(0))?;
            let v: Vec<String> = rows.filter_map(Result::ok).collect();
            v
        };
        println!("\n[{}× identical folder]  hash={}", count, &hex::encode(fh)[..12]);
        for p in paths {
            println!("    {}/", p);
        }
    }
    Ok(())
}
