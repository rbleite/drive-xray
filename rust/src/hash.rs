//! BLAKE2b hashing — partial (16 bytes, head+middle+tail) and full
//! (32 bytes). Output must be byte-identical to Python `hashlib.blake2b`
//! so `.db` files are interchangeable.
//!
//! Algorithm:
//!   partial = BLAKE2b-128(size_le_u64 || head_64k || middle_64k || tail_64k)
//!   full    = BLAKE2b-256(whole_file)
//!
//! Files of size ≤ 3 * PARTIAL_CHUNK are read whole into the partial hash.
//! Empty files get a deterministic digest of just the size header (no read).
//!
//! Merkle hash for directories:
//!   for each child sorted by name:
//!     update(b"D" or b"F"); update(name_utf8); update(child.full_hash)

use crate::{PARTIAL_CHUNK, READ_CHUNK};
use blake2b_simd::Params;
use std::fs::File;
use std::io::{self, Read, Seek, SeekFrom};
use std::path::Path;

/// 16-byte partial hash (BLAKE2b digest_size=16).
pub fn partial(path: &Path, size: u64) -> io::Result<[u8; 16]> {
    let mut state = Params::new().hash_length(16).to_state();
    state.update(&size.to_le_bytes());
    if size == 0 {
        return Ok(state.finalize().as_bytes().try_into().unwrap());
    }
    let mut f = File::open(path)?;
    let three_chunks = (3 * PARTIAL_CHUNK) as u64;
    if size <= three_chunks {
        let mut buf = Vec::with_capacity(size as usize);
        f.read_to_end(&mut buf)?;
        state.update(&buf);
    } else {
        let mut head = vec![0u8; PARTIAL_CHUNK];
        f.read_exact(&mut head)?;
        state.update(&head);

        let middle_off = size / 2 - (PARTIAL_CHUNK as u64) / 2;
        f.seek(SeekFrom::Start(middle_off))?;
        let mut middle = vec![0u8; PARTIAL_CHUNK];
        f.read_exact(&mut middle)?;
        state.update(&middle);

        f.seek(SeekFrom::End(-(PARTIAL_CHUNK as i64)))?;
        let mut tail = vec![0u8; PARTIAL_CHUNK];
        f.read_exact(&mut tail)?;
        state.update(&tail);
    }
    Ok(state.finalize().as_bytes().try_into().unwrap())
}

/// 32-byte full hash (BLAKE2b digest_size=32).
pub fn full(path: &Path) -> io::Result<[u8; 32]> {
    let mut state = Params::new().hash_length(32).to_state();
    let mut f = File::open(path)?;
    let mut buf = vec![0u8; READ_CHUNK];
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        state.update(&buf[..n]);
    }
    Ok(state.finalize().as_bytes().try_into().unwrap())
}

/// Merkle hash of a directory given its children pre-sorted by name.
/// Each item is (name, is_dir, child_full_hash).
pub fn merkle<'a, I>(children: I) -> [u8; 32]
where
    I: IntoIterator<Item = (&'a str, bool, &'a [u8])>,
{
    let mut state = Params::new().hash_length(32).to_state();
    for (name, is_dir, child_hash) in children {
        state.update(if is_dir { b"D" } else { b"F" });
        state.update(name.as_bytes());
        state.update(child_hash);
    }
    state.finalize().as_bytes().try_into().unwrap()
}

/// Compute partial hash for in-memory content (used by tests and by callers
/// that already have the bytes).
pub fn partial_of_bytes(content: &[u8]) -> [u8; 16] {
    let size = content.len() as u64;
    let mut state = Params::new().hash_length(16).to_state();
    state.update(&size.to_le_bytes());
    if size == 0 {
        return state.finalize().as_bytes().try_into().unwrap();
    }
    let three_chunks = 3 * PARTIAL_CHUNK;
    if content.len() <= three_chunks {
        state.update(content);
    } else {
        state.update(&content[..PARTIAL_CHUNK]);
        let off = content.len() / 2 - PARTIAL_CHUNK / 2;
        state.update(&content[off..off + PARTIAL_CHUNK]);
        state.update(&content[content.len() - PARTIAL_CHUNK..]);
    }
    state.finalize().as_bytes().try_into().unwrap()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Golden values computed once via Python `hashlib.blake2b`. Any change
    /// here is a partial-hash protocol break; bump HASH_VERSION first.
    const GOLDEN: &[(&[u8], &str, &str)] = &[
        // (content, partial_hex, full_hex[..16])
        (b"", "c804ce198ec337e3dc762bdd1a09aece", "0e5751c026e543b2"),
        (b"hello\n", "46816a6cc8473f6c2066ce68a9146749", "93becc6e9882211c"),
    ];

    #[test]
    fn partial_known_vectors() {
        for (content, expected_partial, _) in GOLDEN {
            let got = partial_of_bytes(content);
            assert_eq!(
                hex::encode(got),
                *expected_partial,
                "partial mismatch for {:?}",
                content,
            );
        }
    }

    #[test]
    fn partial_size_boundary_cases() {
        // Boundary: exactly 3 * PARTIAL_CHUNK (read whole).
        let content = vec![0u8; 3 * PARTIAL_CHUNK];
        assert_eq!(
            hex::encode(partial_of_bytes(&content)),
            "4171aa3da9161376ef740fb718b28553"
        );
        // One byte over (forces head + middle + tail path).
        let content = vec![0u8; 3 * PARTIAL_CHUNK + 1];
        assert_eq!(
            hex::encode(partial_of_bytes(&content)),
            "952fe5ef84a98ee64a09c3eb46f27d90"
        );
    }

    #[test]
    fn partial_1mb_pattern() {
        // 1 MB of cycling 0..255.
        let mut content = Vec::with_capacity(1024 * 1024);
        for _ in 0..4096 {
            content.extend(0u8..=255);
        }
        assert_eq!(
            hex::encode(partial_of_bytes(&content)),
            "8be41785503bf8acf9fd61c3746b9fd0"
        );
    }

    #[test]
    fn partial_file_matches_bytes() {
        // The on-disk variant must match the in-memory one for the same content.
        let tmp = std::env::temp_dir().join(format!("dx-hash-{}", std::process::id()));
        let content = b"some test content here, more than nothing.";
        std::fs::write(&tmp, content).unwrap();
        let from_file = partial(&tmp, content.len() as u64).unwrap();
        let from_mem = partial_of_bytes(content);
        assert_eq!(from_file, from_mem);
        std::fs::remove_file(&tmp).ok();
    }

    #[test]
    fn partial_large_file() {
        // >192 KB exercises the head/middle/tail seek path on a real file.
        let tmp = std::env::temp_dir().join(format!("dx-hash-large-{}", std::process::id()));
        let content: Vec<u8> = (0..512u32).flat_map(|i| i as u8..(i as u8).wrapping_add(64)).collect();
        let _ = content.len(); // pacify clippy in case it's unused
        // build deterministic ~200 KB content
        let mut data = Vec::with_capacity(200_000);
        let pattern: Vec<u8> = (0u8..=255).collect();
        while data.len() < 200_000 {
            data.extend_from_slice(&pattern);
        }
        data.truncate(200_000);
        std::fs::write(&tmp, &data).unwrap();
        let from_file = partial(&tmp, data.len() as u64).unwrap();
        let from_mem = partial_of_bytes(&data);
        assert_eq!(from_file, from_mem, "file vs bytes partial must agree above 3*chunk");
        std::fs::remove_file(&tmp).ok();
    }

    #[test]
    fn full_hash_known() {
        let tmp = std::env::temp_dir().join(format!("dx-full-{}", std::process::id()));
        std::fs::write(&tmp, b"hello\n").unwrap();
        let h = full(&tmp).unwrap();
        // First 16 hex chars of Python's full hash for "hello\n"
        assert_eq!(&hex::encode(h)[..16], "93becc6e9882211c");
        std::fs::remove_file(&tmp).ok();
    }

    #[test]
    fn merkle_empty_is_deterministic() {
        let empty: [(&str, bool, &[u8]); 0] = [];
        let a = merkle(empty);
        let empty2: [(&str, bool, &[u8]); 0] = [];
        let b = merkle(empty2);
        assert_eq!(a, b);
    }

    #[test]
    fn merkle_order_matters() {
        // The caller is responsible for sorting; with the same items in
        // different orders we expect different outputs.
        let h1: [u8; 32] = [1u8; 32];
        let h2: [u8; 32] = [2u8; 32];
        let order_a = merkle([("a", false, &h1[..]), ("b", false, &h2[..])]);
        let order_b = merkle([("b", false, &h2[..]), ("a", false, &h1[..])]);
        assert_ne!(order_a, order_b);
    }
}
