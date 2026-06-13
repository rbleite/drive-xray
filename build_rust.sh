#!/usr/bin/env bash
# build_rust.sh — build the universal dx binary for macOS Apple Silicon + Intel.
#
# Usage:  bash build_rust.sh
#
# After lipo the binary is ad-hoc signed so macOS Sequoia loads the arm64
# slice natively instead of falling back to Rosetta.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUST_DIR="$SCRIPT_DIR/rust"
OUT="$RUST_DIR/target/universal/dx"

cd "$RUST_DIR"

echo "→ building arm64..."
cargo build --release --target aarch64-apple-darwin

echo "→ building x86_64..."
cargo build --release --target x86_64-apple-darwin

echo "→ creating universal binary..."
mkdir -p target/universal
lipo -create -output "$OUT" \
    target/aarch64-apple-darwin/release/dx \
    target/x86_64-apple-darwin/release/dx

echo "→ signing (ad-hoc)..."
codesign --force --sign - "$OUT"

echo ""
echo "✓ $OUT"
file "$OUT"
codesign -dv "$OUT" 2>&1 | grep -E "Format|Signature"
