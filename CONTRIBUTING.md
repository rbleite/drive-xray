# Contributing to drive-xray

Thanks for considering contributing. The project is small and pragmatic
and intends to stay that way — please skim this whole file before
opening a PR; it'll save us both time.

## Filing a bug report

The single most useful thing in a bug report is **enough information to
reproduce the bug without asking follow-up questions**. Concretely:

1. **Environment** — macOS version (`sw_vers -productVersion`), CPU
   architecture (`arch`), and `dx --version` output.
2. **What you ran** — exact command (`dx index ... -x` or the UI click
   path).
3. **What you expected vs what happened** — error message verbatim,
   not paraphrased.
4. **The relevant slice of the `.db`** if possible. SQLite dbs are
   easy to share: `sqlite3 mydrive.db ".dump entries WHERE rel_path LIKE '%suspect%'"`
   and paste the output (don't share whole multi-GB dbs).

Open issues at https://github.com/rbleite/drive-xray/issues.

## Asking a question / proposing a feature

Use **Discussions** at https://github.com/rbleite/drive-xray/discussions
for open-ended questions, "would feature X make sense?", or "how do I
do Y?". Issues are for confirmed bugs or accepted feature work.

## Development setup

```bash
git clone https://github.com/rbleite/drive-xray.git
cd drive-xray

# Python side
python3 -m venv .venv
.venv/bin/pip install streamlit openpyxl plotly

# Rust side (optional, only if you're touching the dx binary)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
. "$HOME/.cargo/env"
rustup target add aarch64-apple-darwin x86_64-apple-darwin
```

## Running tests

```bash
# Rust unit + integration tests
cd rust
cargo test --release

# Includes the cross-implementation parity suite, which auto-skips
# when the Python venv isn't present.
```

CI runs both the standard test suite and the parity suite on every
push and PR. A green CI is a hard requirement before merge.

## Code style

- **Python**: PEP 8 with 88-column soft limit. Type hints are
  encouraged on public functions; not religious about them.
- **Rust**: `cargo fmt` and `cargo clippy -- -W clippy::pedantic` clean.
  We accept allow-listed warnings if there's a comment justifying them.
- **SQL**: lowercase keywords are fine but please keep the schema
  string `SCHEMA` aligned with the formatted style already there.
- **Comments**: explain *why*, not *what*. The code already says what
  it does; comments should add context that isn't obvious.
- **Commit messages**: imperative mood ("add foo", not "added foo" or
  "adds foo"), 60-char subject, paragraph body if it warrants one.

## Cross-implementation parity

`drive_xray.py` and the Rust binary in `rust/` must produce
byte-identical `.db` files for the same input. If your change affects
either engine, run the parity suite locally before pushing:

```bash
cd rust && cargo test --release --test parity
```

If a parity test starts failing, the fix is usually to bring the other
engine in line — not to relax the test.

## Submitting changes

1. Open a PR against `main`. Small, focused PRs land faster than big
   omnibus ones.
2. Describe what changed and why. Include screenshots if you're
   touching the UI.
3. Make sure CI passes. If a parity test fails after your change,
   fix it before requesting review.
4. Update `CHANGELOG.md` under `[Unreleased]` with a brief note.

## Licensing

By contributing, you agree that your work is licensed under
[Apache-2.0](LICENSE), same as the rest of the project. The
[`NOTICE`](NOTICE) file lists the project and its third-party
dependencies; if your contribution introduces a new dependency, please
add it there.
