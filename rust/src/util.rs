//! Shared helpers: human(), int64 wrap for uint64 inodes, basename.

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

#[cfg(test)]
mod tests {
    use super::*;

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
