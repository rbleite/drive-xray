//! Snapshot operations: take, list, prune (retention policy), diff.
//! Mirrors `snapshot_drive`, `list_snapshots`, `prune_snapshots`,
//! `diff_snapshots`, `print_diff` in `drive_xray.py`.

use crate::db;
use crate::util::human;
use anyhow::{bail, Result};
use chrono::{Datelike, Duration, Local, NaiveDateTime};
use rusqlite::{params_from_iter, Connection};
use std::collections::{HashMap, HashSet};
use std::path::Path;

#[derive(Debug, Clone)]
pub struct Snapshot {
    pub id: i64,
    pub taken_at: String,
    pub label: Option<String>,
    pub total_files: Option<i64>,
    pub total_dirs: Option<i64>,
    pub total_size: Option<i64>,
}

/// Take a new snapshot (preserves history), with optional auto-prune.
/// Returns the new snapshot id.
pub fn take(
    db_path: &Path,
    do_full: bool,
    auto_prune: bool,
    keep_last: usize,
    keep_monthly: usize,
) -> Result<i64> {
    let sid = crate::index::snapshot_drive(db_path, do_full)?;
    if auto_prune {
        let conn = db::open_db(db_path)?;
        let pruned = prune_snapshots(&conn, keep_last, keep_monthly)?;
        if !pruned.is_empty() {
            eprintln!(
                "  prune: dropped {} old snapshot(s) ({:?})",
                pruned.len(),
                pruned
            );
        }
    }
    Ok(sid)
}

/// List all snapshots in the db, most recent first.
pub fn list(db_path: &Path) -> Result<Vec<Snapshot>> {
    let conn = db::open_db(db_path)?;
    list_snapshots(&conn)
}

pub fn list_snapshots(conn: &Connection) -> Result<Vec<Snapshot>> {
    let mut stmt = conn.prepare(
        r#"SELECT id, taken_at, label, total_files, total_dirs, total_size FROM snapshots ORDER BY id DESC"#,
    )?;
    let rows = stmt.query_map([], |r| {
        Ok(Snapshot {
            id: r.get(0)?,
            taken_at: r.get(1)?,
            label: r.get(2)?,
            total_files: r.get(3)?,
            total_dirs: r.get(4)?,
            total_size: r.get(5)?,
        })
    })?;
    let mut out = Vec::new();
    for r in rows {
        out.push(r?);
    }
    Ok(out)
}

/// Apply retention policy. Returns the list of pruned snapshot ids.
pub fn prune(
    db_path: &Path,
    keep_last: usize,
    keep_monthly: usize,
) -> Result<Vec<i64>> {
    let conn = db::open_db(db_path)?;
    prune_snapshots(&conn, keep_last, keep_monthly)
}

/// Mirrors `prune_snapshots` in Python: keep the `keep_last` most recent +
/// one per calendar month for the last `keep_monthly` months.
pub fn prune_snapshots(
    conn: &Connection,
    keep_last: usize,
    keep_monthly: usize,
) -> Result<Vec<i64>> {
    let snaps: Vec<(i64, String)> = {
        let mut stmt = conn.prepare(
            "SELECT id, taken_at FROM snapshots ORDER BY taken_at DESC, id DESC",
        )?;
        let rows = stmt.query_map([], |r| {
            Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?))
        })?;
        let v: Vec<(i64, String)> = rows.filter_map(Result::ok).collect();
        v
    };
    if snaps.is_empty() {
        return Ok(Vec::new());
    }

    let mut keep: HashSet<i64> = HashSet::new();
    // most recent N
    for (sid, _) in snaps.iter().take(keep_last) {
        keep.insert(*sid);
    }
    // monthly samples for the last `keep_monthly` months
    let mut seen_months: HashSet<(i32, u32)> = HashSet::new();
    let cutoff =
        Local::now().naive_local() - Duration::days(31 * keep_monthly as i64);
    for (sid, ts) in snaps.iter().skip(keep_last) {
        let dt = match NaiveDateTime::parse_from_str(ts, "%Y-%m-%dT%H:%M:%S") {
            Ok(d) => d,
            Err(_) => {
                // defensive: keep unparseable timestamps
                keep.insert(*sid);
                continue;
            }
        };
        if dt < cutoff {
            continue;
        }
        let month = (dt.date().year(), dt.date().month());
        if !seen_months.contains(&month) {
            seen_months.insert(month);
            keep.insert(*sid);
        }
    }

    let to_delete: Vec<i64> = snaps
        .iter()
        .filter_map(|(sid, _)| (!keep.contains(sid)).then_some(*sid))
        .collect();
    if !to_delete.is_empty() {
        let placeholders = std::iter::repeat("?")
            .take(to_delete.len())
            .collect::<Vec<_>>()
            .join(",");
        conn.execute(
            &format!("DELETE FROM entries WHERE snapshot_id IN ({placeholders})"),
            params_from_iter(to_delete.iter()),
        )?;
        conn.execute(
            &format!("DELETE FROM snapshots WHERE id IN ({placeholders})"),
            params_from_iter(to_delete.iter()),
        )?;
    }
    Ok(to_delete)
}

// ---------- diff ----------

#[derive(Debug, Default, Clone)]
pub struct DiffResult {
    pub from_snap: Option<Snapshot>,
    pub to_snap: Option<Snapshot>,
    pub added_count: u64,
    pub added_bytes: u64,
    pub removed_count: u64,
    pub removed_bytes: u64,
    pub modified_count: u64,
    pub modified_delta_bytes: i64,
    pub top_growth: Vec<(String, i64)>,
    pub top_shrink: Vec<(String, i64)>,
    pub top_count: Vec<(String, i64)>,
}

pub fn diff(
    db_path: &Path,
    from_id: Option<i64>,
    to_id: Option<i64>,
    top_n: usize,
) -> Result<DiffResult> {
    let conn = db::open_db(db_path)?;
    let snaps = list_snapshots(&conn)?;
    if snaps.is_empty() {
        bail!("no snapshots in db");
    }
    let to_id = to_id.unwrap_or(snaps[0].id);
    let from_id = match from_id {
        Some(f) => f,
        None => {
            if snaps.len() < 2 {
                bail!("need at least 2 snapshots to diff (only 1 found)");
            }
            snaps[1].id
        }
    };
    if from_id == to_id {
        bail!("from and to are the same snapshot");
    }
    let (a, b) = (from_id, to_id);

    // added: in b but not a
    let added: Vec<(String, i64)> = {
        let mut stmt = conn.prepare(
            r#"SELECT b.rel_path, COALESCE(b.size, 0) FROM entries b WHERE b.snapshot_id=? AND b.is_dir=0 AND NOT EXISTS ( SELECT 1 FROM entries a WHERE a.snapshot_id=? AND a.rel_path=b.rel_path AND a.is_dir=0)"#,
        )?;
        let rows = stmt.query_map([b, a], |r| Ok((r.get(0)?, r.get(1)?)))?;
        let v: Vec<(String, i64)> = rows.filter_map(Result::ok).collect();
        v
    };
    // removed: in a but not b
    let removed: Vec<(String, i64)> = {
        let mut stmt = conn.prepare(
            r#"SELECT a.rel_path, COALESCE(a.size, 0) FROM entries a WHERE a.snapshot_id=? AND a.is_dir=0 AND NOT EXISTS ( SELECT 1 FROM entries b WHERE b.snapshot_id=? AND b.rel_path=a.rel_path AND b.is_dir=0)"#,
        )?;
        let rows = stmt.query_map([a, b], |r| Ok((r.get(0)?, r.get(1)?)))?;
        let v: Vec<(String, i64)> = rows.filter_map(Result::ok).collect();
        v
    };
    // modified: same path, different size OR different partial_hash
    let modified: Vec<(String, i64, i64)> = {
        let mut stmt = conn.prepare(
            r#"SELECT b.rel_path, COALESCE(a.size, 0), COALESCE(b.size, 0) FROM entries b JOIN entries a ON a.rel_path=b.rel_path WHERE b.snapshot_id=? AND a.snapshot_id=? AND b.is_dir=0 AND a.is_dir=0 AND (b.size != a.size OR b.partial_hash IS NOT a.partial_hash)"#,
        )?;
        let rows = stmt.query_map([b, a], |r| {
            Ok((r.get(0)?, r.get(1)?, r.get(2)?))
        })?;
        let v: Vec<(String, i64, i64)> = rows.filter_map(Result::ok).collect();
        v
    };

    fn top_key(rel: &str, depth: usize) -> String {
        let parts: Vec<&str> = rel.split('/').collect();
        if parts.len() > depth {
            parts[..depth].join("/")
        } else {
            parts[0].to_string()
        }
    }

    let mut growth: HashMap<String, i64> = HashMap::new();
    let mut addcount: HashMap<String, i64> = HashMap::new();
    for (rp, sz) in &added {
        let k = top_key(rp, 2);
        *growth.entry(k.clone()).or_default() += sz;
        *addcount.entry(k).or_default() += 1;
    }
    for (rp, sz) in &removed {
        let k = top_key(rp, 2);
        *growth.entry(k.clone()).or_default() -= sz;
        *addcount.entry(k).or_default() -= 1;
    }
    for (rp, osz, nsz) in &modified {
        let k = top_key(rp, 2);
        *growth.entry(k).or_default() += nsz - osz;
    }

    fn topn(map: &HashMap<String, i64>, n: usize, ascending: bool) -> Vec<(String, i64)> {
        let mut v: Vec<(String, i64)> =
            map.iter().map(|(k, val)| (k.clone(), *val)).collect();
        if ascending {
            v.sort_by(|a, b| a.1.cmp(&b.1));
        } else {
            v.sort_by(|a, b| b.1.cmp(&a.1));
        }
        v.truncate(n);
        v
    }

    Ok(DiffResult {
        from_snap: snaps.iter().find(|s| s.id == a).cloned(),
        to_snap: snaps.iter().find(|s| s.id == b).cloned(),
        added_count: added.len() as u64,
        added_bytes: added.iter().map(|(_, s)| *s as u64).sum(),
        removed_count: removed.len() as u64,
        removed_bytes: removed.iter().map(|(_, s)| *s as u64).sum(),
        modified_count: modified.len() as u64,
        modified_delta_bytes: modified.iter().map(|(_, o, n)| n - o).sum(),
        top_growth: topn(&growth, top_n, false),
        top_shrink: topn(&growth, top_n, true),
        top_count: topn(&addcount, top_n, false),
    })
}

/// Mirrors `print_diff` in Python: same layout, same Unicode chars (− = U+2212).
pub fn print_diff(d: &DiffResult) {
    let fa = d.from_snap.as_ref().expect("from snapshot resolved");
    let tb = d.to_snap.as_ref().expect("to snapshot resolved");
    println!(
        "\n=== diff: snapshot #{} ({}) → #{} ({}) ===\n",
        fa.id, fa.taken_at, tb.id, tb.taken_at
    );
    println!(
        "  + {:>8} files  +{}",
        d.added_count,
        human(d.added_bytes as f64)
    );
    println!(
        "  − {:>8} files  −{}",
        d.removed_count,
        human(d.removed_bytes as f64)
    );
    let delta = d.modified_delta_bytes;
    let sign = if delta >= 0 { "+" } else { "−" };
    println!(
        "  ~ {:>8} modified  (size Δ {}{})",
        d.modified_count,
        sign,
        human(delta.unsigned_abs() as f64)
    );
    println!("  ─────────────────────────────────");
    let net = d.added_bytes as i64 - d.removed_bytes as i64 + d.modified_delta_bytes;
    let sign = if net >= 0 { "+" } else { "−" };
    println!(
        "  net size change: {}{}",
        sign,
        human(net.unsigned_abs() as f64)
    );

    println!("\nTop folders by growth:");
    for (k, v) in &d.top_growth {
        if *v <= 0 {
            break;
        }
        println!("  +{:>8}   {}/", human(*v as f64), k);
    }
    if d.top_shrink.iter().any(|(_, v)| *v < 0) {
        println!("\nTop folders by shrink:");
        for (k, v) in &d.top_shrink {
            if *v >= 0 {
                break;
            }
            println!("  −{:>8}   {}/", human((-*v) as f64), k);
        }
    }
}
