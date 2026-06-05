// drive-xray binary entry point. Delegates to the library.

// Use mimalloc as the global allocator. Hash and walk phases produce
// many short-lived Vec<u8> / String allocations across rayon threads;
// mimalloc beats the system allocator on this workload (~5-15% on
// macOS Apple Silicon).
#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

fn main() -> anyhow::Result<()> {
    drive_xray::run()
}
