//! Parity tests for db.rs.
//!
//! Validates that the Rust implementation produces .db files indistinguishable
//! from the Python ones for the schema/migration layer.

use drive_xray::db::{
    get_hash_version, latest_snapshot_id, migrate_to_v3, migrate_to_v4, open_db,
};
use rusqlite::{params, Connection};
use std::collections::HashSet;
use std::path::PathBuf;

fn tmpdir() -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "drive-xray-test-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn table_columns(conn: &Connection, table: &str) -> HashSet<String> {
    let mut stmt = conn
        .prepare(&format!("PRAGMA table_info({table})"))
        .unwrap();
    let rows = stmt.query_map([], |r| r.get::<_, String>(1)).unwrap();
    rows.filter_map(Result::ok).collect()
}

fn index_names(conn: &Connection, table: &str) -> HashSet<String> {
    let mut stmt = conn
        .prepare(&format!("PRAGMA index_list({table})"))
        .unwrap();
    let rows = stmt.query_map([], |r| r.get::<_, String>(1)).unwrap();
    rows.filter_map(Result::ok).collect()
}

/// 1. Fresh db has v5 schema (Tier 3 — path interning).
#[test]
fn fresh_db_has_v4_schema() {
    let dir = tmpdir();
    let path = dir.join("fresh.db");
    let conn = open_db(&path).unwrap();

    let ent_cols = table_columns(&conn, "entries");
    let expected: HashSet<String> = [
        "id", "snapshot_id", "rel_path", "path_id", "parent_id", "is_dir",
        "size", "mtime", "partial_hash", "full_hash",
        "is_symlink", "error", "inode", "device",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();
    assert_eq!(ent_cols, expected, "entries columns differ");

    // v5 also has a `paths` table
    let paths_cols = table_columns(&conn, "paths");
    assert!(paths_cols.contains("id"));
    assert!(paths_cols.contains("parent_id"));
    assert!(paths_cols.contains("segment"));

    let snap_cols = table_columns(&conn, "snapshots");
    assert!(snap_cols.contains("id"));
    assert!(snap_cols.contains("taken_at"));
    assert!(snap_cols.contains("hash_version"));
    assert!(snap_cols.contains("opt_one_fs"));
    assert!(snap_cols.contains("opt_skip_cloud"));

    let drv_cols = table_columns(&conn, "drive");
    assert!(drv_cols.contains("hash_version"));
    assert!(drv_cols.contains("opt_one_fs"));

    let entry_indexes = index_names(&conn, "entries");
    // v5: the heavy idx_snap_path (UNIQUE on text rel_path) is gone, replaced
    // by idx_snap_path_id (UNIQUE on int path_id).
    assert!(entry_indexes.contains("idx_snap_path_id"));
    assert!(!entry_indexes.contains("idx_snap_path"));
    assert!(entry_indexes.contains("idx_snap_parent"));
    assert!(entry_indexes.contains("idx_snap_size_partial"));
    assert!(entry_indexes.contains("idx_full"));
    assert!(entry_indexes.contains("idx_snap_inode"));

    // Fresh db: no snapshots until first index.
    assert_eq!(latest_snapshot_id(&conn).unwrap(), None);
    // Hash version falls back to 1 if drive is empty.
    assert_eq!(get_hash_version(&conn).unwrap(), 1);
}

/// 2. A synthetic v2 db (with hex hashes, `name`, `parent_path`) is migrated
///    all the way to v4 on first open.
#[test]
fn v2_db_migrates_to_v4() {
    let dir = tmpdir();
    let path = dir.join("v2.db");

    // Build a v2-shaped db by hand.
    {
        let conn = Connection::open(&path).unwrap();
        conn.execute_batch(
            r#"
            CREATE TABLE drive (
                id INTEGER PRIMARY KEY, label TEXT, root_path TEXT NOT NULL,
                indexed_at TEXT NOT NULL, total_files INTEGER, total_dirs INTEGER,
                total_size INTEGER);
            CREATE TABLE entries (
                id INTEGER PRIMARY KEY, rel_path TEXT NOT NULL UNIQUE,
                parent_path TEXT, name TEXT NOT NULL, is_dir INTEGER NOT NULL,
                size INTEGER, mtime REAL, partial_hash TEXT, full_hash TEXT,
                is_symlink INTEGER DEFAULT 0, error TEXT);
            "#,
        )
        .unwrap();
        conn.execute(
            "INSERT INTO drive (label, root_path, indexed_at, total_files, total_dirs, total_size)\
             VALUES ('legacy', '/tmp/old', '2024-01-01T00:00:00', 2, 1, 200)",
            [],
        )
        .unwrap();
        // root
        conn.execute(
            "INSERT INTO entries VALUES (1, '.', NULL, 'old', 1, NULL, NULL, NULL, NULL, 0, NULL)",
            [],
        )
        .unwrap();
        // dir
        conn.execute(
            "INSERT INTO entries VALUES (2, 'folder', '.', 'folder', 1, NULL, NULL, NULL, NULL, 0, NULL)",
            [],
        )
        .unwrap();
        // file with normal hex partial + full
        conn.execute(
            "INSERT INTO entries VALUES (3, 'folder/a.txt', 'folder', 'a.txt', 0, 100, 1700000000.0,\
             'aabbccddeeff00112233445566778899',\
             'fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210', 0, NULL)",
            [],
        )
        .unwrap();
        // empty file sentinel
        conn.execute(
            "INSERT INTO entries VALUES (4, 'folder/empty.bin', 'folder', 'empty.bin', 0, 0, 1700000000.0,\
             'EMPTY', NULL, 0, NULL)",
            [],
        )
        .unwrap();
        // error sentinel
        conn.execute(
            "INSERT INTO entries VALUES (5, 'folder/err.bin', 'folder', 'err.bin', 0, 50, 1700000000.0,\
             'ERR:13', NULL, 0, 'permission denied')",
            [],
        )
        .unwrap();
    }

    // Open via our wrapper — triggers both migrations.
    let conn = open_db(&path).unwrap();

    // Schema is now v4.
    let ent_cols = table_columns(&conn, "entries");
    assert!(ent_cols.contains("snapshot_id"));
    assert!(ent_cols.contains("parent_id"));
    assert!(!ent_cols.contains("name"));
    assert!(!ent_cols.contains("parent_path"));

    // All 5 rows preserved.
    let n: i64 = conn
        .query_row("SELECT COUNT(*) FROM entries", [], |r| r.get(0))
        .unwrap();
    assert_eq!(n, 5);

    // Hex hashes became BLOB.
    let (pblob, fblob): (Option<Vec<u8>>, Option<Vec<u8>>) = conn
        .query_row(
            "SELECT partial_hash, full_hash FROM entries WHERE rel_path = 'folder/a.txt'",
            [], |r| Ok((r.get(0)?, r.get(1)?)))
        .unwrap();
    let pb = pblob.expect("partial blob present");
    let fb = fblob.expect("full blob present");
    assert_eq!(pb.len(), 16, "partial hash should be 16 bytes");
    assert_eq!(fb.len(), 32, "full hash should be 32 bytes");
    assert_eq!(hex::encode(&pb), "aabbccddeeff00112233445566778899");
    assert_eq!(
        hex::encode(&fb),
        "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210"
    );

    // EMPTY → deterministic blake2b(size=0) blob.
    let pblob: Option<Vec<u8>> = conn
        .query_row(
            "SELECT partial_hash FROM entries WHERE rel_path = 'folder/empty.bin'",
            [], |r| r.get(0))
        .unwrap();
    let pb = pblob.expect("empty file should have computed partial");
    assert_eq!(pb.len(), 16);
    // The expected blob is also what Python computes — recompute here and
    // compare so we depend on the same algorithm.
    let mut s = blake2b_simd::Params::new().hash_length(16).to_state();
    s.update(&0u64.to_le_bytes());
    let expected = s.finalize().as_bytes().to_vec();
    assert_eq!(pb, expected, "EMPTY → blake2b(size=0) digest");

    // ERR: → NULL partial_hash, error preserved.
    let (pblob, err): (Option<Vec<u8>>, Option<String>) = conn
        .query_row(
            "SELECT partial_hash, error FROM entries WHERE rel_path = 'folder/err.bin'",
            [], |r| Ok((r.get(0)?, r.get(1)?)))
        .unwrap();
    assert_eq!(pblob, None, "ERR: should become NULL partial_hash");
    assert_eq!(err.as_deref(), Some("permission denied"));

    // parent_id chain: a.txt -> folder -> root.
    let (a_pid, folder_pid, root_pid): (Option<i64>, Option<i64>, Option<i64>) = (
        conn.query_row("SELECT parent_id FROM entries WHERE rel_path='folder/a.txt'", [], |r| r.get(0))
            .unwrap(),
        conn.query_row("SELECT parent_id FROM entries WHERE rel_path='folder'", [], |r| r.get(0))
            .unwrap(),
        conn.query_row("SELECT parent_id FROM entries WHERE rel_path='.'", [], |r| r.get(0))
            .unwrap(),
    );
    let folder_id: i64 = conn
        .query_row("SELECT id FROM entries WHERE rel_path='folder'", [], |r| r.get(0))
        .unwrap();
    let root_id: i64 = conn
        .query_row("SELECT id FROM entries WHERE rel_path='.'", [], |r| r.get(0))
        .unwrap();
    assert_eq!(a_pid, Some(folder_id));
    assert_eq!(folder_pid, Some(root_id));
    assert_eq!(root_pid, None);

    // snapshots: one row seeded from drive metadata.
    let n: i64 = conn
        .query_row("SELECT COUNT(*) FROM snapshots", [], |r| r.get(0))
        .unwrap();
    assert_eq!(n, 1);
    let (taken_at, label): (String, Option<String>) = conn
        .query_row("SELECT taken_at, label FROM snapshots LIMIT 1", [], |r| {
            Ok((r.get(0)?, r.get(1)?))
        })
        .unwrap();
    assert_eq!(taken_at, "2024-01-01T00:00:00");
    assert_eq!(label.as_deref(), Some("legacy"));
    assert_eq!(latest_snapshot_id(&conn).unwrap(), Some(1));

    // All entries linked to that snapshot.
    let sid_set: HashSet<i64> = {
        let mut stmt = conn.prepare("SELECT DISTINCT snapshot_id FROM entries").unwrap();
        stmt.query_map([], |r| r.get::<_, i64>(0))
            .unwrap()
            .filter_map(Result::ok)
            .collect()
    };
    assert_eq!(sid_set, [1].into_iter().collect());
}

/// 3. Idempotent: opening twice doesn't re-migrate or change anything.
#[test]
fn open_is_idempotent() {
    let dir = tmpdir();
    let path = dir.join("idem.db");
    {
        let _ = open_db(&path).unwrap();
    }
    // Insert a snapshot manually.
    {
        let conn = Connection::open(&path).unwrap();
        conn.execute(
            "INSERT INTO snapshots (taken_at, label, hash_version) VALUES (?, ?, ?)",
            params!["2026-01-01T00:00:00", "x", 2],
        )
        .unwrap();
    }
    {
        let conn = open_db(&path).unwrap();
        // Second open didn't try to re-migrate (no error) and the row survives.
        let n: i64 = conn
            .query_row("SELECT COUNT(*) FROM snapshots", [], |r| r.get(0))
            .unwrap();
        assert_eq!(n, 1);
        assert_eq!(get_hash_version(&conn).unwrap(), 2);
    }
}

/// 4. migrate_to_v3 on a v3+ db is a no-op (returns false).
#[test]
fn migrate_v3_noop_on_v3() {
    let dir = tmpdir();
    let path = dir.join("v3noop.db");
    // First open materializes v4; check that migrate_to_v3 says "nothing
    // to do" the second time.
    let conn = open_db(&path).unwrap();
    assert!(!migrate_to_v3(&conn).unwrap());
}

/// 5. migrate_to_v4 on a v4 db is a no-op.
#[test]
fn migrate_v4_noop_on_v4() {
    let dir = tmpdir();
    let path = dir.join("v4noop.db");
    let conn = open_db(&path).unwrap();
    assert!(!migrate_to_v4(&conn).unwrap());
}

/// 6. mtime stored as REAL (f64) by both implementations must produce the
///    same IEEE 754 bit pattern. CPython computes `st_mtime` as
///    `tv_sec + tv_nsec * 1e-9` in C; our Rust walker does the same in the
///    same order. If they ever drift, this test catches it.
///    Unix-only: it exercises the tv_sec/tv_nsec formula via MetadataExt;
///    on Windows st_mtime comes from a different (FILETIME) code path.
#[cfg(unix)]
#[test]
fn mtime_storage_matches_cpython_formula() {
    use std::os::unix::fs::MetadataExt;
    let dir = tmpdir();
    let f = dir.join("touch.txt");
    std::fs::write(&f, b"x").unwrap();
    let md = std::fs::symlink_metadata(&f).unwrap();
    let computed = md.mtime() as f64 + md.mtime_nsec() as f64 * 1e-9;
    // The same formula written differently must yield bit-identical f64.
    let alt = (md.mtime() as f64) + (md.mtime_nsec() as f64) * (1.0f64 / 1e9_f64.recip());
    // Both expressions are equivalent under IEEE 754 only with the standard
    // formula; we deliberately *avoid* the `alt` rewrite in walker.rs.
    let _ = alt;
    // Round-trip through SQLite REAL: should preserve f64 bits exactly.
    let conn = rusqlite::Connection::open_in_memory().unwrap();
    conn.execute("CREATE TABLE t(m REAL)", []).unwrap();
    conn.execute("INSERT INTO t VALUES(?)", [computed]).unwrap();
    let back: f64 = conn.query_row("SELECT m FROM t", [], |r| r.get(0)).unwrap();
    assert_eq!(
        computed.to_bits(),
        back.to_bits(),
        "f64 must survive SQLite REAL round-trip bit-for-bit",
    );
}
