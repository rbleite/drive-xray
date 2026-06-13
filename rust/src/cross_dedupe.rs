//! Cross-drive deduplification across N .db files simultaneously.
//! Mirrors `cross_dedupe()` in `drive_xray.py`.
//!
//! Each db is queried in parallel (rayon). Results are merged into a
//! HashMap keyed by (size, partial_hash). Groups that span ≥2 drives are
//! emitted. When all members share the same full_hash the match is
//! confirmed (=); otherwise it is approximate (≈).

use crate::db;
use crate::util::human;
use anyhow::Result;
use rayon::prelude::*;
use rusqlite::params;
use serde::Serialize;
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Serialize)]
pub struct CrossCopy {
    pub drive: String,
    pub path: String,
}

#[derive(Debug, Serialize)]
pub struct CrossGroup {
    pub size: i64,
    pub confirmed: bool,
    pub wasted_bytes: i64,
    pub copies: Vec<CrossCopy>,
    pub intra_drives: Vec<String>,
}

// (size, partial_hash, rel_path, full_hash, inode, device)
type RawRow = (i64, Vec<u8>, String, Option<Vec<u8>>, Option<i64>, Option<i64>);

fn query_one(db_path: &Path, min_size: i64) -> Result<(String, Vec<RawRow>)> {
    let conn = db::open_db(db_path)?;
    let label: String = conn
        .query_row("SELECT COALESCE(label, '') FROM drive LIMIT 1", [], |r| r.get(0))
        .unwrap_or_else(|_| {
            db_path
                .file_stem()
                .and_then(|s| s.to_str())
                .unwrap_or("")
                .to_string()
        });
    let sid = match db::latest_snapshot_id(&conn)? {
        Some(s) => s,
        None => return Ok((label, Vec::new())),
    };
    let mut stmt = conn.prepare(
        r#"SELECT size, partial_hash, rel_path, full_hash, inode, device
           FROM entries
           WHERE snapshot_id=? AND is_dir=0 AND size>=? AND partial_hash IS NOT NULL"#,
    )?;
    let rows = stmt.query_map(params![sid, min_size], |r| {
        Ok((
            r.get::<_, i64>(0)?,
            r.get::<_, Vec<u8>>(1)?,
            r.get::<_, String>(2)?,
            r.get::<_, Option<Vec<u8>>>(3)?,
            r.get::<_, Option<i64>>(4)?,
            r.get::<_, Option<i64>>(5)?,
        ))
    })?;
    let v: Vec<RawRow> = rows.filter_map(Result::ok).collect();
    Ok((label, v))
}

pub fn cross_dedupe(db_paths: &[PathBuf], min_size: i64) -> Result<Vec<CrossGroup>> {
    // Phase 1: query each db in parallel.
    let per_db: Vec<(String, Vec<RawRow>)> = db_paths
        .par_iter()
        .filter_map(|p| query_one(p, min_size).ok())
        .collect();

    // Phase 2: merge into index with per-drive inode dedup (APFS firmlinks).
    // key = (size, partial_hash) → Vec<(rel_path, full_hash, drive_label)>
    let mut index: HashMap<(i64, Vec<u8>), Vec<(String, Option<Vec<u8>>, String)>> =
        HashMap::new();
    for (label, rows) in &per_db {
        let mut seen_inodes: HashSet<(i64, i64)> = HashSet::new();
        for (size, partial, rel, fh, ino, dev) in rows {
            if let (Some(i), Some(d)) = (ino, dev) {
                if !seen_inodes.insert((*i, *d)) {
                    continue; // APFS firmlink — same physical file, skip
                }
            }
            index
                .entry((*size, partial.clone()))
                .or_default()
                .push((rel.clone(), fh.clone(), label.clone()));
        }
    }

    // Phase 3: build groups — only groups spanning ≥2 drives.
    let mut groups: Vec<CrossGroup> = Vec::new();
    for ((size, _), copies) in &index {
        let drive_set: HashSet<&str> = copies.iter().map(|(_, _, l)| l.as_str()).collect();
        if drive_set.len() < 2 {
            continue;
        }

        let all_have_fh = copies.iter().all(|(_, fh, _)| fh.is_some());
        if all_have_fh {
            // Subgroup by exact full_hash — only cross-drive subgroups matter.
            let mut by_fh: HashMap<&Vec<u8>, Vec<CrossCopy>> = HashMap::new();
            for (rel, fh, lbl) in copies {
                by_fh
                    .entry(fh.as_ref().unwrap())
                    .or_default()
                    .push(CrossCopy { drive: lbl.clone(), path: rel.clone() });
            }
            for (_, sub) in by_fh {
                let sub_drives: HashSet<&str> = sub.iter().map(|c| c.drive.as_str()).collect();
                if sub_drives.len() < 2 {
                    continue;
                }
                let intra_drives = intra_drives_list(&sub);
                let wasted = *size * (sub.len() as i64 - 1);
                groups.push(CrossGroup {
                    size: *size,
                    confirmed: true,
                    wasted_bytes: wasted,
                    copies: sub,
                    intra_drives,
                });
            }
        } else {
            let sub: Vec<CrossCopy> = copies
                .iter()
                .map(|(rel, _, lbl)| CrossCopy { drive: lbl.clone(), path: rel.clone() })
                .collect();
            let intra_drives = intra_drives_list(&sub);
            let wasted = *size * (sub.len() as i64 - 1);
            groups.push(CrossGroup {
                size: *size,
                confirmed: false,
                wasted_bytes: wasted,
                copies: sub,
                intra_drives,
            });
        }
    }

    groups.sort_by(|a, b| b.wasted_bytes.cmp(&a.wasted_bytes));
    Ok(groups)
}

fn intra_drives_list(copies: &[CrossCopy]) -> Vec<String> {
    let mut counts: HashMap<&str, usize> = HashMap::new();
    for c in copies {
        *counts.entry(c.drive.as_str()).or_insert(0) += 1;
    }
    counts
        .into_iter()
        .filter(|(_, n)| *n > 1)
        .map(|(d, _)| d.to_string())
        .collect()
}

/// CLI entry point — called by cli.rs dispatch.
pub fn run(db_paths: &[PathBuf], min_size: i64, json_output: bool) -> Result<()> {
    eprintln!(
        "  cross-dedupe: {} drives, min_size={}",
        db_paths.len(),
        human(min_size as f64)
    );
    let groups = cross_dedupe(db_paths, min_size)?;

    if json_output {
        println!("{}", serde_json::to_string(&groups)?);
        return Ok(());
    }

    // Human-readable fallback (for terminal use).
    let total_wasted: i64 = groups.iter().map(|g| g.wasted_bytes).sum();
    let confirmed = groups.iter().filter(|g| g.confirmed).count();
    println!(
        "Cross-drive duplicates: {} groups ({} confirmed = , {} approximate ≈)",
        groups.len(),
        confirmed,
        groups.len() - confirmed
    );
    println!("Total redundancy: {}", human(total_wasted as f64));
    for (i, g) in groups.iter().enumerate().take(50) {
        let tag = if g.confirmed { "=" } else { "≈" };
        println!(
            "\n[{tag}] #{} — {}×{} = {}",
            i + 1,
            g.copies.len(),
            human(g.size as f64),
            human(g.wasted_bytes as f64)
        );
        for c in &g.copies {
            println!("    [{}] {}", c.drive, c.path);
        }
    }
    if groups.len() > 50 {
        println!("\n  ... {} more groups", groups.len() - 50);
    }
    Ok(())
}
