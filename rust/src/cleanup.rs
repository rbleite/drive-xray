//! Shell script generator for assisted cleanup. Mirrors
//! `generate_cleanup_script` in `drive_xray.py`. Output structure matches
//! the Python version (header, KEEP/rm/mv lines, hardlink notes, summary).

use crate::dedupe::duplicate_rows;
use crate::{db, dedupe, util};
use anyhow::{anyhow, Result};
use chrono::Local;
use std::collections::HashMap;
use std::path::Path;

#[derive(Debug, Clone, Copy)]
pub enum Strategy {
    Shortest,
    Oldest,
    Newest,
    Alphabetical,
}

#[derive(Debug, Clone, Copy)]
pub enum Action {
    Delete,
    Quarantine,
}

impl Strategy {
    pub fn parse(s: &str) -> Result<Self> {
        match s {
            "shortest" => Ok(Strategy::Shortest),
            "oldest" => Ok(Strategy::Oldest),
            "newest" => Ok(Strategy::Newest),
            "alphabetical" => Ok(Strategy::Alphabetical),
            o => Err(anyhow!("unknown strategy: {o}")),
        }
    }
    pub fn as_str(&self) -> &'static str {
        match self {
            Strategy::Shortest => "shortest",
            Strategy::Oldest => "oldest",
            Strategy::Newest => "newest",
            Strategy::Alphabetical => "alphabetical",
        }
    }
}

impl Action {
    pub fn parse(s: &str) -> Result<Self> {
        match s {
            "delete" => Ok(Action::Delete),
            "quarantine" => Ok(Action::Quarantine),
            o => Err(anyhow!("unknown action: {o}")),
        }
    }
    pub fn as_str(&self) -> &'static str {
        match self {
            Action::Delete => "delete",
            Action::Quarantine => "quarantine",
        }
    }
}

/// Conservative single-quote shell escape — matches Python `_shell_quote`.
fn shell_quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', "'\\''"))
}

/// Build the cleanup script text. Drive-mounted not required; works off
/// the .db only.
pub fn generate(
    db_path: &Path,
    min_size: i64,
    strategy: Strategy,
    action: Action,
) -> Result<String> {
    let conn = db::open_db(db_path)?;
    let drv: (Option<String>, String) = conn.query_row(
        "SELECT label, root_path FROM drive LIMIT 1",
        [],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    drop(conn);

    // Best-effort warmup if the drive is mounted — matches Python behavior.
    let root = std::path::PathBuf::from(&drv.1);
    if root.is_dir() {
        dedupe::fill_full_hashes(db_path, &root, min_size, None)?;
    }

    let rows = duplicate_rows(db_path, min_size, None)?;
    let label = drv.0.unwrap_or_default();
    let root_path = drv.1;
    let stamp = Local::now().format("%Y%m%dT%H%M%S").to_string();
    let now = Local::now().format("%Y-%m-%dT%H:%M:%S").to_string();

    let mut out = String::new();
    out.push_str("#!/usr/bin/env bash\n");
    out.push_str(&format!("# drive-xray cleanup plan\n"));
    out.push_str(&format!("# Drive label : {label}\n"));
    out.push_str(&format!("# Drive root  : {root_path}\n"));
    out.push_str(&format!("# Generated   : {now}\n"));
    out.push_str(&format!(
        "# Strategy    : keep '{}'   (action: {})\n",
        strategy.as_str(), action.as_str()
    ));
    out.push_str(&format!(
        "# Min size    : {} ({} bytes)\n",
        util::human(min_size as f64), min_size
    ));
    out.push_str("#\n");
    out.push_str("# Review every line. Comment out (#) anything you want to keep.\n");
    out.push_str("# Hardlinks share storage with another path; lines pointing at\n");
    out.push_str("# hardlinked-but-already-kept inodes are commented out.\n");
    out.push_str("#\n");
    out.push_str("# After review, run with:  bash <this-file>\n\n");
    out.push_str("set -euo pipefail\n\n");
    if matches!(action, Action::Quarantine) {
        out.push_str(&format!(
            "QUARANTINE=\"$HOME/.drive-xray-quarantine/{label}-{stamp}\"\n"
        ));
        out.push_str("mkdir -p \"$QUARANTINE\"\n");
        out.push_str("echo \"moving copies to: $QUARANTINE\"\n\n");
    }

    // Group rows by (group_id, hash). Each "physical" (inode, device) gets
    // one representative; we then choose a KEEP rep per group.
    #[derive(Debug)]
    struct Rep {
        rep_path: String,
        size: i64,
        mtime: Option<f64>,
        siblings: Vec<String>, // other rel_paths that share rep's inode
    }
    let mut groups: HashMap<usize, Vec<Rep>> = HashMap::new();
    let mut group_meta: HashMap<usize, (String, i64)> = HashMap::new(); // (hash_hex, size)
    {
        let mut current_group: Option<usize> = None;
        let mut current_inode_reps: HashMap<(i64, i64), Rep> = HashMap::new();
        let mut current_no_inode: Vec<Rep> = Vec::new();

        // duplicate_rows order: by wasted DESC, then group_id ASC, then path.
        // Easier to re-sort by (group_id, path) so each group's rows are
        // contiguous and intra-group is alphabetical.
        let mut rows_sorted = rows.clone();
        rows_sorted.sort_by(|a, b| a.group_id.cmp(&b.group_id).then(a.path.cmp(&b.path)));

        let finalize = |gid: Option<usize>,
                            reps_by_inode: &mut HashMap<(i64, i64), Rep>,
                            no_inode: &mut Vec<Rep>,
                            groups: &mut HashMap<usize, Vec<Rep>>| {
            if let Some(gid) = gid {
                let mut reps: Vec<Rep> = reps_by_inode.drain().map(|(_, r)| r).collect();
                reps.append(no_inode);
                groups.insert(gid, reps);
            }
        };

        for row in rows_sorted {
            if Some(row.group_id) != current_group {
                finalize(current_group, &mut current_inode_reps,
                         &mut current_no_inode, &mut groups);
                current_group = Some(row.group_id);
                current_inode_reps.clear();
                current_no_inode.clear();
                group_meta.insert(row.group_id, (row.hash_hex.clone(), row.size_bytes));
            }
            match (row.inode, row.device) {
                (Some(i), Some(d)) => {
                    let key = (i, d);
                    match current_inode_reps.get_mut(&key) {
                        Some(rep) => rep.siblings.push(row.path.clone()),
                        None => {
                            current_inode_reps.insert(key, Rep {
                                rep_path: row.path.clone(),
                                size: row.size_bytes,
                                mtime: row.mtime,
                                siblings: Vec::new(),
                            });
                        }
                    }
                }
                _ => current_no_inode.push(Rep {
                    rep_path: row.path.clone(),
                    size: row.size_bytes,
                    mtime: row.mtime,
                    siblings: Vec::new(),
                }),
            }
        }
        finalize(current_group, &mut current_inode_reps,
                 &mut current_no_inode, &mut groups);
    }

    let mut total_freeable: i64 = 0;
    let mut n_actions: usize = 0;
    let mut n_hardlink_notes: usize = 0;
    let mut group_ids: Vec<usize> = groups.keys().copied().collect();
    group_ids.sort();
    let mut gnum = 0usize;

    for gid in group_ids {
        let mut reps = groups.remove(&gid).unwrap();
        if reps.len() < 2 {
            // Only hardlinks → no real duplication, skip.
            continue;
        }
        // Sort reps alphabetically by rep_path for deterministic display.
        reps.sort_by(|a, b| a.rep_path.cmp(&b.rep_path));
        gnum += 1;

        // Pick keeper.
        let keeper_idx = match strategy {
            Strategy::Shortest => reps
                .iter().enumerate()
                .min_by_key(|(_, r)| r.rep_path.len())
                .map(|(i, _)| i).unwrap_or(0),
            Strategy::Oldest => reps
                .iter().enumerate()
                .min_by(|(_, a), (_, b)| {
                    let av = a.mtime.unwrap_or(f64::INFINITY);
                    let bv = b.mtime.unwrap_or(f64::INFINITY);
                    av.partial_cmp(&bv).unwrap_or(std::cmp::Ordering::Equal)
                })
                .map(|(i, _)| i).unwrap_or(0),
            Strategy::Newest => reps
                .iter().enumerate()
                .max_by(|(_, a), (_, b)| {
                    let av = a.mtime.unwrap_or(0.0);
                    let bv = b.mtime.unwrap_or(0.0);
                    av.partial_cmp(&bv).unwrap_or(std::cmp::Ordering::Equal)
                })
                .map(|(i, _)| i).unwrap_or(0),
            Strategy::Alphabetical => 0, // reps are already sorted ascending
        };
        let (hash_hex, size) = group_meta.get(&gid).cloned().unwrap();
        out.push_str(&format!(
            "# === Group {}: {} distinct copies of {}  ·  hash={} ===\n",
            gnum, reps.len(), util::human(size as f64), &hash_hex[..12]
        ));
        let keeper_rp = reps[keeper_idx].rep_path.clone();
        out.push_str(&format!("#   KEEP   : {keeper_rp}\n"));
        for sib in &reps[keeper_idx].siblings {
            out.push_str(&format!("#   keep↳hl: {sib}  (hardlink to KEEP)\n"));
            n_hardlink_notes += 1;
        }

        for (i, rep) in reps.iter().enumerate() {
            if i == keeper_idx { continue; }
            let full = shell_quote(&format!("{root_path}/{}", rep.rep_path));
            match action {
                Action::Delete => {
                    out.push_str(&format!(
                        "rm   {full}  # {}\n", util::human(rep.size as f64)
                    ));
                }
                Action::Quarantine => {
                    let safe = rep.rep_path.replace('/', "__").replace(' ', "_");
                    let dst_name = shell_quote(
                        &format!("g{:04}_i{}__{}", gnum, i, safe)
                    );
                    out.push_str(&format!(
                        "mv   {full}  \"$QUARANTINE\"/{dst_name}  # {}\n",
                        util::human(rep.size as f64)
                    ));
                }
            }
            n_actions += 1;
            total_freeable += rep.size;
            for sib_rp in &rep.siblings {
                let sib_full = shell_quote(&format!("{root_path}/{sib_rp}"));
                out.push_str(&format!(
                    "#    ↳ hardlink (same inode, no extra space): {sib_full}\n"
                ));
                n_hardlink_notes += 1;
            }
        }
        out.push('\n');
    }

    out.push_str("# ── Summary ──────────────────────────────────────\n");
    out.push_str(&format!("# Actions     : {n_actions}\n"));
    out.push_str(&format!("# Hardlink notes: {n_hardlink_notes}\n"));
    out.push_str(&format!(
        "# Reclaimable : ~{} ({} bytes)\n",
        util::human(total_freeable as f64), total_freeable
    ));
    Ok(out)
}
