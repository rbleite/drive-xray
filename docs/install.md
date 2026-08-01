# Installing

The [README](../README.md) covers the one-line install for Windows and macOS.
This page is everything else: manual routes, Homebrew, desktop shortcuts,
running from the CLI, and building the Rust engine from source.

## What the Windows one-liner actually does

```powershell
irm https://raw.githubusercontent.com/rbleite/drive-xray/main/install.ps1 | iex
```

Step by step, skipping anything already present:

1. **Python 3.10+** via `winget`, falling back to the official installer run
   silently with `PrependPath=1` -- the *Add Python to PATH* checkbox that is
   so easy to miss by hand. A Microsoft Store `python.exe` stub is detected and
   ignored, since it cannot actually run code.
2. **git**, via `winget` or the current Git for Windows installer. The apps use
   it to update themselves later.
3. **drive-xray and media-catalog**, cloned side by side into
   `%USERPROFILE%\tools`. That location is not arbitrary: media-catalog looks
   for drive-xray's engine there, and the shortcut script pairs them as
   siblings.
4. **Virtual environments and dependencies** for both.
5. **`dx.exe`**, the optional fast engine, from the latest release.
6. **Desktop and Start Menu shortcuts** for both apps.

Re-running is how you update: existing checkouts are pulled rather than
re-cloned, and a local edit is reported and kept, never discarded.

| Flag | Effect |
|---|---|
| `-Path "D:\apps"` | install somewhere other than `%USERPROFILE%\tools` |
| `-SkipRustEngine` | do not download `dx.exe` |
| `-SkipShortcuts` | no Desktop / Start Menu buttons |
| `-Startup` | also launch the apps at login |

If PATH still looks wrong right after Python is installed, close the window and
open a new PowerShell -- on some systems the change only reaches the next
process.

## macOS / Linux, by hand

```bash
git clone https://github.com/rbleite/drive-xray.git
cd drive-xray
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/streamlit run app.py
```

Or build a **clickable `.app` launcher** (recommended for daily use):

```bash
bash build_app.sh
open ~/Applications/drive-xray.app
```

(The launcher auto-opens your browser at http://localhost:8501, with a
real icon in the Dock and Spotlight.)


## Windows, by hand

Requirements: [Python 3.10+](https://www.python.org/downloads/) — during install, tick **"Add Python to PATH"**.

```bat
git clone https://github.com/rbleite/drive-xray.git
cd drive-xray
start.bat
```

`start.bat` creates a virtual environment, installs dependencies, and launches the UI — all in one step. Double-click it on subsequent runs.

`install.bat` (double-click) runs the same full setup as the one-line
installer, but from an existing checkout. It takes `-Path`, `-SkipRustEngine`,
`-SkipShortcuts` and `-Startup`.


> **Tip:** to index a drive from the CLI on Windows, use the `dx` command in the same terminal:
> ```bat
> .venv\Scripts\python drive_xray.py index D:\ --label "External_D"
> ```

> **Faster engine (optional):** the pure-Python engine indexes everything on
> Windows out of the box. For very large drives, download
> `dx-<version>-windows-x86_64.zip` from the
> [Releases](https://github.com/rbleite/drive-xray/releases) page, unzip
> `dx.exe` into the project folder, and the UI switches to `engine: 🦀 Rust`
> automatically — the `.db` files are byte-identical either way.

## Desktop shortcuts / start at login (Windows)

`setup_shortcuts.bat` creates launch "buttons" so you never open a terminal —
double-click it, or run from a terminal:

```bat
setup_shortcuts.bat            # Desktop + Start Menu shortcuts
setup_shortcuts.bat -Startup   # also start automatically at login
setup_shortcuts.bat -Remove    # undo everything it created
```

(The `.bat` wraps `setup_shortcuts.ps1` with `-ExecutionPolicy Bypass`, so it
works on the default Windows script policy without changing system settings.)

If [media-catalog](https://github.com/rbleite/media-catalog) is cloned next
to this project (same parent folder), it gets its own shortcuts too — its
`run.bat` serves on port 8503, so both apps can run at the same time. Use
`-MediaCatalog "C:\path\to\media-catalog"` when it lives elsewhere.

(On macOS the equivalent is `./build_app.sh`, which builds a double-click
`~/Applications/drive-xray.app` — add it to **System Settings → Login Items**
to start it at login. media-catalog ships its own `build_app.sh` as well.)


## Building the Rust engine from source

Only needed if you want to build it yourself -- the Windows installer and the
Homebrew formula both ship a prebuilt binary.

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
cd rust
rustup target add aarch64-apple-darwin x86_64-apple-darwin
cargo build --release --target aarch64-apple-darwin
cargo build --release --target x86_64-apple-darwin
lipo -create -output target/universal/dx \
    target/aarch64-apple-darwin/release/dx \
    target/x86_64-apple-darwin/release/dx
```

The Streamlit UI auto-detects the Rust binary — the sidebar shows
`engine: 🦀 Rust` instead of `🐍 Python`. The `.db` files are
byte-identical, so you can switch engines at will.


## Homebrew (macOS)

```bash
brew tap rbleite/tap
brew install drive-xray
```

This installs the universal `dx` binary plus a `drive-xray-ui`
launcher shortcut.

