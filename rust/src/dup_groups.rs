//! Duplicate-group queries — replaces the Python dup_file_groups / dup_folder_groups
//! functions. Processing 556k rows in Python costs ~3-4s; in Rust <200ms.

use crate::db;
use anyhow::Result;
use rusqlite::params;
use serde::Serialize;
use std::collections::HashMap;
use std::path::Path;

// ── output types ──────────────────────────────────────────────────────────────

#[derive(Debug, Serialize)]
pub struct FilePath {
    pub path: String,
    pub hardlink: bool,
}

#[derive(Debug, Serialize)]
pub struct FileGroup {
    pub hash: String,
    pub count: usize,
    pub size: i64,
    pub wasted: i64,
    pub distinct_inodes: usize,
    pub hardlinks: usize,
    pub paths: Vec<FilePath>,
    pub confirmed: bool,
}

#[derive(Debug, Serialize)]
pub struct FolderGroup {
    pub hash: String,
    pub count: usize,
    pub paths: Vec<String>,
}

// ── helpers ───────────────────────────────────────────────────────────────────

fn to_hex(b: &[u8]) -> String {
    b.iter().map(|byte| format!("{byte:02x}")).collect()
}

// ── file duplicates ───────────────────────────────────────────────────────────

pub fn dup_file_groups(db_path: &Path, min_size: i64) -> Result<Vec<FileGroup>> {
    let conn = db::open_db(db_path)?;
    let sid = match db::latest_snapshot_id(&conn)? {
        Some(s) => s,
        None => return Ok(Vec::new()),
    };

    // Single JOIN: fetch every file that belongs to a candidate group
    let mut stmt = conn.prepare(
        "SELECT e.size, e.partial_hash, e.rel_path, e.inode, e.device, e.full_hash \
         FROM entries e \
         JOIN ( \
           SELECT size, partial_hash FROM entries \
           WHERE snapshot_id=? AND is_dir=0 \
             AND partial_hash IS NOT NULL AND size>=? \
           GROUP BY size, partial_hash HAVING COUNT(*)>1 \
         ) c ON e.size=c.size AND e.partial_hash=c.partial_hash \
         WHERE e.snapshot_id=? AND e.is_dir=0",
    )?;

    // (size, partial_hash) → Vec<(rel_path, inode, device, full_hash)>
    type Key = (i64, Vec<u8>);
    let mut by_key: HashMap<Key, Vec<(String, Option<i64>, Option<i64>, Option<Vec<u8>>)>> =
        HashMap::new();

    let rows = stmt.query_map(params![sid, min_size, sid], |r| {
        Ok((
            r.get::<_, i64>(0)?,
            r.get::<_, Vec<u8>>(1)?,
            r.get::<_, String>(2)?,
            r.get::<_, Option<i64>>(3)?,
            r.get::<_, Option<i64>>(4)?,
            r.get::<_, Option<Vec<u8>>>(5)?,
        ))
    })?;

    for row in rows {
        let (size, partial, rel, ino, dev, fh) = row?;
        by_key.entry((size, partial)).or_default().push((rel, ino, dev, fh));
    }

    let mut out: Vec<FileGroup> = Vec::new();

    for ((size, partial), members) in by_key {
        // inode dedup: hardlinks share storage — count only once
        let mut seen_inodes: HashMap<(i64, i64), ()> = HashMap::new();
        let mut deduped: Vec<(String, Option<Vec<u8>>)> = Vec::new();
        let mut hardlink_count = 0usize;

        for (rel, ino, dev, fh) in members {
            if let (Some(i), Some(d)) = (ino, dev) {
                if seen_inodes.insert((i, d), ()).is_some() {
                    hardlink_count += 1;
                    continue;
                }
            }
            deduped.push((rel, fh));
        }

        if deduped.len() < 2 {
            continue;
        }

        let all_have_fh = deduped.iter().all(|(_, fh)| fh.is_some());

        if all_have_fh {
            // sub-group by full_hash
            let mut by_fh: HashMap<Vec<u8>, Vec<String>> = HashMap::new();
            for (rel, fh) in deduped {
                by_fh.entry(fh.unwrap()).or_default().push(rel);
            }
            for (fh, grp_paths) in by_fh {
                if grp_paths.len() < 2 {
                    continue; // partial collision — different content
                }
                let wasted = size * (grp_paths.len() as i64 - 1);
                out.push(FileGroup {
                    hash: to_hex(&fh),
                    count: grp_paths.len(),
                    size,
                    wasted,
                    distinct_inodes: grp_paths.len(),
                    hardlinks: hardlink_count,
                    paths: grp_paths.into_iter().map(|p| FilePath { path: p, hardlink: false }).collect(),
                    confirmed: true,
                });
            }
        } else {
            // approximate: full_hash not yet available
            let wasted = size * (deduped.len() as i64 - 1);
            out.push(FileGroup {
                hash: to_hex(&partial),
                count: deduped.len(),
                size,
                wasted,
                distinct_inodes: deduped.len(),
                hardlinks: hardlink_count,
                paths: deduped.into_iter().map(|(p, _)| FilePath { path: p, hardlink: false }).collect(),
                confirmed: false,
            });
        }
    }

    out.sort_by(|a, b| b.wasted.cmp(&a.wasted));
    Ok(out)
}

// ── folder duplicates ─────────────────────────────────────────────────────────

pub fn dup_folder_groups(db_path: &Path) -> Result<Vec<FolderGroup>> {
    let conn = db::open_db(db_path)?;
    let sid = match db::latest_snapshot_id(&conn)? {
        Some(s) => s,
        None => return Ok(Vec::new()),
    };

    let mut stmt = conn.prepare(
        "SELECT e.full_hash, c.cnt, e.rel_path \
         FROM entries e \
         JOIN ( \
           SELECT full_hash, COUNT(*) cnt FROM entries \
           WHERE snapshot_id=? AND is_dir=1 AND full_hash IS NOT NULL \
           GROUP BY full_hash HAVING cnt > 1 \
         ) c ON e.full_hash = c.full_hash \
         WHERE e.snapshot_id=? AND is_dir=1 \
         ORDER BY c.cnt DESC, e.full_hash",
    )?;

    let rows = stmt.query_map(params![sid, sid], |r| {
        Ok((
            r.get::<_, Vec<u8>>(0)?,
            r.get::<_, usize>(1)?,
            r.get::<_, String>(2)?,
        ))
    })?;

    let mut by_hash: HashMap<Vec<u8>, FolderGroup> = HashMap::new();
    for row in rows {
        let (fh, cnt, rel) = row?;
        let entry = by_hash.entry(fh.clone()).or_insert_with(|| FolderGroup {
            hash: to_hex(&fh),
            count: cnt,
            paths: Vec::new(),
        });
        entry.paths.push(rel);
    }

    // keep insertion order stable — sort by count desc
    let mut out: Vec<FolderGroup> = by_hash.into_values().collect();
    out.sort_by(|a, b| b.count.cmp(&a.count));
    Ok(out)
}

// ── CLI entry points ──────────────────────────────────────────────────────────

pub fn run_files(db_path: &Path, min_size: i64, json: bool) -> Result<()> {
    let groups = dup_file_groups(db_path, min_size)?;
    if json {
        println!("{}", serde_json::to_string(&groups)?);
    } else {
        for g in &groups {
            let mark = if g.confirmed { "=" } else { "≈" };
            println!("{mark} {} copies  {}  wasted {}", g.count, g.size, g.wasted);
            for p in &g.paths {
                println!("    {}", p.path);
            }
        }
    }
    Ok(())
}

pub fn run_folders(db_path: &Path, json: bool) -> Result<()> {
    let groups = dup_folder_groups(db_path)?;
    if json {
        println!("{}", serde_json::to_string(&groups)?);
    } else {
        for g in &groups {
            println!("{} copies  {}", g.count, g.hash);
            for p in &g.paths {
                println!("    {p}");
            }
        }
    }
    Ok(())
}
