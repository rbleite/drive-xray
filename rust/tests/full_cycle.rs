//! Full-cycle integration test: exercises every CLI subcommand against
//! one synthetic tree, in the order a real user would touch them.
//!
//! Catches regressions where a subcommand silently breaks after a schema
//! change but isn't covered by the focused per-feature tests.

use drive_xray::index::{index_drive, snapshot_drive, refresh_drive, Mode};
use drive_xray::snapshot::{diff, list, prune, take};
use drive_xray::dedupe::{compute_dir_hashes, dedupe, duplicate_rows, fill_full_hashes};
use drive_xray::compact;
use drive_xray::export;
use drive_xray::cleanup::{generate, Action, Strategy};
use drive_xray::db;

use std::fs;
use std::path::Path;

fn touch(p: &Path, content: &[u8]) {
    if let Some(parent) = p.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(p, content).unwrap();
}

#[test]
fn user_journey_index_snapshot_diff_dedupe_export_cleanup_compact() {
    let td = tempfile::tempdir().unwrap();
    let data = td.path().join("data");
    let db_path = td.path().join("nas.db");

    // -------- t=0: initial tree --------
    fs::create_dir_all(&data).unwrap();
    touch(&data.join("proj/raw/a.bam"), &vec![b'A'; 200_000]);
    touch(&data.join("proj/raw/b.bam"), &vec![b'B'; 200_000]);
    touch(&data.join("backups/a_copy.bam"), &vec![b'A'; 200_000]); // dup of a.bam
    fs::hard_link(
        data.join("proj/raw/a.bam"),
        data.join("proj/raw/a_hl.bam"),
    ).unwrap();
    touch(&data.join("docs/readme.md"), b"# hello\n");

    // -------- INDEX --------
    let sid = index_drive(
        &data, &db_path, Some("nas"),
        false, true, true, None, Mode::Fresh, None,
    ).unwrap();
    assert_eq!(sid, 1);

    // schema sanity
    let conn = db::open_db(&db_path).unwrap();
    let n_paths: i64 = conn.query_row("SELECT COUNT(*) FROM paths", [], |r| r.get(0)).unwrap();
    assert!(n_paths > 0, "paths table populated by Tier 3 interning");
    let n_null_path_id: i64 = conn.query_row(
        "SELECT COUNT(*) FROM entries WHERE path_id IS NULL",
        [], |r| r.get(0),
    ).unwrap();
    assert_eq!(n_null_path_id, 0, "every entry has a path_id");
    drop(conn);

    // -------- DEDUPE --------
    fill_full_hashes(&db_path, &data, 1, None).unwrap();
    compute_dir_hashes(&db_path, None).unwrap();
    let rows = duplicate_rows(&db_path, 1, None).unwrap();
    assert!(!rows.is_empty(), "should find at least one dup group");
    // The dup group has 3 paths but 2 distinct inodes (a + a_hl are linked).
    assert!(rows.iter().any(|r| r.is_hardlink));

    // also smoke the printed CLI form
    dedupe(&db_path, 1).unwrap();

    // -------- EXPORT --------
    let csv = td.path().join("dups.csv");
    let xlsx = td.path().join("dups.xlsx");
    export::export(&db_path, &csv, "csv", 1).unwrap();
    export::export(&db_path, &xlsx, "xlsx", 1).unwrap();
    assert!(fs::metadata(&csv).unwrap().len() > 0);
    assert!(fs::metadata(&xlsx).unwrap().len() > 0);

    // -------- CLEANUP --------
    let plan = generate(&db_path, 1, Strategy::Shortest, Action::Quarantine).unwrap();
    assert!(plan.starts_with("#!/usr/bin/env bash"));
    assert!(plan.contains("KEEP"));
    assert!(plan.contains("mv ") || plan.contains("rm "));

    // -------- SNAPSHOT --------
    std::thread::sleep(std::time::Duration::from_secs(1));
    touch(&data.join("proj/raw/c.bam"), &vec![b'C'; 300_000]); // ADDED
    fs::remove_file(data.join("proj/raw/b.bam")).unwrap();      // REMOVED
    let sid2 = snapshot_drive(&db_path, false).unwrap();
    assert_eq!(sid2, 2);

    let snaps = list(&db_path).unwrap();
    assert_eq!(snaps.len(), 2);

    // -------- DIFF --------
    let d = diff(&db_path, None, None, 10).unwrap();
    assert_eq!(d.added_count, 1);
    assert_eq!(d.removed_count, 1);

    // -------- REFRESH --------
    let sid_after_refresh = refresh_drive(&db_path, false).unwrap();
    assert_eq!(sid_after_refresh, sid2, "refresh overwrites latest snapshot");
    let snaps = list(&db_path).unwrap();
    assert_eq!(snaps.len(), 2, "refresh does NOT add a snapshot");

    // -------- PRUNE --------
    let pruned = prune(&db_path, 1, 0).unwrap();
    assert_eq!(pruned.len(), 1, "keep-last=1 drops one of the two");
    let snaps_after_prune = list(&db_path).unwrap();
    assert_eq!(snaps_after_prune.len(), 1);

    // -------- TAKE (with auto-prune at default 10/12 — should be no-op) --------
    let _sid3 = take(&db_path, false, true, 10, 12).unwrap();
    assert!(list(&db_path).unwrap().len() >= 2);

    // -------- COMPACT --------
    compact::compact(&db_path).unwrap();
    // db still readable after compact:
    let conn = db::open_db(&db_path).unwrap();
    let n_entries: i64 = conn.query_row("SELECT COUNT(*) FROM entries", [], |r| r.get(0)).unwrap();
    assert!(n_entries > 0);
}
