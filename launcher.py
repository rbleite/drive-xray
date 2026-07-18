"""Launch the sibling media-catalog app from the drive-xray UI.

Finds the local media-catalog clone (configurable via the "media_catalog_dir"
config key), starts it detached on its own port and reports whether it is
already running. Cross-platform; safe no-ops when nothing is found.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from drive_xray import read_config, write_config

REPO_DIR = Path(__file__).resolve().parent
DEFAULT_PORT = 8502

# where to look when no path is configured yet
_CANDIDATES = (
    REPO_DIR.parent / "media-catalog",
    Path.home() / "media-catalog",
    Path.home() / "Projects" / "media-catalog",
    Path.home() / "projects" / "media-catalog",
    Path.home() / "Developer" / "media-catalog",
    Path.home() / "dev" / "media-catalog",
)


def get_port() -> int:
    try:
        return int(read_config().get("media_catalog_port", DEFAULT_PORT))
    except Exception:
        return DEFAULT_PORT


def find_media_catalog() -> Path | None:
    cfg = read_config().get("media_catalog_dir")
    if cfg:
        p = Path(cfg).expanduser()
        if p.is_dir():
            return p
    for cand in _CANDIDATES:
        if cand.is_dir():
            return cand
    return None


def save_media_catalog_dir(path: str) -> None:
    cfg = read_config()
    cfg["media_catalog_dir"] = str(Path(path).expanduser())
    write_config(cfg)


def is_running(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _entry_command(folder: Path, port: int) -> list[str] | None:
    """Prefer the project's own start script (it knows best how to run
    itself); fall back to `streamlit run` on a conventional entry file."""
    if os.name == "nt":
        for name in ("start.bat", "run.bat"):
            if (folder / name).exists():
                return ["cmd", "/c", str(folder / name)]
    else:
        for name in ("start.sh", "run.sh"):
            if (folder / name).exists():
                return ["bash", str(folder / name)]
    for name in ("app.py", "streamlit_app.py", "Home.py", "main.py"):
        if (folder / name).exists():
            return [sys.executable, "-m", "streamlit", "run", name,
                    "--server.port", str(port), "--server.headless", "true"]
    return None


def launch(folder: Path, port: int, wait: float = 20.0) -> dict:
    """Start media-catalog detached and wait until its port answers.
    Returns {ok, message}."""
    if is_running(port):
        return {"ok": True, "message": "já está a correr"}
    cmd = _entry_command(folder, port)
    if cmd is None:
        return {"ok": False,
                "message": "sem entrypoint conhecido (start.sh, run.sh, app.py…)"}
    log = folder / "media-catalog.launch.log"
    kwargs: dict = {}
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        kwargs["creationflags"] = 0x00000208
    else:
        kwargs["start_new_session"] = True
    try:
        with open(log, "ab") as fh:
            proc = subprocess.Popen(cmd, cwd=str(folder), stdout=fh, stderr=fh,
                                    stdin=subprocess.DEVNULL, **kwargs)
    except Exception as e:
        return {"ok": False, "message": f"falhou a arrancar: {e}"[:300]}
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if is_running(port):
            return {"ok": True, "message": "arrancado"}
        if proc.poll() is not None:
            return {"ok": False,
                    "message": f"o processo terminou (código {proc.returncode})"
                               f" — vê {log.name}"}
        time.sleep(0.5)
    return {"ok": False,
            "message": f"não respondeu na porta {port} em {int(wait)}s"
                       f" — vê {log.name}"}
