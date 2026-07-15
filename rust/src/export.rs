//! CSV / XLSX export of duplicate groups. Mirrors `_duplicate_rows` +
//! `export_duplicates` in `drive_xray.py`. Output columns are identical
//! (group_id, hash, size_bytes, size_human, group_count, distinct_inodes,
//! wasted_bytes, wasted_human, path, is_hardlink).

use crate::dedupe::{duplicate_rows, DupRow};
use crate::{db, dedupe};
use anyhow::{anyhow, Result};
use rust_xlsxwriter::{Format, FormatBorder, Workbook};
use std::path::Path;

const HEADERS: [&str; 10] = [
    "group_id", "hash", "size_bytes", "size_human", "group_count",
    "distinct_inodes", "wasted_bytes", "wasted_human", "path", "is_hardlink",
];

/// Same fill_full_hashes warmup as Python, then write rows in the requested
/// format. `format` is "csv" or "xlsx"; inferred from extension by the CLI.
pub fn export(db_path: &Path, out: &Path, format: &str, min_size: i64) -> Result<()> {
    // Best-effort: compute missing full hashes if the drive is mounted.
    let conn = db::open_db(db_path)?;
    let drv: Option<(String,)> = conn
        .query_row("SELECT root_path FROM drive LIMIT 1", [], |r| Ok((r.get(0)?,)))
        .ok();
    let resolved = drv.as_ref().map(|(r,)| db::resolve_root(&conn, r));
    drop(conn);
    if let Some(root) = resolved {
        if root.is_dir() {
            dedupe::fill_full_hashes(db_path, &root, min_size, None)?;
        } else {
            eprintln!(
                "  warning: root {} not mounted — using existing full_hash only",
                root.display()
            );
        }
    }

    let rows = duplicate_rows(db_path, min_size, None)?;
    if rows.is_empty() {
        eprintln!(
            "  no duplicate groups found above min-size — nothing to export"
        );
        return Ok(());
    }

    match format {
        "csv" => write_csv(out, &rows)?,
        "xlsx" => write_xlsx(out, &rows)?,
        other => return Err(anyhow!("unknown export format: {other}")),
    }
    eprintln!("  wrote {} rows to {}", rows.len(), out.display());
    Ok(())
}

fn write_csv(out: &Path, rows: &[DupRow]) -> Result<()> {
    let mut w = csv::Writer::from_path(out)?;
    w.write_record(HEADERS)?;
    for r in rows {
        w.write_record([
            r.group_id.to_string(),
            r.hash_hex.clone(),
            r.size_bytes.to_string(),
            r.size_human.clone(),
            r.group_count.to_string(),
            r.distinct_inodes.to_string(),
            r.wasted_bytes.to_string(),
            r.wasted_human.clone(),
            r.path.clone(),
            (r.is_hardlink as i32).to_string(),
        ])?;
    }
    w.flush()?;
    Ok(())
}

fn write_xlsx(out: &Path, rows: &[DupRow]) -> Result<()> {
    let mut book = Workbook::new();
    let sheet = book.add_worksheet();
    sheet.set_name("Duplicates")?;

    // Header style: bold + grey fill (matches Python openpyxl version).
    let header_fmt = Format::new()
        .set_bold()
        .set_background_color("EEEEEE")
        .set_border(FormatBorder::Thin);
    for (i, h) in HEADERS.iter().enumerate() {
        sheet.write_string_with_format(0, i as u16, *h, &header_fmt)?;
    }

    // Data rows.
    for (ri, r) in rows.iter().enumerate() {
        let row = (ri + 1) as u32;
        sheet.write_number(row, 0, r.group_id as f64)?;
        sheet.write_string(row, 1, &r.hash_hex)?;
        sheet.write_number(row, 2, r.size_bytes as f64)?;
        sheet.write_string(row, 3, &r.size_human)?;
        sheet.write_number(row, 4, r.group_count as f64)?;
        sheet.write_number(row, 5, r.distinct_inodes as f64)?;
        sheet.write_number(row, 6, r.wasted_bytes as f64)?;
        sheet.write_string(row, 7, &r.wasted_human)?;
        sheet.write_string(row, 8, &r.path)?;
        sheet.write_number(row, 9, r.is_hardlink as i32 as f64)?;
    }

    // Column widths matching the Python openpyxl defaults.
    let widths: &[(&str, f64)] = &[
        ("path", 60.0), ("hash", 28.0), ("size_human", 12.0), ("wasted_human", 14.0),
    ];
    for (i, h) in HEADERS.iter().enumerate() {
        let w = widths.iter().find_map(|(n, w)| (n == h).then_some(*w)).unwrap_or(14.0);
        sheet.set_column_width(i as u16, w)?;
    }

    sheet.set_freeze_panes(1, 0)?;
    book.save(out)?;
    Ok(())
}
