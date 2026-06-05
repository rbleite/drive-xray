//! End-to-end parity suite. Runs the Python implementation and the Rust
//! `dx` binary against the *same* synthetic tree, then asserts the produced
//! `.db` files match row-for-row (modulo non-deterministic columns like
//! `taken_at` and `indexed_at`).
//!
//! Skipped (via `eprintln!` + early-return) when the Python venv or
//! script is missing, so CI on machines without them still passes.

use rusqlite::Connection;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::Command;

/// Resolve the project's repo root (parent of the `rust/` cargo workspace).
fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("workspace has a parent")
        .to_path_buf()
}

/// Path to the Python interpreter inside the project venv.
fn python() -> Option<PathBuf> {
    let p = repo_root().join(".venv/bin/python");
    p.exists().then_some(p)
}

/// Path to `drive_xray.py`.
fn pyscript() -> PathBuf {
    repo_root().join("drive_xray.py")
}

/// Path to the Rust release binary produced by `cargo build --release`.
fn dx_bin() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("target/release/dx")
}

/// Build the release binary if it isn't already in place.
fn ensure_release_binary() -> PathBuf {
    let bin = dx_bin();
    if bin.exists() {
        return bin;
    }
    let status = Command::new(env!("CARGO"))
        .args(["build", "--release", "--bin", "dx"])
        .current_dir(env!("CARGO_MANIFEST_DIR"))
        .status()
        .expect("cargo build");
    assert!(status.success(), "cargo build --release failed");
    bin
}

/// Skip the test if Python prerequisites are missing.
macro_rules! require_python {
    () => {{
        let py = match python() {
            Some(p) => p,
            None => {
                eprintln!("  (skipping: .venv/bin/python not present)");
                return;
            }
        };
        if !pyscript().exists() {
            eprintln!("  (skipping: drive_xray.py not present)");
            return;
        }
        py
    }};
}

/// Build a deterministic synthetic tree under `root` covering the
/// interesting code paths: duplicates, hardlinks, empties, small/large
/// files, deep nesting, unicode names.
fn build_tree(root: &Path) {
    use std::fs::{create_dir_all, write};
    use std::os::unix::fs::symlink;

    create_dir_all(root.join("proj/raw")).unwrap();
    create_dir_all(root.join("proj/sub/deep")).unwrap();
    create_dir_all(root.join("backups")).unwrap();
    create_dir_all(root.join("docs/Ünîcôdé")).unwrap();

    // Plain files of varied sizes.
    write(root.join("proj/raw/a.txt"), b"alpha-content").unwrap();
    write(root.join("proj/raw/b.txt"), b"beta-content").unwrap();
    write(root.join("proj/sub/deep/c.txt"), b"gamma").unwrap();
    write(root.join("docs/readme.md"), b"# title\n").unwrap();
    write(root.join("docs/Ünîcôdé/é.txt"), "olá mundo".as_bytes()).unwrap();
    write(root.join("backups/empty.bin"), b"").unwrap();

    // Duplicates: same content in 3 distinct paths.
    let dup = b"this content is intentionally duplicated";
    write(root.join("proj/raw/dup1.bin"), dup).unwrap();
    write(root.join("backups/dup_copy.bin"), dup).unwrap();
    write(root.join("docs/dup_third.bin"), dup).unwrap();

    // Hardlink that shares an inode with dup1.bin.
    std::fs::hard_link(
        root.join("proj/raw/dup1.bin"),
        root.join("proj/raw/dup_hl.bin"),
    )
    .unwrap();

    // A larger file that forces the partial-hash head+middle+tail path.
    let big: Vec<u8> = (0u32..)
        .map(|i| (i % 256) as u8)
        .take(200_000)
        .collect();
    write(root.join("proj/big.bin"), &big).unwrap();

    // A symlink (must be recorded as is_symlink=1, size=0).
    let _ = symlink("proj/raw/a.txt", root.join("link-to-a.txt"));
}

/// Open `db` and dump (rel_path, is_dir, size, partial_hex, full_hex,
/// is_symlink, inode_present) for every entry of the latest snapshot.
fn dump_entries(db: &Path) -> HashMap<String, EntrySnapshot> {
    let conn = Connection::open(db).unwrap();
    let sid: i64 = conn
        .query_row("SELECT MAX(id) FROM snapshots", [], |r| r.get(0))
        .expect("snapshot present");
    let mut stmt = conn
        .prepare(
            "SELECT rel_path, is_dir, size, partial_hash, full_hash,\
             is_symlink, inode FROM entries WHERE snapshot_id=?",
        )
        .unwrap();
    let rows = stmt
        .query_map([sid], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, i64>(1)?,
                r.get::<_, Option<i64>>(2)?,
                r.get::<_, Option<Vec<u8>>>(3)?,
                r.get::<_, Option<Vec<u8>>>(4)?,
                r.get::<_, Option<i64>>(5)?,
                r.get::<_, Option<i64>>(6)?,
            ))
        })
        .unwrap();
    let mut out = HashMap::new();
    for row in rows {
        let (rp, is_dir, size, partial, full, is_symlink, inode) = row.unwrap();
        out.insert(
            rp,
            EntrySnapshot {
                is_dir,
                size,
                partial_hex: partial.map(hex::encode),
                full_hex: full.map(hex::encode),
                is_symlink: is_symlink.unwrap_or(0),
                inode_present: inode.is_some(),
            },
        );
    }
    out
}

#[derive(Debug, PartialEq, Eq)]
struct EntrySnapshot {
    is_dir: i64,
    size: Option<i64>,
    partial_hex: Option<String>,
    full_hex: Option<String>,
    is_symlink: i64,
    inode_present: bool,
}

#[test]
fn rust_and_python_produce_equivalent_index() {
    let py = require_python!();
    let dx = ensure_release_binary();

    let td = tempfile::tempdir().unwrap();
    let data = td.path().join("data");
    std::fs::create_dir_all(&data).unwrap();
    build_tree(&data);

    let dbs = td.path().join("dbs");
    std::fs::create_dir_all(&dbs).unwrap();
    let py_db = dbs.join("py.db");
    let rs_db = dbs.join("rs.db");

    // Run both indexers with the same flags.
    let py_status = Command::new(&py)
        .args([
            pyscript().to_str().unwrap(),
            "index",
            data.to_str().unwrap(),
            "--db",
            py_db.to_str().unwrap(),
            "--label",
            "parity",
            "-x",
        ])
        .status()
        .expect("python index");
    assert!(py_status.success(), "python index failed");

    let rs_status = Command::new(&dx)
        .args([
            "index",
            data.to_str().unwrap(),
            "--db",
            rs_db.to_str().unwrap(),
            "--label",
            "parity",
            "-x",
        ])
        .status()
        .expect("rust dx index");
    assert!(rs_status.success(), "dx index failed");

    let py_entries = dump_entries(&py_db);
    let rs_entries = dump_entries(&rs_db);

    // Same set of paths.
    let py_paths: std::collections::BTreeSet<_> = py_entries.keys().collect();
    let rs_paths: std::collections::BTreeSet<_> = rs_entries.keys().collect();
    assert_eq!(
        py_paths, rs_paths,
        "path sets differ:\n  only in py: {:?}\n  only in rs: {:?}",
        py_paths.difference(&rs_paths).collect::<Vec<_>>(),
        rs_paths.difference(&py_paths).collect::<Vec<_>>(),
    );

    // For every path, the deterministic columns must match.
    let mut mismatches = Vec::new();
    for path in py_paths {
        let py = &py_entries[path];
        let rs = &rs_entries[path];
        if py.is_dir != rs.is_dir
            || py.size != rs.size
            || py.partial_hex != rs.partial_hex
            || py.full_hex != rs.full_hex
            || py.is_symlink != rs.is_symlink
        {
            mismatches.push((path.clone(), py, rs));
        }
    }
    assert!(
        mismatches.is_empty(),
        "{} mismatched rows:\n{:#?}",
        mismatches.len(),
        mismatches,
    );

    // inode should be populated for every walked entry on both sides.
    let py_with_inode = py_entries.values().filter(|e| e.inode_present).count();
    let rs_with_inode = rs_entries.values().filter(|e| e.inode_present).count();
    assert_eq!(py_with_inode, rs_with_inode);
}

#[test]
fn rust_db_opens_in_python_dedupe() {
    let py = require_python!();
    let dx = ensure_release_binary();

    let td = tempfile::tempdir().unwrap();
    let data = td.path().join("data");
    std::fs::create_dir_all(&data).unwrap();
    build_tree(&data);
    let db = td.path().join("rs.db");

    let s = Command::new(&dx)
        .args([
            "index",
            data.to_str().unwrap(),
            "--db",
            db.to_str().unwrap(),
            "--label",
            "rs",
            "-x",
        ])
        .status()
        .unwrap();
    assert!(s.success());

    let out = Command::new(&py)
        .args([
            pyscript().to_str().unwrap(),
            "dedupe",
            db.to_str().unwrap(),
            "--min-size",
            "1",
        ])
        .output()
        .unwrap();
    let stdout = String::from_utf8_lossy(&out.stdout);
    assert!(out.status.success(), "python dedupe on rust .db failed");
    // The dup group has 4 paths (3 distinct inodes + 1 hardlink).
    assert!(
        stdout.contains("[4×") && stdout.contains("hardlink"),
        "expected dup group with hardlink tag in stdout:\n{stdout}"
    );
}

#[test]
fn python_db_opens_in_rust_dedupe() {
    let py = require_python!();
    let dx = ensure_release_binary();

    let td = tempfile::tempdir().unwrap();
    let data = td.path().join("data");
    std::fs::create_dir_all(&data).unwrap();
    build_tree(&data);
    let db = td.path().join("py.db");

    let s = Command::new(&py)
        .args([
            pyscript().to_str().unwrap(),
            "index",
            data.to_str().unwrap(),
            "--db",
            db.to_str().unwrap(),
            "--label",
            "py",
            "-x",
        ])
        .status()
        .unwrap();
    assert!(s.success());

    let out = Command::new(&dx)
        .args(["dedupe", db.to_str().unwrap(), "--min-size", "1"])
        .output()
        .unwrap();
    let stdout = String::from_utf8_lossy(&out.stdout);
    assert!(out.status.success(), "rust dedupe on python .db failed");
    assert!(
        stdout.contains("[4×") && stdout.contains("hardlink"),
        "expected dup group with hardlink tag in stdout:\n{stdout}"
    );
}
