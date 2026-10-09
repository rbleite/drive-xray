//! What a Windows index records as its root.
//!
//! `Path::canonicalize` on Windows returns a verbatim path, `\\?\E:\`, not
//! `E:\`, and that string went straight into `drive.root_path`. The Python
//! engine stores `E:\` for the same drive, so the two engines disagreed; the
//! app showed the prefix; and `migrate_windows_seps`, which only recognises a
//! root that starts with a drive letter, skipped these indexes.
//!
//! The first two tests pass trivially on macOS and Linux, where canonicalize
//! never adds a prefix; they exist for the windows-latest leg of
//! `cargo test`, where the first one failed before the fix.

use drive_xray::db;
use drive_xray::index::{index_drive, Mode};

use std::fs;
use std::path::Path;

fn touch(p: &Path, content: &[u8]) {
    fs::create_dir_all(p.parent().unwrap()).unwrap();
    fs::write(p, content).unwrap();
}

fn index_sample() -> (tempfile::TempDir, std::path::PathBuf) {
    let td = tempfile::tempdir().unwrap();
    let data = td.path().join("data");
    touch(&data.join("top.bin"), &vec![b'T'; 5000]);
    touch(&data.join("Films/one.mkv"), &vec![b'A'; 5000]);
    touch(&data.join("Films/deep/two.mkv"), &vec![b'B'; 5000]);
    let db_path = td.path().join("d.db");
    index_drive(&data, &db_path, Some("d"), true, true, true, None,
                Mode::Fresh, None).unwrap();
    (td, db_path)
}

#[test]
fn the_stored_root_is_not_a_verbatim_path() {
    let (_td, db_path) = index_sample();
    let conn = db::open_db(&db_path).unwrap();
    let root: String = conn
        .query_row("SELECT root_path FROM drive", [], |r| r.get(0))
        .unwrap();
    assert!(!root.starts_with(r"\\?\"),
            "root_path stored as a verbatim path: {root}");
}

#[test]
fn files_in_subfolders_are_hashed() {
    // Not a failure that was seen -- Windows did open `\\?\C:\..\data\Films/one.mkv`
    // -- but a file the engine cannot open gets no hash, silently, and is then
    // never found as a duplicate. Stripping the prefix changes how every path
    // below the root is built, so this guards that.
    let (_td, db_path) = index_sample();
    let conn = db::open_db(&db_path).unwrap();
    let mut st = conn
        .prepare("SELECT rel_path, partial_hash IS NOT NULL, full_hash IS NOT NULL
                    FROM entries WHERE is_dir = 0 ORDER BY rel_path")
        .unwrap();
    let rows: Vec<(String, bool, bool)> = st
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
        .unwrap()
        .map(|r| r.unwrap())
        .collect();
    assert_eq!(rows.len(), 3, "{rows:?}");
    let unhashed: Vec<&str> = rows.iter()
        .filter(|(_, p, f)| !p || !f)
        .map(|(rp, _, _)| rp.as_str())
        .collect();
    assert!(unhashed.is_empty(), "never hashed: {unhashed:?}");
}

// ── indexes already written with the prefix ─────────────────────────────────
//
// These run on every OS: the stored string is just text until it is used.

fn db_with_root(root: &str) -> (tempfile::TempDir, std::path::PathBuf) {
    let td = tempfile::tempdir().unwrap();
    let db_path = td.path().join("old.db");
    let conn = db::open_db(&db_path).unwrap();
    conn.execute(
        "INSERT INTO drive (label, root_path, indexed_at, total_files, total_dirs, total_size)
         VALUES ('4TbMyBook', ?1, '2026-01-01T00:00:00', 0, 0, 0)",
        [root],
    ).unwrap();
    drop(conn);
    (td, db_path)
}

fn stored_root(db_path: &Path) -> String {
    db::open_db(db_path).unwrap()
        .query_row("SELECT root_path FROM drive", [], |r| r.get(0))
        .unwrap()
}

#[test]
fn opening_an_old_index_rewrites_its_verbatim_root() {
    let (_td, db_path) = db_with_root(r"\\?\E:\");
    assert_eq!(stored_root(&db_path), r"E:\");
}

#[test]
fn a_unc_root_becomes_a_plain_unc_path() {
    let (_td, db_path) = db_with_root(r"\\?\UNC\nas\media");
    assert_eq!(stored_root(&db_path), r"\\nas\media");
}

#[test]
fn ordinary_roots_are_left_alone() {
    for r in ["/Volumes/4TbMyBook", r"E:\", r"\\nas\media"] {
        let (_td, db_path) = db_with_root(r);
        assert_eq!(stored_root(&db_path), r);
    }
}

#[test]
fn resolve_root_drops_the_prefix_even_without_the_migration() {
    // a reader that opened the file read-only never migrated it
    let (_td, db_path) = db_with_root("/x");
    let conn = db::open_db(&db_path).unwrap();
    let got = db::resolve_root_with(&conn, r"\\?\E:\", Some(vec![]));
    assert_eq!(got.to_string_lossy(), r"E:\");
}
