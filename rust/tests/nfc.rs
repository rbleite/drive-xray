//! Cross-OS Unicode normalization (NFC/NFD) regression tests, mirroring the
//! Python tests/test_nfc.py. macOS walks filenames decomposed (NFD), Windows
//! composed (NFC). Stored rel_path bytes are never normalized (so an I/O path
//! reconstructed from rel_path still opens on exFAT/FAT), but every rel_path
//! *comparison* folds through util::nfc so a Mac↔Windows sync still matches:
//! reuse cache, snapshot diff, and resolve_root fingerprint.

use drive_xray::db;
use drive_xray::index::{index_drive, snapshot_drive, Mode};
use drive_xray::snapshot::diff;
use drive_xray::util;
use rusqlite::Connection;
use std::fs;
use std::path::Path;
use unicode_normalization::UnicodeNormalization;

// "Investigação" — has 'ç' and 'ã', so NFC and NFD differ in bytes.
const WORD_NFC: &str = "Investiga\u{00e7}\u{00e3}o";

fn nfd(s: &str) -> String {
    s.nfd().collect()
}

/// Rewrite stored rel_paths to NFD to simulate a macOS-indexed db. Optionally
/// scoped to one snapshot.
fn flip_stored_to_nfd(db: &Path, snapshot_id: Option<i64>) {
    let conn = Connection::open(db).unwrap();
    let sql = match snapshot_id {
        Some(_) => "SELECT id, rel_path FROM entries WHERE rel_path != '.' AND snapshot_id = ?1",
        None => "SELECT id, rel_path FROM entries WHERE rel_path != '.'",
    };
    let mut stmt = conn.prepare(sql).unwrap();
    let rows: Vec<(i64, String)> = if let Some(sid) = snapshot_id {
        stmt.query_map([sid], |r| Ok((r.get(0)?, r.get(1)?)))
            .unwrap()
            .filter_map(Result::ok)
            .collect()
    } else {
        stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))
            .unwrap()
            .filter_map(Result::ok)
            .collect()
    };
    for (id, rel) in rows {
        let d = nfd(&rel);
        if d != rel {
            conn.execute("UPDATE entries SET rel_path=?1 WHERE id=?2", rusqlite::params![d, id])
                .unwrap();
        }
    }
}

#[test]
fn nfc_folds_nfd_to_nfc() {
    assert_ne!(nfd(WORD_NFC), WORD_NFC); // the forms really differ
    assert_eq!(util::nfc(&nfd(WORD_NFC)), WORD_NFC);
    assert_eq!(util::nfc(WORD_NFC), WORD_NFC); // idempotent on already-NFC
}

#[test]
fn index_keeps_on_disk_bytes() {
    let td = tempfile::tempdir().unwrap();
    let root = td.path().join("vol");
    fs::create_dir_all(root.join(&nfd(WORD_NFC))).unwrap();
    fs::write(root.join(&nfd(WORD_NFC)).join("dados.bin"), vec![0u8; 20_000]).unwrap();
    let dbp = td.path().join("a.db");
    index_drive(&root, &dbp, Some("v"), false, true, true, None, Mode::Fresh, None).unwrap();

    let conn = Connection::open(&dbp).unwrap();
    let mut stmt = conn
        .prepare("SELECT rel_path FROM entries WHERE rel_path != '.'")
        .unwrap();
    let rels: Vec<String> = stmt
        .query_map([], |r| r.get(0))
        .unwrap()
        .filter_map(Result::ok)
        .collect();
    // the decomposed dir name is kept verbatim, not silently recomposed
    assert!(rels.iter().any(|r| r == &nfd(WORD_NFC)), "{rels:?}");
    assert!(!rels.iter().any(|r| r == WORD_NFC), "{rels:?}");
}

#[test]
fn resolve_root_recognizes_accented_volume() {
    let td = tempfile::tempdir().unwrap();
    let vol = td.path().join("vol");
    fs::create_dir_all(vol.join(WORD_NFC)).unwrap(); // on disk: NFC
    fs::write(vol.join(WORD_NFC).join("big.bin"), vec![7u8; 40_000]).unwrap();
    let dbp = td.path().join("c.db");
    index_drive(&vol, &dbp, Some("v"), false, true, true, None, Mode::Fresh, None).unwrap();
    flip_stored_to_nfd(&dbp, None); // db now looks Mac-indexed (NFD)

    let conn = db::open_db(&dbp).unwrap();
    let gone = td.path().join("gone");
    let resolved = db::resolve_root_with(&conn, &gone.to_string_lossy(), Some(vec![vol.clone()]));
    assert_eq!(resolved, vol);
}

#[test]
fn diff_reconciles_nfd_nfc_same_file() {
    let td = tempfile::tempdir().unwrap();
    let vol = td.path().join("vol");
    fs::create_dir_all(vol.join(WORD_NFC)).unwrap();
    fs::write(vol.join(WORD_NFC).join("paper.bin"), vec![9u8; 5_000]).unwrap();
    let dbp = td.path().join("d.db");
    index_drive(&vol, &dbp, Some("v"), false, true, true, None, Mode::Fresh, None).unwrap();
    snapshot_drive(&dbp, false).unwrap(); // second, identical snapshot

    // flip only the older snapshot to NFD → cross-OS skew between the two
    let conn = Connection::open(&dbp).unwrap();
    let s_old: i64 = conn
        .query_row("SELECT MIN(snapshot_id) FROM entries", [], |r| r.get(0))
        .unwrap();
    drop(conn);
    flip_stored_to_nfd(&dbp, Some(s_old));

    let d = diff(&dbp, None, None, 10).unwrap();
    assert_eq!(d.added_count, 0, "added should reconcile to 0");
    assert_eq!(d.removed_count, 0, "removed should reconcile to 0");
}
