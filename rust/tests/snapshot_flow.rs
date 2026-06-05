//! Integration tests for the snapshot family: take, list, prune, diff.
//! Each test sets up a synthetic tree, exercises the full pipeline via
//! the library API, and asserts on the .db state.

use drive_xray::db::{self, latest_snapshot_id};
use drive_xray::index::{index_drive, refresh_drive, snapshot_drive, Mode};
use drive_xray::snapshot::{diff, list, prune, take};
use std::fs;
use std::path::Path;

fn write_at(root: &Path, rel: &str, content: &[u8]) {
    let full = root.join(rel);
    if let Some(p) = full.parent() {
        fs::create_dir_all(p).unwrap();
    }
    fs::write(full, content).unwrap();
}

/// Create initial snapshot via `index_drive`. Returns the db path.
fn make_initial(td: &Path) -> std::path::PathBuf {
    let root = td.join("data");
    fs::create_dir_all(&root).unwrap();
    write_at(&root, "proj/a.txt", b"alpha");
    write_at(&root, "proj/b.txt", b"beta");
    write_at(&root, "backups/old.tar", b"binary-ish payload here");
    let db = td.join("test.db");
    index_drive(
        &root, &db, Some("test"), false, true, true,
        None, Mode::Fresh, None,
    )
    .unwrap();
    db
}

#[test]
fn take_and_list() {
    let td = tempfile::tempdir().unwrap();
    let db = make_initial(td.path());

    let snaps = list(&db).unwrap();
    assert_eq!(snaps.len(), 1);
    assert_eq!(snaps[0].label.as_deref(), Some("test"));
    assert!(snaps[0].total_files.unwrap() >= 3);

    // sleep so the second snapshot's taken_at differs by ≥1s
    std::thread::sleep(std::time::Duration::from_secs(1));
    let root = td.path().join("data");
    write_at(&root, "proj/c.txt", b"gamma");
    let sid2 = snapshot_drive(&db, false).unwrap();
    assert_eq!(sid2, 2);

    let snaps = list(&db).unwrap();
    assert_eq!(snaps.len(), 2);
    // most recent first
    assert_eq!(snaps[0].id, 2);
    assert_eq!(snaps[1].id, 1);
    // snapshot 2 has more files than snapshot 1
    assert!(snaps[0].total_files.unwrap() > snaps[1].total_files.unwrap());
}

#[test]
fn refresh_overwrites_latest_snapshot() {
    let td = tempfile::tempdir().unwrap();
    let db = make_initial(td.path());
    let conn = db::open_db(&db).unwrap();
    let original_sid = latest_snapshot_id(&conn).unwrap();
    drop(conn);

    // Make a change and refresh.
    let root = td.path().join("data");
    write_at(&root, "proj/new.txt", b"introduced after refresh");
    let sid_after = refresh_drive(&db, false).unwrap();

    // Still the same snapshot id — refresh overwrites in place.
    assert_eq!(sid_after, original_sid.unwrap());
    let snaps = list(&db).unwrap();
    assert_eq!(snaps.len(), 1);
    assert!(snaps[0].total_files.unwrap() >= 4); // saw the new file
}

#[test]
fn diff_added_removed_modified() {
    let td = tempfile::tempdir().unwrap();
    let db = make_initial(td.path());
    let root = td.path().join("data");

    std::thread::sleep(std::time::Duration::from_secs(1));
    // make changes between snapshots:
    write_at(&root, "proj/c.txt", b"gamma");                  // ADDED
    fs::remove_file(root.join("proj/b.txt")).unwrap();        // REMOVED
    write_at(&root, "proj/a.txt", b"alpha-changed-longer");   // MODIFIED
    let _sid2 = snapshot_drive(&db, false).unwrap();

    let d = diff(&db, None, None, 10).unwrap();
    assert!(d.from_snap.is_some());
    assert!(d.to_snap.is_some());
    assert_eq!(d.added_count, 1, "exactly one added file");
    assert_eq!(d.removed_count, 1, "exactly one removed file");
    assert_eq!(d.modified_count, 1, "exactly one modified file");

    // Modified delta: alpha (5 bytes) → alpha-changed-longer (20 bytes) = +15
    assert_eq!(d.modified_delta_bytes, 15);

    // Top growth: proj/ should be the only positive bucket.
    let proj = d.top_growth.iter().find(|(k, _)| k == "proj");
    assert!(proj.is_some());
    // net for proj = +5 (c.txt) − 4 (b.txt "beta") + 15 (modified) = +16
    assert_eq!(proj.unwrap().1, 16);
}

#[test]
fn prune_keeps_last_n() {
    let td = tempfile::tempdir().unwrap();
    let db = make_initial(td.path());
    let root = td.path().join("data");

    for i in 0..4 {
        std::thread::sleep(std::time::Duration::from_secs(1));
        write_at(&root, &format!("proj/f{i}.txt"), format!("file {i}").as_bytes());
        snapshot_drive(&db, false).unwrap();
    }
    // We now have 5 snapshots total (initial + 4).
    let snaps = list(&db).unwrap();
    assert_eq!(snaps.len(), 5);

    // Prune with keep_last=2, keep_monthly=0 — only the 2 newest survive.
    let pruned = prune(&db, 2, 0).unwrap();
    assert_eq!(pruned.len(), 3);
    let remaining = list(&db).unwrap();
    assert_eq!(remaining.len(), 2);
    // ids 4 and 5 should survive (they are the most recent).
    let ids: Vec<i64> = remaining.iter().map(|s| s.id).collect();
    assert!(ids.contains(&5));
    assert!(ids.contains(&4));
}

#[test]
fn diff_errors_with_one_snapshot() {
    let td = tempfile::tempdir().unwrap();
    let db = make_initial(td.path());
    let err = diff(&db, None, None, 10).unwrap_err();
    let msg = format!("{err}");
    assert!(
        msg.contains("at least 2 snapshots") || msg.contains("only 1 found"),
        "got: {msg}"
    );
}
