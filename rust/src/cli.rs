//! Clap definitions matching the Python `argparse` surface in
//! `drive_xray.py:main()`. Subcommand defaults and help strings should
//! mirror the Python ones so the Streamlit subprocess calls keep working.

use clap::{Parser, Subcommand};
use std::path::PathBuf;

/// Build-time strings shown by `dx --version` (short) and `dx -V` (clap
/// inverts these). Both include the schema and hash protocol versions
/// so the user can tell, a year from now, why an older `.db` opens with
/// a migration banner.
const VERSION_SHORT: &str = concat!(
    env!("CARGO_PKG_VERSION"),
    " (schema v5 · hash v2)",
);
const VERSION_LONG: &str = concat!(
    env!("CARGO_PKG_VERSION"), "\n",
    "  schema:  v5 (path interning)\n",
    "  hash:    BLAKE2b v2 (head + middle + tail)\n",
    "  repo:    ", env!("CARGO_PKG_REPOSITORY"), "\n",
);

#[derive(Parser, Debug)]
#[command(name = "dx", about = "drive-xray: index + dedupe + snapshots",
          version = VERSION_SHORT,
          long_version = VERSION_LONG)]
pub struct Cli {
    #[command(subcommand)]
    pub command: Command,
}

#[derive(Subcommand, Debug)]
pub enum Command {
    /// X-ray a drive into a sqlite db.
    Index {
        root: PathBuf,
        #[arg(long)]
        db: Option<PathBuf>,
        #[arg(long)]
        label: Option<String>,
        #[arg(long)]
        full: bool,
        #[arg(short = 'x', long = "one-filesystem")]
        one_fs: bool,
        #[arg(long = "skip-cloud")]
        skip_cloud: bool,
    },
    /// Re-index, overwriting the latest snapshot in place.
    Refresh {
        db: PathBuf,
        #[arg(long)]
        full: bool,
    },
    /// Snapshot operations.
    Snapshot {
        #[command(subcommand)]
        op: SnapshotOp,
    },
    /// Apply retention policy to old snapshots.
    Prune {
        db: PathBuf,
        #[arg(long, default_value_t = 10)]
        keep_last: usize,
        #[arg(long, default_value_t = 12)]
        keep_monthly: usize,
    },
    /// Diff two snapshots (default: previous → latest).
    Diff {
        db: PathBuf,
        #[arg(long = "from")]
        from_id: Option<i64>,
        #[arg(long = "to")]
        to_id: Option<i64>,
        #[arg(long, default_value_t = 10)]
        top: usize,
    },
    /// Find duplicates within an indexed drive.
    Dedupe {
        db: PathBuf,
        #[arg(long, default_value_t = 1024)]
        min_size: u64,
    },
    /// Compare two indexed drives.
    Compare {
        db_a: PathBuf,
        db_b: PathBuf,
        #[arg(long, default_value_t = 1024)]
        min_size: u64,
    },
    /// Export duplicate groups as CSV or XLSX.
    Export {
        db: PathBuf,
        out: PathBuf,
        #[arg(long, default_value_t = 1024)]
        min_size: u64,
        #[arg(long, value_parser = ["csv", "xlsx"])]
        format: Option<String>,
    },
    /// Generate a shell script proposing deletes/moves for duplicates.
    Cleanup {
        db: PathBuf,
        #[arg(long, default_value = "shortest",
              value_parser = ["shortest", "oldest", "newest", "alphabetical"])]
        strategy: String,
        #[arg(long, default_value = "quarantine",
              value_parser = ["delete", "quarantine"])]
        action: String,
        #[arg(long, default_value_t = 1048576)]
        min_size: u64,
        #[arg(short = 'o', long)]
        out: Option<PathBuf>,
    },
    /// VACUUM + WAL checkpoint to shrink the .db file.
    Compact { db: PathBuf },
    /// Find duplicates across multiple indexed drives.
    CrossDedupe {
        /// Paths to .db index files to compare (two or more).
        dbs: Vec<PathBuf>,
        #[arg(long, default_value_t = 1048576)]
        min_size: i64,
        /// Emit results as a JSON array to stdout (for Streamlit integration).
        #[arg(long)]
        json: bool,
    },
}

#[derive(Subcommand, Debug)]
pub enum SnapshotOp {
    /// Take a new snapshot, preserving previous ones.
    Take {
        db: PathBuf,
        #[arg(long)]
        full: bool,
        #[arg(long)]
        no_prune: bool,
        #[arg(long, default_value_t = 10)]
        keep_last: usize,
        #[arg(long, default_value_t = 12)]
        keep_monthly: usize,
    },
    /// List snapshots in the db.
    List { db: PathBuf },
}

pub fn dispatch() -> anyhow::Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Command::Index { root, db, label, full, one_fs, skip_cloud } => {
            let lbl = label.clone().unwrap_or_else(|| {
                root.file_name()
                    .and_then(|s| s.to_str())
                    .unwrap_or("drive")
                    .to_string()
            });
            let db_path = db.unwrap_or_else(|| {
                let home = std::env::var("HOME").unwrap_or_else(|_| ".".into());
                PathBuf::from(home)
                    .join("tools/drive-xray")
                    .join(format!("{lbl}.db"))
            });
            if let Some(parent) = db_path.parent() {
                std::fs::create_dir_all(parent).ok();
            }
            let sid = crate::index::index_drive(
                &root, &db_path, Some(&lbl),
                full, one_fs, skip_cloud,
                None, crate::index::Mode::Fresh, None,
            )?;
            eprintln!("  done: snapshot id {sid}");
            Ok(())
        }
        Command::Refresh { db, full } => {
            eprintln!("refreshing {}", db.display());
            let sid = crate::index::refresh_drive(&db, full)?;
            eprintln!("  done: snapshot id {sid}");
            Ok(())
        }
        Command::Snapshot { op } => match op {
            SnapshotOp::Take { db, full, no_prune, keep_last, keep_monthly } => {
                eprintln!("taking snapshot of {}", db.display());
                let sid = crate::snapshot::take(
                    &db, full, !no_prune, keep_last, keep_monthly,
                )?;
                eprintln!("  new snapshot id: {sid}");
                Ok(())
            }
            SnapshotOp::List { db } => {
                let snaps = crate::snapshot::list(&db)?;
                let name = db.file_name().and_then(|s| s.to_str()).unwrap_or("");
                println!("  {} snapshot(s) in {}:", snaps.len(), name);
                for s in &snaps {
                    let files = s.total_files.unwrap_or(0);
                    let size = crate::util::human(s.total_size.unwrap_or(0) as f64);
                    println!(
                        "  #{:>3}  {}   {:>10} files   {:>10}   {}",
                        s.id, s.taken_at, files, size,
                        s.label.as_deref().unwrap_or("")
                    );
                }
                Ok(())
            }
        },
        Command::Prune { db, keep_last, keep_monthly } => {
            let pruned = crate::snapshot::prune(&db, keep_last, keep_monthly)?;
            if pruned.is_empty() {
                println!("  no snapshots to prune");
            } else {
                println!(
                    "  pruned {} snapshot(s): {:?}",
                    pruned.len(),
                    pruned
                );
            }
            Ok(())
        }
        Command::Diff { db, from_id, to_id, top } => {
            let d = crate::snapshot::diff(&db, from_id, to_id, top)?;
            crate::snapshot::print_diff(&d);
            Ok(())
        }
        Command::Dedupe { db, min_size } => {
            crate::dedupe::dedupe(&db, min_size as i64)?;
            Ok(())
        }
        Command::Compare { db_a, db_b, min_size } => {
            crate::compare::compare(&db_a, &db_b, min_size as i64)?;
            Ok(())
        }
        Command::Export { db, out, min_size, format } => {
            let fmt = format.unwrap_or_else(|| {
                out.extension()
                    .and_then(|s| s.to_str())
                    .unwrap_or("")
                    .to_lowercase()
            });
            if fmt != "csv" && fmt != "xlsx" {
                anyhow::bail!(
                    "cannot infer format from extension {:?}; use --format",
                    out.extension()
                );
            }
            crate::export::export(&db, &out, &fmt, min_size as i64)?;
            Ok(())
        }
        Command::Cleanup { db, strategy, action, min_size, out } => {
            let strat = crate::cleanup::Strategy::parse(&strategy)?;
            let act = crate::cleanup::Action::parse(&action)?;
            let script = crate::cleanup::generate(&db, min_size as i64, strat, act)?;
            match out {
                Some(path) => {
                    std::fs::write(&path, &script)?;
                    eprintln!("  wrote cleanup script to {}", path.display());
                }
                None => print!("{script}"),
            }
            Ok(())
        }
        Command::Compact { db } => {
            eprintln!("compacting {}", db.display());
            crate::compact::compact(&db)?;
            Ok(())
        }
        Command::CrossDedupe { dbs, min_size, json } => {
            crate::cross_dedupe::run(&dbs, min_size, json)?;
            Ok(())
        }
    }
}
