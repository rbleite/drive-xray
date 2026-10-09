//! Shared helpers: human(), int64 wrap for uint64 inodes, basename.

use unicode_normalization::UnicodeNormalization;

/// Fold a rel_path to Unicode NFC for cross-OS *comparison* only. macOS walks
/// filenames decomposed (NFD), Windows/Linux composed (NFC), so the same file
/// has different rel_path bytes per OS — which broke reuse-cache hits, showed
/// phantom snapshot-diff add/remove pairs, and could fail resolve_root's
/// fingerprint across a Mac↔Windows sync. Mirrors Python `nfc()` in
/// drive_xray.py. Stored rel_path bytes are NEVER normalized: any path
/// reconstructed for I/O (root.join(rel_path)) keeps the exact on-disk form so
/// it still opens on normalization-sensitive filesystems (exFAT/FAT).
pub fn nfc(s: &str) -> String {
    s.nfc().collect()
}

/// Format a byte count human-readably. Must match Python `human()` to keep
/// CLI stdout byte-identical (powers of 1024, one decimal place).
pub fn human(mut n: f64) -> String {
    for unit in ["B", "KB", "MB", "GB", "TB"] {
        if n < 1024.0 {
            return format!("{:.1}{}", n, unit);
        }
        n /= 1024.0;
    }
    format!("{:.1}PB", n)
}

/// Wrap a u64 inode/dev into signed i64 so SQLite INTEGER accepts it.
/// Matches Python `_i64()`.
#[inline]
pub fn i64_wrap(n: u64) -> i64 {
    n as i64 // Rust `as` is the wrap-on-overflow conversion we need.
}

/// `os.path.basename` equivalent. Returns the last path segment or the
/// whole string if there is no '/'.
pub fn basename(rel: &str) -> &str {
    match rel.rfind('/') {
        Some(i) => &rel[i + 1..],
        None => rel,
    }
}

/// Parent rel-path. Returns "." for top-level entries (matches Python).
pub fn parent_rel(rel: &str) -> &str {
    match rel.rfind('/') {
        Some(i) => &rel[..i],
        None => ".",
    }
}

/// `\\?\E:\Films` → `E:\Films`; `\\?\UNC\srv\share` → `\\srv\share`.
///
/// `Path::canonicalize` returns Windows paths in this "verbatim" form, and it
/// was stored as drive.root_path: shown in the app, different from the `E:\`
/// the Python engine stores for the same drive, and not recognised as a
/// Windows root by migrate_windows_seps. A verbatim path also opts out of
/// Win32 path normalisation, which every `{root}/{rel}` built from it relied on.
///
/// Left unchanged whenever dropping the prefix could change what the path
/// means: other verbatim forms (`\\?\Volume{…}`), anything over MAX_PATH, and
/// names only reachable verbatim (reserved device names, a trailing dot or
/// space). Plain paths pass through. String-based so it is testable on every
/// OS. Mirrors `strip_verbatim` in drive_xray.py.
pub fn strip_verbatim(p: &str) -> String {
    let Some(rest) = p.strip_prefix(r"\\?\") else {
        return p.to_string();
    };
    let b = rest.as_bytes();
    let (out, tail): (String, Vec<&str>) =
        if b.len() >= 4 && rest[..4].eq_ignore_ascii_case(r"UNC\") {
            let unc: Vec<&str> = rest[4..].split('\\').collect();
            if unc.len() < 2 || unc[0].is_empty() || unc[1].is_empty() {
                return p.to_string(); // not \\server\share
            }
            (format!(r"\\{}", &rest[4..]), unc[2..].to_vec())
        } else if b.len() >= 2 && b[0].is_ascii_alphabetic() && b[1] == b':'
            && (b.len() == 2 || b[2] == b'\\')
        {
            // "E:" alone means the current directory on E, not its root
            let out = if b.len() == 2 { format!("{rest}\\") } else { rest.to_string() };
            let tail = if b.len() > 3 { rest[3..].split('\\').collect() } else { vec![] };
            (out, tail)
        } else {
            return p.to_string();
        };
    if out.encode_utf16().count() >= 260 || out.contains('/') {
        return p.to_string();
    }
    const RESERVED: [&str; 4] = ["CON", "PRN", "AUX", "NUL"];
    for part in tail.iter().filter(|s| !s.is_empty()) {
        if *part == "." || *part == ".." || part.ends_with('.') || part.ends_with(' ') {
            return p.to_string();
        }
        let stem = part.split('.').next().unwrap_or("").trim_end_matches(' ')
            .to_ascii_uppercase();
        let numbered = stem.len() == 4
            && (stem.starts_with("COM") || stem.starts_with("LPT"))
            && matches!(stem.as_bytes()[3], b'1'..=b'9');
        if RESERVED.contains(&stem.as_str()) || numbered {
            return p.to_string();
        }
    }
    out
}

/// `strip_verbatim` for a `Path`, as `canonicalize` hands it back.
pub fn strip_verbatim_path(p: std::path::PathBuf) -> std::path::PathBuf {
    match p.to_str() {
        Some(s) if s.starts_with(r"\\?\") => std::path::PathBuf::from(strip_verbatim(s)),
        _ => p,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strip_verbatim_cases() {
        let cases = [
            (r"\\?\E:\", r"E:\"),
            (r"\\?\E:", r"E:\"),
            (r"\\?\e:\Films\Old", r"e:\Films\Old"),
            (r"\\?\UNC\nas\share", r"\\nas\share"),
            (r"\\?\UNC\nas\share\x", r"\\nas\share\x"),
            (r"\\?\unc\nas\share", r"\\nas\share"),
            // left alone: the prefix is what makes these work
            (r"\\?\UNC\nas", r"\\?\UNC\nas"),
            (r"\\?\Volume{0b1e}\", r"\\?\Volume{0b1e}\"),
            (r"\\?\E:\CON", r"\\?\E:\CON"),
            (r"\\?\E:\lpt1.txt", r"\\?\E:\lpt1.txt"),
            (r"\\?\E:\trailing.", r"\\?\E:\trailing."),
            (r"\\?\E:\trailing ", r"\\?\E:\trailing "),
            (r"\\?\E:\a/b", r"\\?\E:\a/b"),
            // not verbatim at all
            (r"E:\", r"E:\"),
            ("/Volumes/X", "/Volumes/X"),
            (r"\\nas\share", r"\\nas\share"),
            ("", ""),
        ];
        for (inp, want) in cases {
            assert_eq!(strip_verbatim(inp), want, "input {inp:?}");
        }
        let long = format!(r"\\?\E:\{}", "a".repeat(300));
        assert_eq!(strip_verbatim(&long), long);
        // COM0 and CONSOLE are ordinary names
        assert_eq!(strip_verbatim(r"\\?\E:\COM0"), r"E:\COM0");
        assert_eq!(strip_verbatim(r"\\?\E:\CONSOLE"), r"E:\CONSOLE");
    }

    #[test]
    fn human_basics() {
        assert_eq!(human(0.0), "0.0B");
        assert_eq!(human(1023.0), "1023.0B");
        assert_eq!(human(1024.0), "1.0KB");
        assert_eq!(human(1024.0 * 1024.0), "1.0MB");
    }

    #[test]
    fn i64_wrap_matches_python() {
        // The test vectors from the Python smoke test:
        assert_eq!(i64_wrap(0), 0);
        assert_eq!(i64_wrap(1), 1);
        assert_eq!(i64_wrap(i64::MAX as u64), i64::MAX);
        assert_eq!(i64_wrap(i64::MAX as u64 + 1), i64::MIN);
        assert_eq!(i64_wrap(u64::MAX), -1);
    }

    #[test]
    fn basename_parent() {
        assert_eq!(basename("a/b/c.txt"), "c.txt");
        assert_eq!(basename("root.txt"), "root.txt");
        assert_eq!(parent_rel("a/b/c.txt"), "a/b");
        assert_eq!(parent_rel("root.txt"), ".");
    }
}
