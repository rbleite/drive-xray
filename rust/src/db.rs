//! Schema v4 + migrations v1→v2→v3→v4. Port of `_migrate_to_v3`,
//! `_migrate_to_v4`, `open_db` from `drive_xray.py`.
//!
//! The SQL must stay byte-identical to the Python version so that .db
//! files can be opened by either tool indistinguishably.

use anyhow::{Context, Result};
use blake2b_simd::Params as Blake2bParams;
use chrono::Local;
use rusqlite::{params, Connection, OpenFlags, OptionalExtension};
use std::collections::{HashMap, HashSet};
use std::path::Path;

/// Schema v4 — must match `SCHEMA` constant in `drive_xray.py`.
/// Schema v5 ("Tier 3" — path interning). Adds a `paths` table that holds
/// each directory/file name once with parent pointers; `entries.path_id`
/// references it. The bulky `UNIQUE(snapshot_id, rel_path)` text index is
/// replaced by `UNIQUE(snapshot_id, path_id)` (int+int). Must stay in sync
/// with the `SCHEMA` constant in `drive_xray.py`.
pub const SCHEMA_V5: &str = r#"
CREATE TABLE IF NOT EXISTS drive (
    id INTEGER PRIMARY KEY,
    label TEXT,
    root_path TEXT NOT NULL,
    indexed_at TEXT NOT NULL,
    total_files INTEGER,
    total_dirs INTEGER,
    total_size INTEGER,
    hash_version INTEGER,
    opt_one_fs INTEGER,
    opt_skip_cloud INTEGER
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    taken_at TEXT NOT NULL,
    label TEXT,
    total_files INTEGER,
    total_dirs INTEGER,
    total_size INTEGER,
    hash_version INTEGER,
    opt_one_fs INTEGER,
    opt_skip_cloud INTEGER
);
CREATE TABLE IF NOT EXISTS paths (
    id INTEGER PRIMARY KEY,
    parent_id INTEGER REFERENCES paths(id),
    segment TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_paths_parent_seg ON paths(parent_id, segment);
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    rel_path TEXT NOT NULL,
    path_id INTEGER REFERENCES paths(id),
    parent_id INTEGER REFERENCES entries(id),
    is_dir INTEGER NOT NULL,
    size INTEGER,
    mtime REAL,
    partial_hash BLOB,
    full_hash BLOB,
    is_symlink INTEGER DEFAULT 0,
    error TEXT,
    inode INTEGER,
    device INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_snap_path_id ON entries(snapshot_id, path_id);
CREATE INDEX IF NOT EXISTS idx_snap_parent ON entries(snapshot_id, parent_id);
CREATE INDEX IF NOT EXISTS idx_snap_size_partial ON entries(snapshot_id, size, partial_hash) WHERE is_dir=0;
CREATE INDEX IF NOT EXISTS idx_full ON entries(full_hash);
CREATE INDEX IF NOT EXISTS idx_snap_inode ON entries(snapshot_id, inode, device);
CREATE TABLE IF NOT EXISTS exclusions (rel_path TEXT PRIMARY KEY);
"#;

/// Backwards-compatible alias used elsewhere in the crate.
pub const SCHEMA_V4: &str = SCHEMA_V5;

/// User-configured folder exclusions (rel_path prefixes) for this drive.
pub fn read_exclusions(conn: &Connection) -> Result<Vec<String>> {
    let mut out = Vec::new();
    if let Ok(mut stmt) = conn.prepare("SELECT rel_path FROM exclusions") {
        let rows = stmt.query_map([], |r| r.get::<_, String>(0))?;
        for r in rows {
            out.push(r?);
        }
    }
    Ok(out)
}

/// Open a db at `path`, running v2→v3 and v3→v4 migrations as needed,
/// applying `SCHEMA_V4`, then setting WAL + foreign keys. Idempotent.
pub fn open_db(path: &Path) -> Result<Connection> {
    let conn = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_CREATE
            | OpenFlags::SQLITE_OPEN_URI | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .with_context(|| format!("opening sqlite at {}", path.display()))?;

    // Migrate forward in sequence. Each is a no-op if already at that level.
    migrate_to_v3(&conn)?;
    migrate_to_v4(&conn)?;
    migrate_to_v5(&conn)?;

    // Apply the fresh-db schema (CREATE TABLE / INDEX IF NOT EXISTS).
    conn.execute_batch(SCHEMA_V5)?;

    // Backfill `drive` columns that may be missing on very old .db files.
    let drv_cols: HashSet<String> = column_names(&conn, "drive")?;
    for col in ["hash_version", "opt_one_fs", "opt_skip_cloud"] {
        if !drv_cols.contains(col) {
            conn.execute(&format!("ALTER TABLE drive ADD COLUMN {col} INTEGER"), [])?;
        }
    }

    conn.pragma_update(None, "journal_mode", "WAL")?;
    conn.pragma_update(None, "synchronous", "NORMAL")?;
    conn.pragma_update(None, "foreign_keys", "ON")?;
    // Wait up to 5 s if another writer holds the lock (e.g. Streamlit
    // poking the same db during an `index` subprocess). WAL keeps reads
    // non-blocking; this only matters for two concurrent writers.
    conn.pragma_update(None, "busy_timeout", 5000)?;

    Ok(conn)
}

/// Drop the five `entries` indexes so a bulk delete/insert doesn't pay
/// per-row index maintenance — on a multi-million-row table that dominates
/// runtime (a 4M-row in-place delete went from ~50 min to ~2 s in testing).
/// Rebuild them afterwards with `execute_batch(SCHEMA_V5)`.
///
/// The `paths` index (idx_paths_parent_seg) is intentionally NOT dropped —
/// `intern_path` relies on it during the write phase.
///
/// Crash-safety: `open_db` runs `CREATE INDEX IF NOT EXISTS` on every open,
/// so an interrupted run (indexes dropped, never rebuilt) self-heals on the
/// next open — the db stays correct, just unindexed until then.
pub fn drop_entries_indexes(conn: &Connection) -> Result<()> {
    conn.execute_batch(
        "DROP INDEX IF EXISTS idx_snap_path_id;
         DROP INDEX IF EXISTS idx_snap_parent;
         DROP INDEX IF EXISTS idx_snap_size_partial;
         DROP INDEX IF EXISTS idx_full;
         DROP INDEX IF EXISTS idx_snap_inode;",
    )?;
    Ok(())
}

/// Get the column names of a table as a HashSet for membership checks.
fn column_names(conn: &Connection, table: &str) -> Result<HashSet<String>> {
    let mut stmt = conn.prepare(&format!("PRAGMA table_info({table})"))?;
    let rows = stmt.query_map([], |r| r.get::<_, String>(1))?;
    let mut out = HashSet::new();
    for r in rows {
        out.insert(r?);
    }
    Ok(out)
}

/// Detect if `entries` is still in v2 layout (has `name` or `parent_path`).
/// If so, rebuild it into the v3 layout (BLOB hashes, parent_id, no name).
/// Returns true when a migration ran.
pub fn migrate_to_v3(conn: &Connection) -> Result<bool> {
    let cols = column_names(conn, "entries")?;
    let needs = cols.contains("name") || cols.contains("parent_path");
    if !needs {
        return Ok(false);
    }

    // Empty-file partial hash: BLAKE2b(size=0). Used to convert "EMPTY"
    // string sentinels. Must match the Python `_eh` calculation byte-for-byte.
    let mut empty_state = Blake2bParams::new().hash_length(16).to_state();
    empty_state.update(&0u64.to_le_bytes());
    let empty_blob = empty_state.finalize().as_bytes().to_vec();

    fn hex_to_blob(s: Option<&str>, empty_blob: &[u8]) -> Option<Vec<u8>> {
        let s = s?;
        if s == "EMPTY" {
            return Some(empty_blob.to_vec());
        }
        if s.starts_with("ERR:") {
            return None;
        }
        hex::decode(s).ok()
    }

    eprintln!("  migrating .db schema to v3 (compact paths + BLOB hashes)...");
    let start = std::time::Instant::now();

    // Ensure v2-era columns are present so the SELECT below doesn't fail.
    if !cols.contains("inode") {
        conn.execute("ALTER TABLE entries ADD COLUMN inode INTEGER", [])?;
    }
    if !cols.contains("device") {
        conn.execute("ALTER TABLE entries ADD COLUMN device INTEGER", [])?;
    }

    conn.execute_batch(
        r#"
        DROP TABLE IF EXISTS entries_v3_new;
        CREATE TABLE entries_v3_new (
            id INTEGER PRIMARY KEY,
            rel_path TEXT NOT NULL UNIQUE,
            parent_id INTEGER REFERENCES entries_v3_new(id),
            is_dir INTEGER NOT NULL,
            size INTEGER,
            mtime REAL,
            partial_hash BLOB,
            full_hash BLOB,
            is_symlink INTEGER DEFAULT 0,
            error TEXT,
            inode INTEGER,
            device INTEGER
        );
        "#,
    )?;

    // Pass 1: copy rows with parent_id=NULL (preserving ids), batched.
    let mut select = conn.prepare(
        r#"SELECT id, rel_path, is_dir, size, mtime, partial_hash, full_hash, is_symlink, error, inode, device FROM entries"#,
    )?;
    let mut insert = conn.prepare(
        r#"INSERT INTO entries_v3_new (id, rel_path, parent_id, is_dir, size, mtime, partial_hash, full_hash, is_symlink, error, inode, device) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)"#,
    )?;

    struct OldRow {
        id: i64,
        rel_path: String,
        is_dir: i64,
        size: Option<i64>,
        mtime: Option<f64>,
        partial: Option<String>,
        full: Option<String>,
        is_symlink: Option<i64>,
        error: Option<String>,
        inode: Option<i64>,
        device: Option<i64>,
    }

    let rows = select.query_map([], |r| {
        Ok(OldRow {
            id: r.get(0)?,
            rel_path: r.get(1)?,
            is_dir: r.get(2)?,
            size: r.get(3)?,
            mtime: r.get(4)?,
            partial: r.get(5)?,
            full: r.get(6)?,
            is_symlink: r.get(7)?,
            error: r.get(8)?,
            inode: r.get(9)?,
            device: r.get(10)?,
        })
    })?;

    let mut n: i64 = 0;
    for row in rows {
        let r = row?;
        let pblob = hex_to_blob(r.partial.as_deref(), &empty_blob);
        let fblob = hex_to_blob(r.full.as_deref(), &empty_blob);
        insert.execute(params![
            r.id, r.rel_path, r.is_dir, r.size, r.mtime,
            pblob, fblob, r.is_symlink.unwrap_or(0), r.error, r.inode, r.device,
        ])?;
        n += 1;
    }
    drop(insert);
    drop(select);

    // Pass 2: link parents. parent_path stored the rel_path of the parent
    // (literal "." for root's children). Look up by rel_path → id.
    let mut rel_to_id: HashMap<String, i64> = HashMap::new();
    {
        let mut stmt = conn.prepare("SELECT rel_path, id FROM entries_v3_new")?;
        for row in stmt.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)))? {
            let (rp, id) = row?;
            rel_to_id.insert(rp, id);
        }
    }
    let mut update = conn.prepare("UPDATE entries_v3_new SET parent_id=? WHERE id=?")?;
    let mut sel_pp = conn.prepare(
        "SELECT id, parent_path FROM entries WHERE parent_path IS NOT NULL",
    )?;
    for row in sel_pp.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)))? {
        let (oid, pp) = row?;
        if let Some(pid) = rel_to_id.get(&pp) {
            update.execute(params![pid, oid])?;
        }
    }
    drop(update);
    drop(sel_pp);

    conn.execute_batch(
        r#"
        DROP TABLE entries;
        ALTER TABLE entries_v3_new RENAME TO entries;
        CREATE INDEX IF NOT EXISTS idx_size_partial ON entries(size, partial_hash) WHERE is_dir=0;
        CREATE INDEX IF NOT EXISTS idx_full ON entries(full_hash);
        CREATE INDEX IF NOT EXISTS idx_parent ON entries(parent_id);
        CREATE INDEX IF NOT EXISTS idx_inode ON entries(inode, device);
        "#,
    )?;

    eprintln!("    migrated {n} rows in {:.1}s", start.elapsed().as_secs_f64());
    Ok(true)
}

/// Detect if `entries` is still in v3 layout (no `snapshot_id`). If so, seed
/// snapshot 1 from the drive row and rebuild entries with snapshot_id +
/// composite UNIQUE(snapshot_id, rel_path).
pub fn migrate_to_v4(conn: &Connection) -> Result<bool> {
    let cols = column_names(conn, "entries")?;
    if cols.is_empty() {
        return Ok(false); // fresh db, no entries table yet
    }
    if cols.contains("snapshot_id") {
        return Ok(false); // already v4
    }

    // Ensure snapshots table exists (idempotent).
    conn.execute_batch(
        r#"
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY,
            taken_at TEXT NOT NULL,
            label TEXT,
            total_files INTEGER,
            total_dirs INTEGER,
            total_size INTEGER,
            hash_version INTEGER,
            opt_one_fs INTEGER,
            opt_skip_cloud INTEGER
        );
        "#,
    )?;

    // Seed an initial snapshot from the drive row (if any). Build the SELECT
    // dynamically so it tolerates v2 drive tables that lack hash_version /
    // opt_one_fs / opt_skip_cloud (those columns get added later in open_db).
    let drv_cols = column_names(conn, "drive")?;
    let has_hv = drv_cols.contains("hash_version");
    let has_ofs = drv_cols.contains("opt_one_fs");
    let has_scl = drv_cols.contains("opt_skip_cloud");

    let mut select_cols = String::from(
        "label, indexed_at, total_files, total_dirs, total_size"
    );
    select_cols.push_str(if has_hv { ", hash_version" } else { ", NULL" });
    select_cols.push_str(if has_ofs { ", opt_one_fs" } else { ", NULL" });
    select_cols.push_str(if has_scl { ", opt_skip_cloud" } else { ", NULL" });
    let drv_sql = format!("SELECT {select_cols} FROM drive LIMIT 1");

    let drv: Option<(Option<String>, Option<String>, Option<i64>, Option<i64>,
                     Option<i64>, Option<i64>, Option<i64>, Option<i64>)> = conn
        .query_row(&drv_sql, [], |r| {
            Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?,
                r.get(5)?, r.get(6)?, r.get(7)?))
        })
        .ok();

    match drv {
        Some((label, Some(taken_at), tf, td, ts, hv, ofs, scl)) => {
            conn.execute(
                r#"INSERT INTO snapshots (taken_at, label, total_files, total_dirs, total_size, hash_version, opt_one_fs, opt_skip_cloud) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"#,
                params![taken_at, label, tf, td, ts, hv, ofs, scl],
            )?;
        }
        _ => {
            // No drive row at all, or NULL indexed_at — synthesize.
            let ts = Local::now().format("%Y-%m-%dT%H:%M:%S").to_string();
            conn.execute(
                r#"INSERT INTO snapshots (taken_at, label, hash_version) VALUES (?, ?, ?)"#,
                params![ts, "(unknown)", 1i64],
            )?;
        }
    }
    let initial_sid: i64 = conn
        .query_row("SELECT id FROM snapshots ORDER BY id DESC LIMIT 1", [], |r| r.get(0))?;

    eprintln!(
        "  migrating .db schema to v4 (snapshot support, initial sid={initial_sid})..."
    );
    let start = std::time::Instant::now();

    conn.execute_batch(
        r#"
        DROP TABLE IF EXISTS entries_v4_new;
        CREATE TABLE entries_v4_new (
            id INTEGER PRIMARY KEY,
            snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
            rel_path TEXT NOT NULL,
            parent_id INTEGER REFERENCES entries_v4_new(id),
            is_dir INTEGER NOT NULL,
            size INTEGER,
            mtime REAL,
            partial_hash BLOB,
            full_hash BLOB,
            is_symlink INTEGER DEFAULT 0,
            error TEXT,
            inode INTEGER,
            device INTEGER
        );
        "#,
    )?;

    // Debug aid: dump source schema if INSERT fails.
    let dbg_cols: Vec<String> = column_names(conn, "entries")?.into_iter().collect();
    let dbg_v4_cols: Vec<String> =
        column_names(conn, "entries_v4_new")?.into_iter().collect();
    let n = conn.execute(
        r#"INSERT INTO entries_v4_new
              (id, snapshot_id, rel_path, parent_id, is_dir, size, mtime,
               partial_hash, full_hash, is_symlink, error, inode, device)
           SELECT id, ?, rel_path, parent_id, is_dir, size, mtime,
                  partial_hash, full_hash, is_symlink, error, inode, device
           FROM entries"#,
        params![initial_sid],
    )
    .with_context(|| format!(
        "v4 INSERT failed; entries cols: {dbg_cols:?}; entries_v4_new cols: {dbg_v4_cols:?}"
    ))?;

    conn.execute_batch(
        r#"
        DROP TABLE entries;
        ALTER TABLE entries_v4_new RENAME TO entries;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_snap_path ON entries(snapshot_id, rel_path);
        CREATE INDEX IF NOT EXISTS idx_snap_parent ON entries(snapshot_id, parent_id);
        CREATE INDEX IF NOT EXISTS idx_snap_size_partial ON entries(snapshot_id, size, partial_hash) WHERE is_dir=0;
        CREATE INDEX IF NOT EXISTS idx_full ON entries(full_hash);
        CREATE INDEX IF NOT EXISTS idx_snap_inode ON entries(snapshot_id, inode, device);
        "#,
    )?;

    eprintln!("    migrated {n} rows in {:.1}s", start.elapsed().as_secs_f64());
    Ok(true)
}

/// v4 → v5 path interning migration. Drops the heavy `idx_snap_path` UNIQUE
/// (which indexed the full text path) and replaces it with
/// `idx_snap_path_id` UNIQUE (int+int), after populating `paths` and
/// `entries.path_id` from existing `rel_path` strings.
///
/// Idempotent: short-circuits when `path_id` is already populated for
/// every row AND the old `idx_snap_path` index is gone.
pub fn migrate_to_v5(conn: &Connection) -> Result<bool> {
    let cols = column_names(conn, "entries")?;
    if cols.is_empty() {
        return Ok(false); // fresh db
    }
    if !cols.contains("snapshot_id") {
        return Ok(false); // not v4 yet
    }
    let has_path_id = cols.contains("path_id");
    let has_old_idx = !conn
        .prepare(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_snap_path'",
        )?
        .query_map([], |r| r.get::<_, String>(0))?
        .filter_map(Result::ok)
        .collect::<Vec<_>>()
        .is_empty();

    // We also have to handle the case where path_id exists but some rows
    // are still NULL (e.g. an older binary inserted without interning).
    let has_unfilled = if has_path_id {
        conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM entries WHERE path_id IS NULL)",
            [],
            |r| r.get::<_, i64>(0),
        )? != 0
    } else {
        true
    };

    if has_path_id && !has_old_idx && !has_unfilled {
        return Ok(false); // already v5
    }

    eprintln!("  migrating .db schema to v5 (path interning)...");
    let t0 = std::time::Instant::now();

    // Ensure paths table + unique index exist (idempotent).
    conn.execute_batch(
        r#"
        CREATE TABLE IF NOT EXISTS paths (
            id INTEGER PRIMARY KEY,
            parent_id INTEGER REFERENCES paths(id),
            segment TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_paths_parent_seg ON paths(parent_id, segment);
        "#,
    )?;
    if !has_path_id {
        conn.execute("ALTER TABLE entries ADD COLUMN path_id INTEGER", [])?;
    }

    // Walk every unfilled entry once, interning its rel_path on the fly.
    let mut cache: HashMap<String, i64> = HashMap::new();
    let rows: Vec<(i64, String)> = {
        let mut stmt = conn.prepare(
            "SELECT id, rel_path FROM entries WHERE path_id IS NULL",
        )?;
        let mapped = stmt.query_map([], |r| {
            Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?))
        })?;
        let v: Vec<(i64, String)> = mapped.filter_map(Result::ok).collect();
        v
    };

    let mut n: i64 = 0;
    for (eid, rp) in &rows {
        let pid = intern_path_inner(conn, rp, &mut cache)?;
        conn.execute(
            "UPDATE entries SET path_id=? WHERE id=?",
            rusqlite::params![pid, eid],
        )?;
        n += 1;
    }

    // Replace the heavy text-based UNIQUE index with the int one.
    conn.execute_batch(
        r#"
        DROP INDEX IF EXISTS idx_snap_path;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_snap_path_id ON entries(snapshot_id, path_id);
        "#,
    )?;

    eprintln!(
        "    interned {n} entries in {:.1}s",
        t0.elapsed().as_secs_f64()
    );
    Ok(true)
}

/// Internal recursive interner. Inserts a chain of `paths` rows for
/// `rel_path`, reusing existing ones via the `(parent_id, segment)`
/// UNIQUE index. `cache` is per-call (a single walk).
pub(crate) fn intern_path_inner(
    conn: &Connection,
    rel_path: &str,
    cache: &mut HashMap<String, i64>,
) -> Result<i64> {
    if let Some(&id) = cache.get(rel_path) {
        return Ok(id);
    }
    // Root: parent NULL, segment "."
    if rel_path == "." || rel_path.is_empty() {
        let pid: i64 = match conn
            .query_row(
                "SELECT id FROM paths WHERE parent_id IS NULL AND segment='.'",
                [],
                |r| r.get(0),
            )
            .optional()?
        {
            Some(id) => id,
            None => {
                conn.execute(
                    "INSERT INTO paths (parent_id, segment) VALUES (NULL, '.')",
                    [],
                )?;
                conn.last_insert_rowid()
            }
        };
        cache.insert(".".to_string(), pid);
        cache.insert(String::new(), pid);
        return Ok(pid);
    }

    let parts: Vec<&str> = rel_path.split('/').collect();
    let mut parent_id = intern_path_inner(conn, ".", cache)?;
    for (i, seg) in parts.iter().enumerate() {
        let key: String = parts[..=i].join("/");
        if let Some(&id) = cache.get(&key) {
            parent_id = id;
            continue;
        }
        // Insert; on UNIQUE conflict the existing row's id is returned.
        let pid: i64 = match conn.execute(
            "INSERT INTO paths (parent_id, segment) VALUES (?, ?)",
            rusqlite::params![parent_id, seg],
        ) {
            Ok(_) => conn.last_insert_rowid(),
            Err(rusqlite::Error::SqliteFailure(_, _)) => conn.query_row(
                "SELECT id FROM paths WHERE parent_id=? AND segment=?",
                rusqlite::params![parent_id, seg],
                |r| r.get(0),
            )?,
            Err(e) => return Err(e.into()),
        };
        cache.insert(key, pid);
        parent_id = pid;
    }
    Ok(parent_id)
}

/// Public re-export so `index.rs` can intern paths during a fresh walk
/// without going through the migration code path.
pub fn intern_path(
    conn: &Connection,
    rel_path: &str,
    cache: &mut HashMap<String, i64>,
) -> Result<i64> {
    intern_path_inner(conn, rel_path, cache)
}

/// Latest snapshot id, or None if there are no snapshots yet.
pub fn latest_snapshot_id(conn: &Connection) -> Result<Option<i64>> {
    let id: Option<i64> = conn
        .query_row("SELECT MAX(id) FROM snapshots", [], |r| r.get(0))
        .unwrap_or(None);
    Ok(id)
}

/// hash_version from the latest snapshot, or `drive.hash_version` as a
/// fallback for legacy .db files. Defaults to 1.
pub fn get_hash_version(conn: &Connection) -> Result<i64> {
    let v: Option<i64> = conn
        .query_row(
            "SELECT hash_version FROM snapshots ORDER BY id DESC LIMIT 1",
            [], |r| r.get(0))
        .ok().flatten();
    if let Some(v) = v { return Ok(v); }
    let v: Option<i64> = conn
        .query_row("SELECT hash_version FROM drive LIMIT 1", [], |r| r.get(0))
        .ok().flatten();
    Ok(v.unwrap_or(1))
}
