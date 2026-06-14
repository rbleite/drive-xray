//! Filesystem walker. Manual DFS over `std::fs::read_dir` with our filter
//! chain: SKIP_DIR_NAMES (macOS system dirs), `-x` one-filesystem, and
//! --skip-cloud (CloudStorage / Mobile Documents / OneDrive / Dropbox /
//! ...). Yields raw entries in top-down order so the writer can resolve
//! parent_id from a running map.
//!
//! Single-threaded by design in Sprint 2 — the hashing parallelism happens
//! downstream (rayon in `index.rs`).

use anyhow::Result;
use std::fs;
use std::path::{Path, PathBuf};

#[cfg(unix)]
use std::os::unix::fs::MetadataExt;

// On Windows we use std::os::windows::fs::MetadataExt only for last_write_time
// (mtime), which is stable. For inode and device we call GetFileInformationByHandle
// via a raw winapi shim so we don't need the unstable `windows_by_handle` feature.
#[cfg(windows)]
use std::os::windows::fs::MetadataExt; // only last_write_time() used — stable
#[cfg(windows)]
use std::os::windows::fs::OpenOptionsExt; // custom_flags() for FILE_FLAG_BACKUP_SEMANTICS

/// Skip-list — must match `SKIP_DIR_NAMES` in `drive_xray.py`.
pub const SKIP_DIR_NAMES: &[&str] = &[
    ".Spotlight-V100", ".Trashes", ".fseventsd", ".TemporaryItems",
    ".DocumentRevisions-V100", ".PKInstallSandboxManager",
    ".PKInstallSandboxManager-SystemSoftware", ".HFS+ Private Directory Data",
    ".vol", "System Volume Information", "$RECYCLE.BIN",
];

/// Case-insensitive directory-name prefixes treated as cloud-sync mounts.
/// Must match `CLOUD_DIR_PREFIXES` in `drive_xray.py`.
pub const CLOUD_DIR_PREFIXES: &[&str] = &[
    "cloudstorage", "mobile documents", "icloud drive",
    "onedrive", "google drive", "googledrive", "dropbox",
    "pclouddrive", "box sync", "box-box", "creative cloud files",
    "mega", "megasync", "tresorit", "proton drive", "protondrive",
];

pub fn is_cloud_dir(name: &str) -> bool {
    let n = name.to_lowercase();
    CLOUD_DIR_PREFIXES.iter().any(|p| n.starts_with(p))
}

#[derive(Debug, Clone, Default)]
pub struct RawEntry {
    pub rel_path: String,
    pub parent_rel: Option<String>, // None for the root entry itself
    pub is_dir: bool,
    pub is_symlink: bool,
    pub size: Option<u64>,
    pub mtime: Option<f64>,         // Python float-seconds-since-epoch
    pub inode: Option<u64>,
    pub device: Option<u64>,
    pub error: Option<String>,
}

#[derive(Debug, Default, Clone, Copy)]
pub struct WalkStats {
    pub crossed: usize,        // subtrees pruned because they sit on another fs
    pub cloud_skipped: usize,  // subtrees pruned by --skip-cloud
}

#[derive(Debug, Default)]
pub struct WalkResult {
    pub entries: Vec<RawEntry>,
    pub stats: WalkStats,
}

/// Combine mtime into float seconds-since-Unix-epoch, matching Python's float.
#[cfg(unix)]
fn unix_mtime(md: &fs::Metadata) -> f64 {
    md.mtime() as f64 + md.mtime_nsec() as f64 * 1e-9
}

/// On Windows, `last_write_time()` returns a FILETIME (100-ns intervals since
/// 1601-01-01 UTC). Convert to Unix epoch seconds to match the Python value.
#[cfg(windows)]
fn unix_mtime(md: &fs::Metadata) -> f64 {
    // Seconds between 1601-01-01 and 1970-01-01
    const EPOCH_DIFF_SECS: u64 = 11_644_473_600;
    let ft = md.last_write_time(); // 100-ns ticks since 1601
    let secs = ft / 10_000_000;
    let subsec = (ft % 10_000_000) as f64 * 1e-7;
    (secs.saturating_sub(EPOCH_DIFF_SECS)) as f64 + subsec
}

/// Device ID: volume serial number on Windows, st_dev on Unix.
#[cfg(unix)]
fn meta_dev(md: &fs::Metadata) -> u64 { md.dev() }
#[cfg(windows)]
fn meta_dev(_md: &fs::Metadata) -> u64 {
    // volume_serial_number() requires unstable `windows_by_handle`.
    // For one_fs enforcement we use win_file_info() below instead.
    // Returning 0 here means meta_dev is only called where win_file_info
    // already handles the one_fs check — this value is stored in the DB
    // purely for informational purposes.
    0
}

/// Inode: file index on Windows, st_ino on Unix.
#[cfg(unix)]
fn meta_ino(md: &fs::Metadata) -> u64 { md.ino() }
#[cfg(windows)]
fn meta_ino(_md: &fs::Metadata) -> u64 {
    // file_index() requires unstable `windows_by_handle`.
    // Inode is used for hardlink dedup on Unix; on Windows we store 0.
    0
}

/// File size — `std::fs::Metadata::len()` is stable and cross-platform.
fn meta_size(md: &fs::Metadata) -> u64 { md.len() }

/// On Windows, obtain (volume_serial, file_index) by opening the file and
/// calling GetFileInformationByHandle. This avoids the unstable
/// `windows_by_handle` feature while giving us stable inode + device values
/// for one_fs checks and hardlink detection.
#[cfg(windows)]
fn win_file_info(path: &std::path::Path) -> Option<(u64, u64)> {
    use std::os::windows::io::AsRawHandle;
    use std::fs::OpenOptions;

    // Open without read permission — we only need the handle for metadata.
    let f = OpenOptions::new()
        .read(true)
        .custom_flags(0x0200_0000) // FILE_FLAG_BACKUP_SEMANTICS — needed for dirs
        .open(path)
        .ok()?;

    // SAFETY: GetFileInformationByHandle is a simple FFI call with a valid
    // handle and a stack-allocated output struct.
    #[repr(C)]
    #[allow(non_snake_case)]
    struct BY_HANDLE_FILE_INFORMATION {
        dwFileAttributes: u32,
        ftCreationTime: [u32; 2],
        ftLastAccessTime: [u32; 2],
        ftLastWriteTime: [u32; 2],
        dwVolumeSerialNumber: u32,
        nFileSizeHigh: u32,
        nFileSizeLow: u32,
        nNumberOfLinks: u32,
        nFileIndexHigh: u32,
        nFileIndexLow: u32,
    }
    extern "system" {
        fn GetFileInformationByHandle(
            hFile: *mut std::ffi::c_void,
            lpFileInformation: *mut BY_HANDLE_FILE_INFORMATION,
        ) -> i32;
    }

    let mut info = std::mem::MaybeUninit::<BY_HANDLE_FILE_INFORMATION>::uninit();
    let ok = unsafe {
        GetFileInformationByHandle(f.as_raw_handle() as *mut _, info.as_mut_ptr())
    };
    if ok == 0 { return None; }
    let info = unsafe { info.assume_init() };
    let serial = info.dwVolumeSerialNumber as u64;
    let index = ((info.nFileIndexHigh as u64) << 32) | (info.nFileIndexLow as u64);
    Some((serial, index))
}

/// Walk `root` synchronously, applying our filters. Returns entries in
/// top-down DFS order so the writer can resolve parent_id from a running
/// `HashMap<rel_path, rowid>`.
pub fn walk(root: &Path, one_fs: bool, skip_cloud: bool) -> Result<WalkResult> {
    let root = root.canonicalize()?;
    let root_md = fs::symlink_metadata(&root)?;
    if !root_md.is_dir() {
        anyhow::bail!("not a directory: {}", root.display());
    }
    #[cfg(unix)]
    let root_dev = meta_dev(&root_md);
    #[cfg(windows)]
    let root_dev: u64 = win_file_info(&root).map(|(s, _)| s).unwrap_or(0);

    let mut entries: Vec<RawEntry> = Vec::new();
    let mut stats = WalkStats::default();

    // Root entry first — parent_rel = None signals "this is the top".
    // We deliberately leave mtime / inode / device as NULL to match the
    // Python implementation byte-for-byte (drive_xray.py:index_drive inserts
    // the root with all-None metadata). The only thing the rest of the
    // pipeline needs for the root row is `is_dir=1` so subdirs can resolve
    // their parent_id from rel_path = ".".
    entries.push(RawEntry {
        rel_path: ".".into(),
        parent_rel: None,
        is_dir: true,
        is_symlink: false,
        size: None,
        mtime: None,
        inode: None,
        device: None,
        error: None,
    });

    // DFS using an explicit stack of (abs_path, rel_path) for the dir to
    // recurse into. We push subdirs in reverse order so popping gives
    // alphabetical-ish iteration of read_dir.
    let mut stack: Vec<(PathBuf, String)> = vec![(root.clone(), ".".into())];

    while let Some((dp, rel_dir)) = stack.pop() {
        let rd = match fs::read_dir(&dp) {
            Ok(rd) => rd,
            Err(e) => {
                // We can still emit an error entry for the directory itself
                // if it isn't the root — but the simpler choice is to drop
                // unreadable dirs entirely (matches the Python onerror=None
                // behavior of os.walk).
                let _ = e;
                continue;
            }
        };

        let mut subdirs_to_recurse: Vec<(PathBuf, String)> = Vec::new();

        for de in rd {
            let de = match de {
                Ok(d) => d,
                Err(_) => continue,
            };
            let name = de.file_name().to_string_lossy().to_string();
            let rel = if rel_dir == "." {
                name.clone()
            } else {
                format!("{}/{}", rel_dir, name)
            };
            let abs = de.path();

            let st = match fs::symlink_metadata(&abs) {
                Ok(s) => s,
                Err(e) => {
                    entries.push(RawEntry {
                        rel_path: rel,
                        parent_rel: Some(rel_dir.clone()),
                        is_dir: false,
                        is_symlink: false,
                        size: None,
                        mtime: None,
                        inode: None,
                        device: None,
                        error: Some(e.to_string()),
                    });
                    continue;
                }
            };

            let ft = st.file_type();
            let is_link = ft.is_symlink();
            let is_dir = ft.is_dir() && !is_link;

            if is_dir {
                // Skip-list (always applied).
                if SKIP_DIR_NAMES.contains(&name.as_str()) {
                    continue;
                }
                // --skip-cloud: prune cloud-sync folders entirely.
                if skip_cloud && is_cloud_dir(&name) {
                    stats.cloud_skipped += 1;
                    continue;
                }
                // -x: don't cross mount points / APFS firmlinks.
                #[cfg(unix)]
                let (dir_dev, dir_ino) = (meta_dev(&st), meta_ino(&st));
                #[cfg(windows)]
                let (dir_dev, dir_ino) = win_file_info(&abs).unwrap_or((0, 0));

                if one_fs && dir_dev != root_dev {
                    stats.crossed += 1;
                    continue;
                }

                entries.push(RawEntry {
                    rel_path: rel.clone(),
                    parent_rel: Some(rel_dir.clone()),
                    is_dir: true,
                    is_symlink: false,
                    size: None,
                    mtime: Some(unix_mtime(&st)),
                    inode: Some(dir_ino),
                    device: Some(dir_dev),
                    error: None,
                });
                subdirs_to_recurse.push((abs, rel));
            } else if is_link {
                // Record as a 0-size file-like entry, but mark is_symlink.
                #[cfg(unix)]
                let (lnk_dev, lnk_ino) = (meta_dev(&st), meta_ino(&st));
                #[cfg(windows)]
                let (lnk_dev, lnk_ino) = win_file_info(&abs).unwrap_or((0, 0));
                entries.push(RawEntry {
                    rel_path: rel,
                    parent_rel: Some(rel_dir.clone()),
                    is_dir: false,
                    is_symlink: true,
                    size: Some(0),
                    mtime: Some(unix_mtime(&st)),
                    inode: Some(lnk_ino),
                    device: Some(lnk_dev),
                    error: None,
                });
            } else if ft.is_file() {
                #[cfg(unix)]
                let (file_dev, file_ino) = (meta_dev(&st), meta_ino(&st));
                #[cfg(windows)]
                let (file_dev, file_ino) = win_file_info(&abs).unwrap_or((0, 0));
                entries.push(RawEntry {
                    rel_path: rel,
                    parent_rel: Some(rel_dir.clone()),
                    is_dir: false,
                    is_symlink: false,
                    size: Some(meta_size(&st)),
                    mtime: Some(unix_mtime(&st)),
                    inode: Some(file_ino),
                    device: Some(file_dev),
                    error: None,
                });
            }
            // FIFOs / sockets / device nodes: skipped, matching Python.
        }

        // Push in reverse so DFS pops them in original order.
        for s in subdirs_to_recurse.into_iter().rev() {
            stack.push(s);
        }
    }

    Ok(WalkResult { entries, stats })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs::{create_dir_all, write};

    fn make_tree() -> tempfile::TempDir {
        let td = tempfile::tempdir().unwrap();
        let r = td.path();
        create_dir_all(r.join("a/sub")).unwrap();
        create_dir_all(r.join("b")).unwrap();
        create_dir_all(r.join(".Spotlight-V100")).unwrap(); // should be skipped
        create_dir_all(r.join("OneDrive - Acme")).unwrap(); // cloud
        create_dir_all(r.join("Library/CloudStorage/GoogleDrive-me")).unwrap();
        write(r.join("top.txt"), b"x").unwrap();
        write(r.join("a/file.txt"), b"yy").unwrap();
        write(r.join("a/sub/deep.txt"), b"zzz").unwrap();
        write(r.join("b/empty.bin"), b"").unwrap();
        write(r.join("OneDrive - Acme/secret.docx"), b"won't see this").unwrap();
        td
    }

    fn rel_paths(res: &WalkResult) -> Vec<String> {
        res.entries.iter().map(|e| e.rel_path.clone()).collect()
    }

    #[test]
    fn skip_dir_names_default() {
        let td = make_tree();
        let r = walk(td.path(), false, false).unwrap();
        let paths = rel_paths(&r);
        // .Spotlight-V100 should never appear.
        assert!(!paths.iter().any(|p| p.contains(".Spotlight-V100")));
        // Without --skip-cloud, OneDrive folder is walked.
        assert!(paths.iter().any(|p| p == "OneDrive - Acme"));
        assert!(paths.iter().any(|p| p == "OneDrive - Acme/secret.docx"));
    }

    #[test]
    fn skip_cloud_filters() {
        let td = make_tree();
        let r = walk(td.path(), false, true).unwrap();
        let paths = rel_paths(&r);
        assert!(!paths.iter().any(|p| p.starts_with("OneDrive")));
        // CloudStorage is also pruned (matches by prefix).
        assert!(!paths.iter().any(|p| p.starts_with("Library/CloudStorage")));
        // `Library` itself is still walked (not a cloud prefix).
        assert!(paths.iter().any(|p| p == "Library"));
        assert!(r.stats.cloud_skipped >= 2);
    }

    #[test]
    fn root_is_first_entry() {
        let td = make_tree();
        let r = walk(td.path(), false, false).unwrap();
        assert_eq!(r.entries[0].rel_path, ".");
        assert!(r.entries[0].is_dir);
        assert!(r.entries[0].parent_rel.is_none());
        // Parity with Python: root entry has all metadata fields NULL.
        assert!(r.entries[0].mtime.is_none());
        assert!(r.entries[0].inode.is_none());
        assert!(r.entries[0].device.is_none());
    }

    #[test]
    fn parent_appears_before_children() {
        let td = make_tree();
        let r = walk(td.path(), false, true).unwrap();
        let paths = rel_paths(&r);
        // Index of a parent must be < index of any of its children.
        let idx = |p: &str| paths.iter().position(|x| x == p);
        let a_idx = idx("a").unwrap();
        let a_sub_idx = idx("a/sub").unwrap();
        let deep_idx = idx("a/sub/deep.txt").unwrap();
        assert!(a_idx < a_sub_idx);
        assert!(a_sub_idx < deep_idx);
    }

    #[test]
    fn metadata_populated_for_files() {
        let td = make_tree();
        let r = walk(td.path(), false, true).unwrap();
        let top = r.entries.iter().find(|e| e.rel_path == "top.txt").unwrap();
        assert!(!top.is_dir);
        assert!(!top.is_symlink);
        assert_eq!(top.size, Some(1));
        assert!(top.inode.is_some());
        assert!(top.mtime.unwrap() > 0.0);
    }

    #[test]
    fn is_cloud_dir_matches() {
        assert!(is_cloud_dir("OneDrive"));
        assert!(is_cloud_dir("OneDrive - Acme Corp"));
        assert!(is_cloud_dir("CloudStorage"));
        assert!(is_cloud_dir("Mobile Documents"));
        assert!(is_cloud_dir("Dropbox"));
        assert!(!is_cloud_dir("Documents"));
        assert!(!is_cloud_dir("Public"));
    }
}
