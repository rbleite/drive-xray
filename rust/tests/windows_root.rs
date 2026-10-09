//! What a Windows index records as its root, and whether it can read below it.
//!
//! `Path::canonicalize` on Windows returns a verbatim path: `\\?\E:\`, not
//! `E:\`. That string went straight into `drive.root_path`. Verbatim paths
//! switch off Win32 path normalisation -- in particular `/` is no longer a
//! separator -- and every rel_path in the index uses `/`. So
//! `root.join("Films/x.mkv")` became `\\?\E:\Films/x.mkv`, a name Windows
//! will not open.
//!
//! These pass trivially on macOS and Linux, where canonicalize never adds a
//! prefix. They exist for the windows-latest leg of `cargo test`.

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
    // The consequence, not the symptom: a file the engine cannot open gets no
    // hash, silently, and a file with no hash is never found as a duplicate.
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
