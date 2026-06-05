//! Cross-db compare. Mirrors `compare()` in `drive_xray.py`.
//!
//! Builds an in-memory index of B keyed by (size, partial_hash), then
//! streams A and reports matches. When both sides have a full_hash, the
//! match is "confirmed"; otherwise it's "≈" (likely).

use crate::db;
use crate::util::human;
use crate::HASH_VERSION;
use anyhow::Result;
use std::collections::HashMap;
use std::path::Path;

pub fn compare(db_a: &Path, db_b: &Path, min_size: i64) -> Result<()> {
    let ca = db::open_db(db_a)?;
    let cb = db::open_db(db_b)?;
    let da: (Option<String>, Option<String>) = ca.query_row(
        "SELECT label, root_path FROM drive LIMIT 1",
        [],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    let dbb: (Option<String>, Option<String>) = cb.query_row(
        "SELECT label, root_path FROM drive LIMIT 1",
        [],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    let va = db::get_hash_version(&ca)?;
    let vb = db::get_hash_version(&cb)?;
    let sid_a = db::latest_snapshot_id(&ca)?;
    let sid_b = db::latest_snapshot_id(&cb)?;
    println!(
        "A = {} ({})  [hash v{}, snapshot {:?}]",
        da.0.as_deref().unwrap_or(""),
        da.1.as_deref().unwrap_or(""),
        va, sid_a,
    );
    println!(
        "B = {} ({})  [hash v{}, snapshot {:?}]",
        dbb.0.as_deref().unwrap_or(""),
        dbb.1.as_deref().unwrap_or(""),
        vb, sid_b,
    );
    if va != vb {
        eprintln!(
            "\nWARNING: partial-hash versions differ. Matches will be unreliable.\n\
             Re-index both drives with the current version (v{}).\n",
            HASH_VERSION
        );
    }
    println!();

    let sid_a = sid_a.unwrap_or(0);
    let sid_b = sid_b.unwrap_or(0);

    // Index B by (size, partial_hash). Vec<(rel_b, full_b)>.
    let mut b_index: HashMap<(i64, Vec<u8>), Vec<(String, Option<Vec<u8>>)>> =
        HashMap::new();
    {
        let mut stmt = cb.prepare(
            r#"SELECT size, partial_hash, rel_path, full_hash FROM entries
               WHERE snapshot_id=? AND is_dir=0 AND size >= ?
                 AND partial_hash IS NOT NULL"#,
        )?;
        let rows = stmt.query_map(rusqlite::params![sid_b, min_size], |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, Vec<u8>>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, Option<Vec<u8>>>(3)?,
            ))
        })?;
        for row in rows {
            let (size, partial, rel, fh) = row?;
            b_index
                .entry((size, partial))
                .or_default()
                .push((rel, fh));
        }
    }

    let mut matches: u64 = 0;
    let mut matched_size: u64 = 0;
    let mut confirmed: u64 = 0;
    let mut only_a: u64 = 0;

    let mut stmt = ca.prepare(
        r#"SELECT size, partial_hash, rel_path, full_hash FROM entries
           WHERE snapshot_id=? AND is_dir=0 AND size >= ?
             AND partial_hash IS NOT NULL"#,
    )?;
    let rows = stmt.query_map(rusqlite::params![sid_a, min_size], |r| {
        Ok((
            r.get::<_, i64>(0)?,
            r.get::<_, Vec<u8>>(1)?,
            r.get::<_, String>(2)?,
            r.get::<_, Option<Vec<u8>>>(3)?,
        ))
    })?;

    for row in rows {
        let (size, partial, rel_a, fh_a) = row?;
        let hits = match b_index.get(&(size, partial)) {
            Some(v) => v,
            None => {
                only_a += 1;
                continue;
            }
        };
        for (rel_b, fh_b) in hits {
            let tag;
            if let (Some(a), Some(b)) = (&fh_a, fh_b) {
                if a == b {
                    tag = "=";
                    confirmed += 1;
                } else {
                    continue; // full hashes differ → not a match
                }
            } else {
                tag = "≈";
            }
            matches += 1;
            matched_size += size as u64;
            println!(
                "  {} {:>8}  A:{}\n           B:{}",
                tag, human(size as f64), rel_a, rel_b,
            );
        }
    }
    println!(
        "\nmatches: {} ({})  confirmed-by-full-hash: {}",
        matches, human(matched_size as f64), confirmed,
    );
    println!("only in A: {} files", only_a);
    Ok(())
}
