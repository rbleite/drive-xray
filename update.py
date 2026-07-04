"""Self-update from GitHub — check whether the local git clone is behind its
remote and pull. Cross-platform (plain git commands). Safe no-ops when the
project isn't a git clone or has no remote / no network.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent


def _git(args: list[str], cwd: Path = REPO_DIR, timeout: int = 30):
    git = shutil.which("git")
    if not git:
        return None
    try:
        return subprocess.run([git, *args], cwd=str(cwd),
                              capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


def is_git_clone() -> bool:
    r = _git(["rev-parse", "--is-inside-work-tree"])
    return bool(r and r.returncode == 0 and r.stdout.strip() == "true")


def has_remote() -> bool:
    r = _git(["remote"])
    return bool(r and r.returncode == 0 and r.stdout.strip())


def check_updates() -> dict:
    """Fetch and compare HEAD with the upstream. Returns dict with:
    {ok, behind, ahead, commits:[str], error}."""
    if not is_git_clone():
        return {"ok": False, "error": "não é um clone git"}
    if not has_remote():
        return {"ok": False, "error": "sem repositório remoto configurado"}
    fetch = _git(["fetch", "--quiet"], timeout=60)
    if fetch is None or fetch.returncode != 0:
        return {"ok": False, "error": "git fetch falhou (rede/SSH?)"}
    # upstream ref
    up = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    upstream = up.stdout.strip() if up and up.returncode == 0 else "origin/main"
    counts = _git(["rev-list", "--left-right", "--count", f"HEAD...{upstream}"])
    behind = ahead = 0
    if counts and counts.returncode == 0:
        parts = counts.stdout.split()
        if len(parts) == 2:
            ahead, behind = int(parts[0]), int(parts[1])
    commits = []
    if behind:
        log = _git(["log", "--oneline", "--no-decorate", f"HEAD..{upstream}"])
        if log and log.returncode == 0:
            commits = [l for l in log.stdout.splitlines() if l.strip()][:20]
    return {"ok": True, "behind": behind, "ahead": ahead,
            "commits": commits, "upstream": upstream, "error": None}


def apply_update() -> dict:
    """git pull --ff-only, then rebuild the Rust binary if its sources changed
    and cargo is available. Returns {ok, pulled, rebuilt, restart, message}."""
    if not is_git_clone() or not has_remote():
        return {"ok": False, "message": "sem git/remoto"}
    before = _git(["rev-parse", "HEAD"])
    pull = _git(["pull", "--ff-only"], timeout=120)
    if pull is None or pull.returncode != 0:
        return {"ok": False, "message": (pull.stderr if pull else "pull falhou")[:300]}
    after = _git(["rev-parse", "HEAD"])
    if before and after and before.stdout == after.stdout:
        return {"ok": True, "pulled": False, "message": "já estava atualizado"}
    # did rust sources change?
    diff = _git(["diff", "--name-only", before.stdout.strip(), "HEAD"])
    rust_changed = bool(diff and "rust/" in (diff.stdout or ""))
    rebuilt = False
    if rust_changed and (REPO_DIR / "build_rust.sh").exists() and shutil.which("cargo"):
        b = subprocess.run(["bash", str(REPO_DIR / "build_rust.sh")],
                           capture_output=True, text=True, timeout=1200)
        rebuilt = b.returncode == 0
    return {"ok": True, "pulled": True, "rebuilt": rebuilt, "restart": True,
            "message": "atualizado — reinicia a app"
                       + (" (binário recompilado)" if rebuilt else "")}
