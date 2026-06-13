//! Extension breakdown — top N extensions by total size.
//! Replaces the Python SQLite UDF approach (which called back into Python
//! once per row = millions of Python/C/Python round-trips on large drives).

use crate::db;
use crate::util::human;
use anyhow::Result;
use rusqlite::params;
use serde::Serialize;
use std::collections::HashMap;
use std::path::Path;

#[derive(Debug, Serialize)]
pub struct ExtRow {
    pub ext: String,
    pub files: i64,
    pub size_bytes: i64,
    pub size_human: String,
}

/// Extract extension: everything after the first '.' in the basename.
/// Matches the Python `_ext()` UDF: `file.tar.gz` → `tar.gz`, `file` → `(no ext)`.
fn extract_ext(rel_path: &str) -> &str {
    let basename = rel_path.rsplit('/').next().unwrap_or(rel_path);
    match basename.find('.') {
        Some(pos) => &basename[pos + 1..],
        None => "(no ext)",
    }
}

pub fn extension_breakdown(db_path: &Path, limit: usize) -> Result<Vec<ExtRow>> {
    let conn = db::open_db(db_path)?;
    let sid = match db::latest_snapshot_id(&conn)? {
        Some(s) => s,
        None => return Ok(Vec::new()),
    };

    // Stream all file rows — grouping happens in Rust, no Python UDF callbacks.
    let mut stmt = conn.prepare(
        "SELECT rel_path, COALESCE(size, 0) FROM entries WHERE snapshot_id=? AND is_dir=0",
    )?;
    let rows = stmt.query_map(params![sid], |r| {
        Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?))
    })?;

    let mut counts: HashMap<String, (i64, i64)> = HashMap::new();
    for row in rows {
        let (rel, size) = row?;
        let ext = extract_ext(&rel).to_lowercase();
        let e = counts.entry(ext).or_insert((0, 0));
        e.0 += 1;
        e.1 += size;
    }

    let mut result: Vec<ExtRow> = counts
        .into_iter()
        .map(|(ext, (files, size_bytes))| ExtRow {
            ext,
            files,
            size_bytes,
            size_human: human(size_bytes as f64),
        })
        .collect();
    result.sort_by(|a, b| b.size_bytes.cmp(&a.size_bytes));
    result.truncate(limit);
    Ok(result)
}

pub fn run(db_path: &Path, limit: usize, json: bool) -> Result<()> {
    let rows = extension_breakdown(db_path, limit)?;
    if json {
        println!("{}", serde_json::to_string(&rows)?);
    } else {
        for r in &rows {
            println!("{:>20}  {:>10} files  {:>10}", r.ext, r.files, r.size_human);
        }
    }
    Ok(())
}
