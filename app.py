#!/usr/bin/env python3
"""Streamlit UI for drive-xray.

Run with:  streamlit run app.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))
from drive_xray import (
    open_db, fill_full_hashes, compute_dir_hashes, human,
    get_hash_version, HASH_VERSION, _duplicate_rows,
    compute_folder_sizes, generate_cleanup_script,
    CLEANUP_STRATEGIES, CLEANUP_ACTIONS,
    latest_snapshot_id, list_snapshots, diff_snapshots,
    registry_list, registry_remove, registry_register,
    cross_dedupe,
)

DB_DIR = Path.home() / "tools" / "drive-xray"
SCRIPT = Path(__file__).parent / "drive_xray.py"
DB_DIR.mkdir(parents=True, exist_ok=True)


# ---------- Rust binary fallback ----------
# When the Rust `dx` binary is available it produces bit-identical .db
# files but ~10× faster. Prefer it for the long-running subprocess
# commands (index / refresh / snapshot / compact). The Python script is
# always used for the in-process helpers (drive_info, dup_file_groups,
# treemap_rows, …) because no subprocess is involved there.
def _dx_command_prefix() -> list[str]:
    """Resolve the indexer command prefix once.
    Order of preference:
      1. $DRIVE_XRAY_DX env var (explicit override)
      2. ./rust/target/{universal,release}/dx adjacent to this file
      3. `dx` on PATH
      4. fall back to Python: [sys.executable, drive_xray.py]
    """
    env = os.environ.get("DRIVE_XRAY_DX")
    if env and Path(env).is_file() and os.access(env, os.X_OK):
        return [env]
    here = Path(__file__).parent / "rust" / "target"
    for cand in (here / "universal" / "dx",
                 here / "release" / "dx"):
        if cand.is_file() and os.access(cand, os.X_OK):
            return [str(cand)]
    on_path = shutil.which("dx")
    if on_path:
        return [on_path]
    return [sys.executable, str(SCRIPT)]


DX_CMD = _dx_command_prefix()
DX_IS_RUST = len(DX_CMD) == 1  # heuristic: a single token means a binary

st.set_page_config(page_title="drive-xray", layout="wide", page_icon="💾")


# ---------- i18n ----------

TRANSLATIONS = {
    "pt": {
        "indexed_drives": "Drives indexadas",
        "no_drives": "Ainda não tens drives indexadas.",
        "delete_tooltip": "Remover esta drive do índice",
        "confirm_delete": "Confirmar remoção",
        "delete_question": "Apagar o índice **{name}**?\n\nO ficheiro `.db` e os auxiliares (`-wal`, `-shm`) vão ser removidos. **Não** afecta a drive original.",
        "yes_delete": "Sim, apagar",
        "cancel": "Cancelar",
        "index_new_drive": "Indexar nova drive",
        "path_label": "Caminho",
        "label_label": "Etiqueta",
        "full_hash_label": "Hash completo (--full)",
        "full_hash_help": "Lento, mas permite comparações offline confirmadas.",
        "one_fs_label": "Apenas este filesystem (-x)",
        "one_fs_help": "Não atravessa mount points. Evita /Volumes/* e firmlinks do APFS.",
        "skip_cloud_label": "Ignorar pastas de cloud (--skip-cloud)",
        "skip_cloud_help": "Salta iCloud, OneDrive, Google Drive, Dropbox, Box, MEGA, Proton Drive, etc.",
        "index_button": "Indexar",
        "path_not_exist": "Caminho inexistente.",
        "indexing": "⏳ A indexar **{label}**…",
        "completed": "✅ {label} concluída",
        "exited_with_code": "❌ {label} saiu com código {code}",
        "clear_log": "Limpar log",
        "log": "Log",
        "no_output_yet": "(sem output ainda)",
        "main_welcome_title": "drive-xray",
        "main_welcome_body": "Indexa o teu Mac ou drives externas, encontra ficheiros e pastas duplicados, compara drives offline.\n\nComeça por **indexar uma drive** na barra lateral.",
        "no_metadata_error": "`{name}` não contém metadados de drive. Re-indexa.",
        "indexed_on": "indexada em",
        "tab_summary": "📊 Resumo",
        "tab_dupes": "🔁 Duplicados",
        "tab_compare": "⚖️ Comparar",
        "files": "Ficheiros",
        "folders": "Pastas",
        "total_size": "Tamanho total",
        "top_ext": "Top 20 extensões por tamanho",
        "ext_col": "extensão",
        "files_col": "ficheiros",
        "size_col": "tamanho",
        "ignore_smaller_mb": "Ignorar ficheiros menores que (MB)",
        "drive_not_mounted": "A drive `{root}` não está montada — só vão ser usados hashes já existentes na .db (índices feitos sem `--full` podem ter resultados incompletos).",
        "find_dupes": "Procurar duplicados",
        "calculating": "A calcular…",
        "confirming_candidates": "A confirmar candidatos com hash completo…",
        "files_hashed": "{n} ficheiros hashados.",
        "computing_merkle": "A calcular hashes Merkle das pastas…",
        "done": "Concluído.",
        "file_groups": "Grupos de ficheiros",
        "folder_groups": "Grupos de pastas",
        "wasted_space": "Espaço desperdiçado (ficheiros)",
        "duplicate_files": "Ficheiros duplicados",
        "sorted_top200": "Ordenado por espaço desperdiçado. Top 200.",
        "wasted": "desperdício",
        "groups_not_shown": "+ {n} grupos não mostrados",
        "duplicate_folders": "Pastas duplicadas",
        "identical_folders": "pastas idênticas",
        "need_two_drives": "Precisas de pelo menos duas drives indexadas para comparar.",
        "cross_title": "🔀 Duplicados entre todas as drives",
        "cross_caption": "Compara todos os índices em simultâneo. As drives não precisam de estar montadas.",
        "cross_btn": "Procurar duplicados entre todas as drives",
        "cross_groups": "Grupos",
        "cross_wasted": "Espaço desperdiçado",
        "cross_confirmed": "Confirmados (=)",
        "cross_approx": "Aproximados (≈)",
        "cross_no_results": "Nenhum duplicado entre drives encontrado com o tamanho mínimo seleccionado.",
        "cross_col_group": "#",
        "cross_col_drive": "drive",
        "cross_col_path": "caminho",
        "cross_col_size": "tamanho",
        "cross_col_match": "match",
        "cross_need_drives": "Precisa de pelo menos 2 drives indexadas.",
        "compare_with": "Comparar com",
        "minimum_mb": "Mínimo (MB)",
        "compare_button": "Comparar",
        "crosschecking": "A cruzar índices…",
        "matches": "Matches",
        "confirmed_eq": "Confirmados (=)",
        "only_in_a": "Apenas em A",
        "matching_size": "Tamanho coincidente",
        "matches_not_shown": "+ {n} matches não mostrados",
        "in_drive": "em {label}",
        "match_col": "match",
        "size_match_col": "tamanho",
        "hardlink_tag": "hardlink",
        "hash_version_mismatch": "⚠️ Versões de partial-hash diferentes (A=v{va}, B=v{vb}). Os matches podem não ser fiáveis — re-indexa ambas as drives com a versão actual (v{cur}).",
        "refresh_tooltip": "Re-indexar incrementalmente (reutiliza hashes de ficheiros inalterados)",
        "download_csv": "⬇️ Exportar CSV",
        "download_xlsx": "⬇️ Exportar Excel (XLSX)",
        "db_size": "Tamanho .db",
        "compact_button": "🧹 Compactar",
        "compact_help": "VACUUM + checkpoint WAL para libertar espaço. Sem perda de dados.",
        # treemap
        "tab_map": "🗺️ Mapa",
        "map_caption": "Treemap de utilização de disco. Cada rectângulo é uma pasta; o tamanho é proporcional ao espaço ocupado. Clica para entrar.",
        "map_min_mb": "Tamanho mínimo (MB)",
        "map_include_files": "Incluir ficheiros individuais (não só pastas)",
        "map_empty": "Sem pastas acima do tamanho mínimo. Baixa o threshold.",
        "map_legend": "A mostrar {n} elementos.",
        # cleanup
        "cleanup_title": "🧽 Assistente de limpeza",
        "cleanup_caption": "Gera um script shell com as remoções/movimentos sugeridos. **Não apaga nada automaticamente** — revê e corre manualmente.",
        "cleanup_strategy": "Qual cópia manter",
        "cleanup_action": "Acção para as outras",
        "strategy_shortest": "Caminho mais curto (recomendado)",
        "strategy_oldest": "Mais antiga (mtime)",
        "strategy_newest": "Mais recente (mtime)",
        "strategy_alphabetical": "Alfabética",
        "action_quarantine": "Mover para quarentena (~/.drive-xray-quarantine/)",
        "action_delete": "Apagar (rm — irreversível!)",
        "cleanup_generate": "Gerar plano",
        "cleanup_ready": "✅ Plano gerado: {n} acções propostas.",
        "cleanup_download": "⬇️ Descarregar script .sh",
        "cleanup_preview": "Pré-visualizar script",
        # info / rename
        "info_tooltip": "Informação e renomear",
        "drive_info_title": "Informação da drive",
        "drive_db_path": "Ficheiro .db",
        "drive_root": "Raiz",
        "drive_last_indexed": "Última indexação",
        "rename_label": "Renomear etiqueta",
        "rename_save": "Guardar",
        # snapshots / history
        "snapshot_tooltip": "Tirar novo snapshot (preserva o histórico anterior)",
        "snapshot_button": "📸 Tirar snapshot",
        "tab_history": "📅 Histórico",
        "history_no_snaps": "Esta drive ainda não tem snapshots. Indexa-a primeiro.",
        "history_title": "{n} snapshots",
        "history_taken_at": "data",
        "diff_title": "Comparar snapshots",
        "diff_from": "De",
        "diff_to": "Até",
        "diff_same": "Escolhe snapshots diferentes para o diff.",
        "diff_compute": "Calcular diff",
        "diff_added": "Adicionados",
        "diff_removed": "Removidos",
        "diff_modified": "Modificados",
        "diff_net": "Variação líquida",
        "diff_top_growth": "Top pastas por crescimento",
        "diff_top_shrink": "Top pastas por redução",
    },
    "en": {
        "indexed_drives": "Indexed drives",
        "no_drives": "No drives indexed yet.",
        "delete_tooltip": "Remove this drive's index",
        "confirm_delete": "Confirm removal",
        "delete_question": "Delete index **{name}**?\n\nThe `.db` file and its sidecars (`-wal`, `-shm`) will be removed. This does **not** affect the original drive.",
        "yes_delete": "Yes, delete",
        "cancel": "Cancel",
        "index_new_drive": "Index new drive",
        "path_label": "Path",
        "label_label": "Label",
        "full_hash_label": "Full hash (--full)",
        "full_hash_help": "Slow, but enables confirmed offline comparisons.",
        "one_fs_label": "Single filesystem only (-x)",
        "one_fs_help": "Does not cross mount points. Avoids /Volumes/* and APFS firmlinks.",
        "skip_cloud_label": "Skip cloud folders (--skip-cloud)",
        "skip_cloud_help": "Skips iCloud, OneDrive, Google Drive, Dropbox, Box, MEGA, Proton Drive, etc.",
        "index_button": "Index",
        "path_not_exist": "Path does not exist.",
        "indexing": "⏳ Indexing **{label}**…",
        "completed": "✅ {label} completed",
        "exited_with_code": "❌ {label} exited with code {code}",
        "clear_log": "Clear log",
        "log": "Log",
        "no_output_yet": "(no output yet)",
        "main_welcome_title": "drive-xray",
        "main_welcome_body": "Index your Mac or external drives, find duplicate files and folders, compare drives offline.\n\nStart by **indexing a drive** in the sidebar.",
        "no_metadata_error": "`{name}` has no drive metadata. Please re-index.",
        "indexed_on": "indexed on",
        "tab_summary": "📊 Summary",
        "tab_dupes": "🔁 Duplicates",
        "tab_compare": "⚖️ Compare",
        "files": "Files",
        "folders": "Folders",
        "total_size": "Total size",
        "top_ext": "Top 20 extensions by size",
        "ext_col": "extension",
        "files_col": "files",
        "size_col": "size",
        "ignore_smaller_mb": "Ignore files smaller than (MB)",
        "drive_not_mounted": "Drive `{root}` is not mounted — only hashes already in the .db will be used (indexes built without `--full` may have incomplete results).",
        "find_dupes": "Find duplicates",
        "calculating": "Calculating…",
        "confirming_candidates": "Confirming candidates with full hashes…",
        "files_hashed": "{n} files hashed.",
        "computing_merkle": "Computing folder Merkle hashes…",
        "done": "Done.",
        "file_groups": "File groups",
        "folder_groups": "Folder groups",
        "wasted_space": "Wasted space (files)",
        "duplicate_files": "Duplicate files",
        "sorted_top200": "Sorted by wasted space. Top 200.",
        "wasted": "wasted",
        "groups_not_shown": "+ {n} groups not shown",
        "duplicate_folders": "Duplicate folders",
        "identical_folders": "identical folders",
        "need_two_drives": "You need at least two indexed drives to compare.",
        "cross_title": "🔀 Duplicates across all drives",
        "cross_caption": "Compares all indexes at once. Drives do not need to be mounted.",
        "cross_btn": "Find duplicates across all drives",
        "cross_groups": "Groups",
        "cross_wasted": "Wasted space",
        "cross_confirmed": "Confirmed (=)",
        "cross_approx": "Approximate (≈)",
        "cross_no_results": "No cross-drive duplicates found at the selected minimum size.",
        "cross_col_group": "#",
        "cross_col_drive": "drive",
        "cross_col_path": "path",
        "cross_col_size": "size",
        "cross_col_match": "match",
        "cross_need_drives": "Need at least 2 indexed drives.",
        "compare_with": "Compare with",
        "minimum_mb": "Minimum (MB)",
        "compare_button": "Compare",
        "crosschecking": "Cross-checking indexes…",
        "matches": "Matches",
        "confirmed_eq": "Confirmed (=)",
        "only_in_a": "Only in A",
        "matching_size": "Matching size",
        "matches_not_shown": "+ {n} matches not shown",
        "in_drive": "in {label}",
        "match_col": "match",
        "size_match_col": "size",
        "hardlink_tag": "hardlink",
        "hash_version_mismatch": "⚠️ Partial-hash versions differ (A=v{va}, B=v{vb}). Matches may be unreliable — re-index both drives with the current version (v{cur}).",
        "refresh_tooltip": "Refresh incrementally (reuses hashes of unchanged files)",
        "download_csv": "⬇️ Export CSV",
        "download_xlsx": "⬇️ Export Excel (XLSX)",
        "db_size": ".db size",
        "compact_button": "🧹 Compact",
        "compact_help": "VACUUM + WAL checkpoint to reclaim space. No data loss.",
        # treemap
        "tab_map": "🗺️ Map",
        "map_caption": "Disk usage treemap. Each rectangle is a folder; size is proportional to space used. Click to drill in.",
        "map_min_mb": "Minimum size (MB)",
        "map_include_files": "Include individual files (not just folders)",
        "map_empty": "No folders above the minimum size. Lower the threshold.",
        "map_legend": "Showing {n} items.",
        # cleanup
        "cleanup_title": "🧽 Cleanup assistant",
        "cleanup_caption": "Generates a shell script of suggested removals/moves. **It does NOT delete anything automatically** — review and run manually.",
        "cleanup_strategy": "Which copy to keep",
        "cleanup_action": "What to do with the others",
        "strategy_shortest": "Shortest path (recommended)",
        "strategy_oldest": "Oldest (mtime)",
        "strategy_newest": "Newest (mtime)",
        "strategy_alphabetical": "Alphabetical",
        "action_quarantine": "Move to quarantine (~/.drive-xray-quarantine/)",
        "action_delete": "Delete (rm — irreversible!)",
        "cleanup_generate": "Generate plan",
        "cleanup_ready": "✅ Plan generated: {n} proposed actions.",
        "cleanup_download": "⬇️ Download .sh script",
        "cleanup_preview": "Preview script",
        # info / rename
        "info_tooltip": "Info & rename",
        "drive_info_title": "Drive info",
        "drive_db_path": ".db file",
        "drive_root": "Root",
        "drive_last_indexed": "Last indexed",
        "rename_label": "Rename label",
        "rename_save": "Save",
        # snapshots / history
        "snapshot_tooltip": "Take new snapshot (preserves previous history)",
        "snapshot_button": "📸 Take snapshot",
        "tab_history": "📅 History",
        "history_no_snaps": "This drive has no snapshots yet. Index it first.",
        "history_title": "{n} snapshots",
        "history_taken_at": "taken at",
        "diff_title": "Compare snapshots",
        "diff_from": "From",
        "diff_to": "To",
        "diff_same": "Pick different snapshots for the diff.",
        "diff_compute": "Compute diff",
        "diff_added": "Added",
        "diff_removed": "Removed",
        "diff_modified": "Modified",
        "diff_net": "Net change",
        "diff_top_growth": "Top folders by growth",
        "diff_top_shrink": "Top folders by shrink",
    },
}


def t(key: str, **fmt) -> str:
    lang = st.session_state.get("lang", "pt")
    s = TRANSLATIONS.get(lang, TRANSLATIONS["pt"]).get(key, key)
    return s.format(**fmt) if fmt else s


# ---------- subprocess: indexer ----------

def _spawn(cmd: list[str], label: str) -> None:
    """Launch a long-running CLI subprocess and stream its output into
    st.session_state.idx_log."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=1, text=True,
    )
    log: list[str] = []

    def reader():
        for line in iter(proc.stdout.readline, ""):
            for piece in line.replace("\r", "\n").splitlines():
                if piece.strip():
                    log.append(piece)
        proc.stdout.close()

    threading.Thread(target=reader, daemon=True).start()
    st.session_state.idx_proc = proc
    st.session_state.idx_log = log
    st.session_state.idx_label = label


def start_indexer(root: str, label: str, do_full: bool,
                  one_fs: bool, skip_cloud: bool) -> None:
    db_out = DB_DIR / f"{label}.db"
    cmd = [*DX_CMD, "index", root, "--label", label, "--db", str(db_out)]
    if do_full:
        cmd.append("--full")
    if one_fs:
        cmd.append("--one-filesystem")
    if skip_cloud:
        cmd.append("--skip-cloud")
    _spawn(cmd, label)


def start_refresh(db: Path) -> None:
    _spawn([*DX_CMD, "refresh", str(db)], db.stem)


def start_compact(db: Path) -> None:
    _spawn([*DX_CMD, "compact", str(db)], db.stem)


def start_snapshot(db: Path) -> None:
    _spawn([*DX_CMD, "snapshot", "take", str(db)], db.stem)


# ---------- db helpers ----------

def list_dbs() -> list[Path]:
    """Return all known .db files: local DB_DIR + anything in the registry."""
    known: dict[Path, None] = {}
    for p in sorted(DB_DIR.glob("*.db")):
        known[p.resolve()] = None
    for entry in registry_list():
        if entry["exists"]:
            known[entry["db"].resolve()] = None
    return sorted(known.keys())


def drive_info(db: Path) -> dict | None:
    try:
        conn = open_db(db)
        row = conn.execute(
            "SELECT label, root_path, indexed_at, total_files, total_dirs, total_size"
            " FROM drive LIMIT 1"
        ).fetchone()
        # latest snapshot stats (preferred when available)
        sid = latest_snapshot_id(conn)
        n_snapshots = conn.execute(
            "SELECT COUNT(*) FROM snapshots"
        ).fetchone()[0]
        conn.close()
    except sqlite3.DatabaseError:
        return None
    if not row:
        return None
    d = dict(zip(
        ["label", "root", "indexed_at", "files", "dirs", "size"], row
    ))
    d["snapshot_id"] = sid
    d["n_snapshots"] = n_snapshots
    return d


def treemap_rows(db: Path, min_size: int, include_files: bool = False) -> list[dict]:
    """Build rows for a plotly treemap. Only folders ≥ min_size are kept;
    their ancestors are added to keep the tree connected. Each row has
    id / parent / name / size / kind."""
    conn = open_db(db)
    sid = latest_snapshot_id(conn)
    if sid is None:
        conn.close()
        return []
    raw = list(conn.execute(
        "SELECT id, rel_path, parent_id, is_dir, size FROM entries"
        " WHERE snapshot_id=?",
        (sid,),
    ))
    conn.close()
    if not raw:
        return []
    sizes = compute_folder_sizes([(e[0], e[2], e[3], e[4]) for e in raw])
    by_id = {e[0]: e for e in raw}

    kept_ids: set[int] = set()
    keep: list[tuple] = []
    for eid, rp, pid, isdir, sz in raw:
        s = sizes.get(eid, sz or 0)
        if isdir and s >= min_size:
            kept_ids.add(eid); keep.append((eid, rp, pid, isdir, s))
        elif not isdir and include_files and s >= min_size:
            kept_ids.add(eid); keep.append((eid, rp, pid, isdir, s))

    # walk up ancestors so plotly's tree is connected
    for eid, rp, pid, isdir, s in list(keep):
        cur = pid
        while cur is not None and cur not in kept_ids:
            anc = by_id.get(cur)
            if anc is None:
                break
            a_id, a_rp, a_pid, a_isdir, a_sz = anc
            a_size = sizes.get(a_id, a_sz or 0)
            kept_ids.add(a_id)
            keep.append((a_id, a_rp, a_pid, a_isdir, a_size))
            cur = a_pid

    rows = []
    for eid, rp, pid, isdir, sz in keep:
        name = os.path.basename(rp) if rp != "." else "/"
        rows.append({
            "id": str(eid),
            "parent": str(pid) if (pid is not None and pid in kept_ids) else "",
            "name": name + ("/" if isdir else ""),
            "size": sz,
            "size_human": human(sz),
            "kind": "folder" if isdir else "file",
        })
    return rows


def _hex(b) -> str:
    """Render a BLOB (bytes) or legacy hex string as a hex string."""
    if isinstance(b, (bytes, bytearray)):
        return b.hex()
    return str(b) if b is not None else ""


def dup_file_groups(db: Path, min_size: int,
                    snapshot_id: int | None = None) -> list[dict]:
    """Return groups of duplicate files within one snapshot. Hardlink-aware."""
    conn = open_db(db)
    sid = snapshot_id if snapshot_id is not None else latest_snapshot_id(conn)
    if sid is None:
        conn.close()
        return []
    groups = conn.execute(
        "SELECT full_hash, COUNT(*) c FROM entries"
        " WHERE snapshot_id=? AND is_dir=0 AND full_hash IS NOT NULL"
        "   AND size >= ?"
        " GROUP BY full_hash HAVING c > 1",
        (sid, min_size),
    ).fetchall()
    out = []
    for fh, count in groups:
        rows = conn.execute(
            "SELECT rel_path, size, inode, device FROM entries"
            " WHERE snapshot_id=? AND full_hash=? AND is_dir=0",
            (sid, fh),
        ).fetchall()
        size = rows[0][1]
        # tag hardlinks: a row is a hardlink if an earlier row had the same (ino, dev)
        seen: set[tuple] = set()
        paths = []
        distinct_inodes: set[tuple] = set()
        for rel, _, ino, dev in rows:
            key = (ino, dev) if ino is not None else None
            is_hl = key is not None and key in seen
            paths.append({"path": rel, "hardlink": is_hl})
            if key is not None:
                seen.add(key)
                distinct_inodes.add(key)
        distinct = len(distinct_inodes) if distinct_inodes else count
        wasted = size * (distinct - 1)
        hardlinks_here = count - distinct if distinct_inodes else 0
        out.append({
            "hash": _hex(fh), "count": count, "size": size,
            "wasted": wasted, "distinct_inodes": distinct,
            "hardlinks": hardlinks_here, "paths": paths,
        })
    out.sort(key=lambda g: -g["wasted"])
    conn.close()
    return out


def dup_folder_groups(db: Path,
                      snapshot_id: int | None = None) -> list[dict]:
    conn = open_db(db)
    sid = snapshot_id if snapshot_id is not None else latest_snapshot_id(conn)
    if sid is None:
        conn.close()
        return []
    groups = conn.execute(
        "SELECT full_hash, COUNT(*) c FROM entries"
        " WHERE snapshot_id=? AND is_dir=1 AND full_hash IS NOT NULL"
        " GROUP BY full_hash HAVING c > 1 ORDER BY c DESC",
        (sid,),
    ).fetchall()
    out = []
    for fh, count in groups:
        paths = [r for (r,) in conn.execute(
            "SELECT rel_path FROM entries"
            " WHERE snapshot_id=? AND full_hash=? AND is_dir=1",
            (sid, fh),
        )]
        out.append({"hash": _hex(fh), "count": count, "paths": paths})
    conn.close()
    return out


def extension_breakdown(db: Path, limit: int = 20) -> list[tuple]:
    """Group files by extension. v3 schema has no `name` column — derive via
    a registered SQLite function on `rel_path`."""
    def _ext(rel_path):
        n = os.path.basename(rel_path or "")
        if "." in n:
            return n[n.index(".") + 1:].lower()
        return "(no ext)"
    conn = open_db(db)
    conn.create_function("ext_of", 1, _ext, deterministic=True)
    sid = latest_snapshot_id(conn)
    if sid is None:
        conn.close()
        return []
    rows = conn.execute(
        "SELECT ext_of(rel_path) AS ext, COUNT(*) c, SUM(size) s"
        " FROM entries WHERE snapshot_id=? AND is_dir=0"
        " GROUP BY ext ORDER BY s DESC LIMIT ?",
        (sid, limit),
    ).fetchall()
    conn.close()
    return rows


def build_csv(rows: list[dict]) -> bytes:
    import csv, io
    if not rows:
        return b""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def build_xlsx(rows: list[dict]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    import io
    wb = Workbook()
    ws = wb.active
    ws.title = "Duplicates"
    if rows:
        headers = list(rows[0].keys())
        ws.append(headers)
        bold = Font(bold=True)
        fill = PatternFill("solid", fgColor="EEEEEE")
        for cell in ws[1]:
            cell.font = bold
            cell.fill = fill
        for r in rows:
            ws.append([r[h] for h in headers])
        widths = {"path": 60, "hash": 28, "size_human": 12, "wasted_human": 14}
        for i, h in enumerate(headers, 1):
            col_letter = chr(64 + i) if i <= 26 else "A"
            ws.column_dimensions[col_letter].width = widths.get(h, 14)
        ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def delete_db_files(target: Path) -> None:
    """Remove the .db plus any -wal/-shm/-journal sidecars, and deregister."""
    registry_remove(target)
    target.unlink(missing_ok=True)
    for ext in ("-wal", "-shm", "-journal"):
        sib = Path(str(target) + ext)
        sib.unlink(missing_ok=True)


# ---------- sidebar ----------

# registry lookup used in both the sidebar loop and the info dialog
_reg_entries: dict = {e["db"].resolve(): e for e in registry_list()}

with st.sidebar:
    # language toggle
    cur_lang = st.session_state.get("lang", "pt")
    lc1, lc2 = st.columns(2)
    if lc1.button(
        "🇵🇹 PT", use_container_width=True,
        type="primary" if cur_lang == "pt" else "secondary",
        key="lang_pt",
    ):
        st.session_state.lang = "pt"
        st.rerun()
    if lc2.button(
        "🇬🇧 EN", use_container_width=True,
        type="primary" if cur_lang == "en" else "secondary",
        key="lang_en",
    ):
        st.session_state.lang = "en"
        st.rerun()

    st.title("💾 drive-xray")
    st.caption(f"engine: {'🦀 Rust' if DX_IS_RUST else '🐍 Python'}")

    # whether an indexer/refresher is currently running
    proc_running = (
        st.session_state.get("idx_proc") is not None
        and st.session_state.idx_proc.poll() is None
    )

    # drives list with refresh + delete buttons
    dbs = list_dbs()
    selected_db: Path | None = None

    if dbs:
        st.subheader(t("indexed_drives"))
        current_path = st.session_state.get("db_choice_path")
        # clear stale selection
        if current_path and not any(str(d) == current_path for d in dbs):
            current_path = None
            st.session_state.pop("db_choice_path", None)
        # auto-select the first if nothing chosen yet
        if current_path is None and dbs:
            current_path = str(dbs[0])
            st.session_state.db_choice_path = current_path

        for db in dbs:
            c1, c2, c3, c4, c5 = st.columns([4, 1, 1, 1, 1])
            _reg = _reg_entries.get(db.resolve(), {})
            _display_label = _reg.get("label", db.stem)
            is_current = (current_path == str(db))
            if c1.button(
                ("▶ " if is_current else "   ") + _display_label,
                key=f"sel_{db}",
                use_container_width=True,
                type="primary" if is_current else "secondary",
            ):
                st.session_state.db_choice_path = str(db)
                st.rerun()
            if c2.button(
                "ℹ️", key=f"info_{db}", help=t("info_tooltip"),
            ):
                st.session_state.pending_info = str(db)
                st.rerun()
            if c3.button(
                "📸", key=f"snap_{db}", help=t("snapshot_tooltip"),
                disabled=proc_running,
            ):
                start_snapshot(db)
                st.rerun()
            if c4.button(
                "🔄", key=f"ref_{db}", help=t("refresh_tooltip"),
                disabled=proc_running,
            ):
                start_refresh(db)
                st.rerun()
            if c5.button(
                "🗑️", key=f"del_{db}", help=t("delete_tooltip"),
            ):
                st.session_state.pending_delete = str(db)
                st.rerun()
        if current_path:
            selected_db = Path(current_path)
    else:
        st.info(t("no_drives"))

    st.divider()
    st.subheader(t("index_new_drive"))

    with st.form("idx_form"):
        new_root = st.text_input(t("path_label"), "/Volumes/")
        new_label = st.text_input(t("label_label"), "")
        do_full = st.checkbox(
            t("full_hash_label"), help=t("full_hash_help"),
        )
        one_fs = st.checkbox(
            t("one_fs_label"), value=True, help=t("one_fs_help"),
        )
        skip_cloud = st.checkbox(
            t("skip_cloud_label"), value=True, help=t("skip_cloud_help"),
        )
        submitted = st.form_submit_button(
            t("index_button"), type="primary", disabled=proc_running,
        )

    if submitted:
        expanded = str(Path(new_root).expanduser()) if new_root else ""
        if not expanded or not Path(expanded).exists():
            st.error(t("path_not_exist"))
        else:
            label = (new_label.strip()
                     or Path(expanded.rstrip("/")).name
                     or "drive")
            start_indexer(expanded, label, do_full, one_fs, skip_cloud)
            st.rerun()

    # indexer status
    proc = st.session_state.get("idx_proc")
    if proc is not None:
        log = st.session_state.idx_log
        label = st.session_state.get("idx_label", "")
        if proc.poll() is None:
            st.info(t("indexing", label=label))
            if st.button(t("cancel"), key="cancel_idx"):
                proc.terminate()
                time.sleep(0.3)
                st.rerun()
        else:
            if proc.returncode == 0:
                st.success(t("completed", label=label))
            else:
                st.error(t("exited_with_code", label=label,
                            code=proc.returncode))
            if st.button(t("clear_log"), key="clear_idx"):
                st.session_state.idx_proc = None
                st.session_state.idx_log = []
                st.rerun()
        with st.expander(t("log"), expanded=proc.poll() is None):
            st.code("\n".join(log[-15:]) or t("no_output_yet"))
        if proc.poll() is None:
            time.sleep(1)
            st.rerun()


# ---------- info / rename dialog ----------

if "pending_info" in st.session_state:
    _pending_info = st.session_state["pending_info"]

    @st.dialog(t("drive_info_title"))
    def _info_dialog():
        _db = Path(_pending_info)
        _reg = _reg_entries.get(_db.resolve(), {})
        st.markdown(f"**{t('drive_db_path')}**")
        st.code(str(_db), language=None)
        st.markdown(f"**{t('drive_root')}:** `{_reg.get('root', '?')}`")
        st.markdown(f"**{t('drive_last_indexed')}:** {_reg.get('last_indexed', '?')}")
        st.divider()
        _current = _reg.get("label", _db.stem)
        _new = st.text_input(t("rename_label"), value=_current, key="rename_inp")
        rc1, rc2 = st.columns(2)
        if rc1.button(t("rename_save"), type="primary",
                      use_container_width=True, key="rename_ok",
                      disabled=not _new.strip() or _new.strip() == _current):
            _nl = _new.strip()
            _conn = open_db(_db)
            _conn.execute("UPDATE drive SET label = ?", (_nl,))
            _conn.commit()
            _conn.close()
            registry_register(_db, _nl, Path(_reg.get("root", "/")))
            st.session_state.pop("pending_info", None)
            st.rerun()
        if rc2.button(t("cancel"), use_container_width=True, key="rename_cancel"):
            st.session_state.pop("pending_info", None)
            st.rerun()

    _info_dialog()


# ---------- delete confirmation dialog ----------

if "pending_delete" in st.session_state:
    _pending = st.session_state["pending_delete"]

    @st.dialog(t("confirm_delete"))
    def _confirm_delete_dialog():
        target = Path(_pending)
        st.markdown(t("delete_question", name=target.stem))
        c1, c2 = st.columns(2)
        if c1.button(
            t("yes_delete"), type="primary",
            use_container_width=True, key="pd_yes",
        ):
            delete_db_files(target)
            st.session_state.pop("pending_delete", None)
            if st.session_state.get("db_choice_path") == _pending:
                st.session_state.pop("db_choice_path", None)
            st.session_state.pop("dupes_ready", None)
            st.rerun()
        if c2.button(
            t("cancel"), use_container_width=True, key="pd_no",
        ):
            st.session_state.pop("pending_delete", None)
            st.rerun()

    _confirm_delete_dialog()


# ---------- main ----------

if selected_db is None:
    st.title(t("main_welcome_title"))
    st.markdown(t("main_welcome_body"))
    st.stop()

info = drive_info(selected_db)
if info is None:
    st.error(t("no_metadata_error", name=selected_db.name))
    st.stop()

st.title(f"💾 {info['label']}")
st.caption(f"`{info['root']}`  ·  {t('indexed_on')} {info['indexed_at']}")

tab_summary, tab_dupes, tab_map, tab_history, tab_compare = st.tabs(
    [t("tab_summary"), t("tab_dupes"), t("tab_map"),
     t("tab_history"), t("tab_compare")]
)

# --- Summary ---
with tab_summary:
    c1, c2, c3, c4 = st.columns([2, 2, 2, 1])
    c1.metric(t("files"), f"{info['files']:,}")
    c2.metric(t("folders"), f"{info['dirs']:,}")
    c3.metric(t("total_size"), human(info["size"] or 0))
    # db file size + compact button
    db_size = selected_db.stat().st_size if selected_db.exists() else 0
    wal = Path(str(selected_db) + "-wal")
    if wal.exists():
        db_size += wal.stat().st_size
    c4.metric(t("db_size"), human(db_size))
    if st.button(t("compact_button"), help=t("compact_help"),
                 disabled=proc_running):
        start_compact(selected_db)
        st.rerun()

    ext_rows = extension_breakdown(selected_db)
    if ext_rows:
        st.subheader(t("top_ext"))
        st.dataframe(
            [{t("ext_col"): e, t("files_col"): c, t("size_col"): human(s)}
             for e, c, s in ext_rows],
            use_container_width=True, hide_index=True,
        )

# --- Duplicates ---
with tab_dupes:
    min_size_mb = st.slider(t("ignore_smaller_mb"), 0, 500, 1)
    min_size = min_size_mb * 1024 * 1024

    root_path = Path(info["root"])
    root_mounted = root_path.exists()
    if not root_mounted:
        st.warning(t("drive_not_mounted", root=str(root_path)))

    if st.button(t("find_dupes"), type="primary"):
        with st.status(t("calculating"), expanded=True) as status:
            conn = open_db(selected_db)
            if root_mounted:
                st.write(t("confirming_candidates"))
                n = fill_full_hashes(conn, root_path, min_size)
                st.write(t("files_hashed", n=n))
            st.write(t("computing_merkle"))
            compute_dir_hashes(conn)
            conn.close()
            status.update(label=t("done"), state="complete")
        st.session_state["dupes_ready"] = str(selected_db)

    if st.session_state.get("dupes_ready") == str(selected_db):
        files = dup_file_groups(selected_db, min_size)
        folders = dup_folder_groups(selected_db)

        c1, c2, c3 = st.columns(3)
        c1.metric(t("file_groups"), f"{len(files):,}")
        c2.metric(t("folder_groups"), f"{len(folders):,}")
        c3.metric(
            t("wasted_space"),
            human(sum(g["wasted"] for g in files)),
        )

        # export buttons
        if files:
            conn = open_db(selected_db)
            export_rows = _duplicate_rows(conn, min_size)
            conn.close()
            ec1, ec2 = st.columns(2)
            ec1.download_button(
                t("download_csv"),
                data=build_csv(export_rows),
                file_name=f"{selected_db.stem}-duplicates.csv",
                mime="text/csv",
                use_container_width=True,
            )
            ec2.download_button(
                t("download_xlsx"),
                data=build_xlsx(export_rows),
                file_name=f"{selected_db.stem}-duplicates.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )

        st.subheader(t("duplicate_files"))
        st.caption(t("sorted_top200"))
        for g in files[:200]:
            hl_tag = (f"  ·  {g['hardlinks']} {t('hardlink_tag')}"
                      if g["hardlinks"] else "")
            with st.expander(
                f"{g['count']}× {human(g['size'])}  ·  "
                f"{t('wasted')} {human(g['wasted'])}{hl_tag}  ·  {g['hash'][:12]}"
            ):
                for p in g["paths"]:
                    suffix = f"   ← {t('hardlink_tag')}" if p["hardlink"] else ""
                    st.code(p["path"] + suffix, language=None)
        if len(files) > 200:
            st.caption(t("groups_not_shown", n=len(files) - 200))

        st.subheader(t("duplicate_folders"))
        for g in folders[:100]:
            with st.expander(
                f"{g['count']}× {t('identical_folders')}  ·  {g['hash'][:12]}"
            ):
                for p in g["paths"]:
                    st.code(p + "/", language=None)
        if len(folders) > 100:
            st.caption(t("groups_not_shown", n=len(folders) - 100))

        # ----- Assisted cleanup -----
        st.divider()
        st.subheader(t("cleanup_title"))
        st.caption(t("cleanup_caption"))
        cc1, cc2 = st.columns(2)
        cleanup_strategy = cc1.selectbox(
            t("cleanup_strategy"),
            options=list(CLEANUP_STRATEGIES),
            format_func=lambda s: t(f"strategy_{s}"),
        )
        cleanup_action = cc2.selectbox(
            t("cleanup_action"),
            options=list(CLEANUP_ACTIONS),
            format_func=lambda a: t(f"action_{a}"),
        )
        if st.button(t("cleanup_generate"), key="gen_cleanup"):
            script = generate_cleanup_script(
                selected_db, min_size,
                strategy=cleanup_strategy, action=cleanup_action,
            )
            st.session_state["cleanup_script"] = script
            st.session_state["cleanup_db"] = str(selected_db)

        if (st.session_state.get("cleanup_script")
                and st.session_state.get("cleanup_db") == str(selected_db)):
            script = st.session_state["cleanup_script"]
            n_actions = sum(1 for l in script.splitlines()
                            if l.startswith(("rm ", "mv ")))
            st.success(t("cleanup_ready", n=n_actions))
            st.download_button(
                t("cleanup_download"),
                data=script.encode("utf-8"),
                file_name=f"{selected_db.stem}-cleanup-{cleanup_action}.sh",
                mime="text/x-shellscript",
                type="primary",
            )
            with st.expander(t("cleanup_preview"), expanded=False):
                st.code(script, language="bash")

# --- TreeMap ---
with tab_map:
    st.caption(t("map_caption"))
    map_min_mb = st.slider(t("map_min_mb"), 1, 5000, 100, key="map_min_mb")
    map_min = map_min_mb * 1024 * 1024
    map_include_files = st.checkbox(t("map_include_files"), value=False,
                                    key="map_include_files")
    rows = treemap_rows(selected_db, map_min, map_include_files)
    if not rows:
        st.info(t("map_empty"))
    else:
        import plotly.graph_objects as go
        # ensure root row exists (some dbs have root id with rel_path=".")
        ids = [r["id"] for r in rows]
        labels = [r["name"] for r in rows]
        parents = [r["parent"] for r in rows]
        values = [r["size"] for r in rows]
        customdata = [r["size_human"] for r in rows]
        colors = [1 if r["kind"] == "folder" else 0 for r in rows]
        fig = go.Figure(go.Treemap(
            ids=ids, labels=labels, parents=parents, values=values,
            branchvalues="total",
            customdata=customdata,
            hovertemplate=("<b>%{label}</b><br>%{customdata}"
                           "<br>%{percentParent:.1%} of parent<extra></extra>"),
            marker=dict(colors=colors,
                        colorscale=[[0, "#888"], [1, "#1f77b4"]],
                        showscale=False),
            textinfo="label+value",
            texttemplate="<b>%{label}</b><br>%{customdata}",
        ))
        fig.update_layout(margin=dict(t=10, l=0, r=0, b=0), height=700)
        st.plotly_chart(fig, use_container_width=True)
        st.caption(t("map_legend", n=len(rows)))

# --- History ---
with tab_history:
    conn = open_db(selected_db)
    snaps = list_snapshots(conn)
    conn.close()

    if not snaps:
        st.info(t("history_no_snaps"))
    else:
        c1, c2 = st.columns([3, 1])
        c1.subheader(t("history_title", n=len(snaps)))
        if c2.button(t("snapshot_button"), type="primary",
                     disabled=proc_running, key="snap_btn_history"):
            start_snapshot(selected_db)
            st.rerun()

        st.dataframe(
            [
                {
                    "id": s["id"],
                    t("history_taken_at"): s["taken_at"],
                    t("files"): f"{(s['total_files'] or 0):,}",
                    t("total_size"): human(s["total_size"] or 0),
                    "label": s.get("label") or "",
                }
                for s in snaps
            ],
            use_container_width=True, hide_index=True,
        )

        if len(snaps) >= 2:
            st.divider()
            st.subheader(t("diff_title"))
            ids = [s["id"] for s in snaps]
            labels = {s["id"]: f"#{s['id']} — {s['taken_at']}" for s in snaps}
            dc1, dc2 = st.columns(2)
            from_id = dc1.selectbox(
                t("diff_from"), ids[1:],
                format_func=lambda i: labels[i],
                index=0, key="diff_from",
            )
            to_id = dc2.selectbox(
                t("diff_to"), ids,
                format_func=lambda i: labels[i],
                index=0, key="diff_to",
            )
            if from_id == to_id:
                st.warning(t("diff_same"))
            elif st.button(t("diff_compute"), type="primary",
                           key="diff_btn"):
                with st.spinner(t("calculating")):
                    d = diff_snapshots(selected_db, from_id=from_id,
                                       to_id=to_id, top_n=10)
                net = (d["added_bytes"] - d["removed_bytes"]
                       + d["modified_delta_bytes"])
                m1, m2, m3, m4 = st.columns(4)
                m1.metric(t("diff_added"), f"{d['added_count']:,}",
                          f"+{human(d['added_bytes'])}")
                m2.metric(t("diff_removed"), f"{d['removed_count']:,}",
                          f"−{human(d['removed_bytes'])}",
                          delta_color="inverse")
                m3.metric(t("diff_modified"), f"{d['modified_count']:,}",
                          f"{'+' if d['modified_delta_bytes']>=0 else '−'}{human(abs(d['modified_delta_bytes']))}")
                m4.metric(t("diff_net"),
                          f"{'+' if net>=0 else '−'}{human(abs(net))}")

                gc1, gc2 = st.columns(2)
                gc1.subheader(t("diff_top_growth"))
                gc1.dataframe(
                    [{"folder": k + "/", "Δ": human(v)}
                     for k, v in d["top_growth"] if v > 0] or
                    [{"folder": "—", "Δ": "—"}],
                    use_container_width=True, hide_index=True,
                )
                if any(v < 0 for _, v in d["top_shrink"]):
                    gc2.subheader(t("diff_top_shrink"))
                    gc2.dataframe(
                        [{"folder": k + "/", "Δ": f"−{human(-v)}"}
                         for k, v in d["top_shrink"] if v < 0],
                        use_container_width=True, hide_index=True,
                    )


# --- Compare ---
with tab_compare:
    # ── cross-drive dedupe (all drives at once) ──────────────────────────────
    st.subheader(t("cross_title"))
    st.caption(t("cross_caption"))

    if len(dbs) < 2:
        st.info(t("cross_need_drives"))
    else:
        min_size_mb_x = st.slider(
            t("minimum_mb"), 0, 500, 1, key="xdp_min"
        )
        if st.button(t("cross_btn"), type="primary", key="xdp_btn"):
            db_labels = []
            for _db in dbs:
                _reg = _reg_entries.get(_db.resolve(), {})
                db_labels.append((_db, _reg.get("label", _db.stem)))
            with st.spinner(t("calculating")):
                _xgroups = cross_dedupe(db_labels, min_size=min_size_mb_x * 1024 * 1024)
            st.session_state["xdp_groups"] = _xgroups

        if "xdp_groups" in st.session_state:
            _xgroups = st.session_state["xdp_groups"]
            if not _xgroups:
                st.info(t("cross_no_results"))
            else:
                _total_wasted = sum(g["wasted_bytes"] for g in _xgroups)
                _confirmed = sum(1 for g in _xgroups if g["confirmed"])
                _approx = len(_xgroups) - _confirmed
                xc1, xc2, xc3, xc4 = st.columns(4)
                xc1.metric(t("cross_groups"), f"{len(_xgroups):,}")
                xc2.metric(t("cross_wasted"), human(_total_wasted))
                xc3.metric(t("cross_confirmed"), f"{_confirmed:,}")
                xc4.metric(t("cross_approx"), f"{_approx:,}")

                # flatten to rows: one row per copy
                _rows = []
                for i, g in enumerate(_xgroups[:500], 1):
                    _tag = "=" if g["confirmed"] else "≈"
                    for c in g["copies"]:
                        _rows.append({
                            t("cross_col_group"): i,
                            t("cross_col_match"): _tag,
                            t("cross_col_size"): human(g["size"]),
                            t("cross_col_drive"): c["drive"],
                            t("cross_col_path"): c["path"],
                            "_bytes": g["size"],
                        })
                st.dataframe(
                    [{k: v for k, v in r.items() if k != "_bytes"} for r in _rows],
                    use_container_width=True,
                    hide_index=True,
                )
                if len(_xgroups) > 500:
                    st.caption(f"+ {len(_xgroups) - 500} {t('groups_not_shown')}")

    st.divider()

    # ── pair comparison ──────────────────────────────────────────────────────
    other_dbs = [d for d in dbs if d != selected_db]
    if not other_dbs:
        st.info(t("need_two_drives"))
    else:
        other = st.selectbox(
            t("compare_with"), other_dbs,
            format_func=lambda p: p.stem,
        )
        min_size_mb_c = st.slider(
            t("minimum_mb"), 0, 500, 1, key="cmp_min"
        )
        min_size_c = min_size_mb_c * 1024 * 1024

        if st.button(t("compare_button"), type="primary", key="cmp_btn"):
            # warn if the two indexes used different partial-hash algorithms
            ca_v = open_db(selected_db)
            cb_v = open_db(other)
            va = get_hash_version(ca_v); vb = get_hash_version(cb_v)
            ca_v.close(); cb_v.close()
            if va != vb:
                st.warning(t("hash_version_mismatch",
                             va=va, vb=vb, cur=HASH_VERSION))
            with st.spinner(t("crosschecking")):
                b_index: dict[tuple[int, str], list[tuple[str, str | None]]] = defaultdict(list)
                cb = open_db(other)
                for size, partial, rel, fh in cb.execute(
                    "SELECT size, partial_hash, rel_path, full_hash FROM entries"
                    " WHERE is_dir=0 AND size >= ? AND partial_hash IS NOT NULL",
                    (min_size_c,),
                ):
                    b_index[(size, partial)].append((rel, fh))
                cb.close()

                matches = []
                only_a = 0
                ca = open_db(selected_db)
                for size, partial, rel_a, fh_a in ca.execute(
                    "SELECT size, partial_hash, rel_path, full_hash FROM entries"
                    " WHERE is_dir=0 AND size >= ? AND partial_hash IS NOT NULL",
                    (min_size_c,),
                ):
                    hits = b_index.get((size, partial))
                    if not hits:
                        only_a += 1
                        continue
                    for rel_b, fh_b in hits:
                        if fh_a and fh_b:
                            if fh_a != fh_b:
                                continue
                            tag = "="
                        else:
                            tag = "≈"
                        matches.append({
                            t("match_col"): tag,
                            t("size_match_col"): human(size),
                            t("in_drive", label=info["label"]): rel_a,
                            t("in_drive", label=Path(other).stem): rel_b,
                            "_bytes": size,
                        })
                ca.close()

            matches.sort(key=lambda m: -m["_bytes"])
            total_bytes = sum(m["_bytes"] for m in matches)
            confirmed = sum(1 for m in matches if m[t("match_col")] == "=")

            c1, c2, c3, c4 = st.columns(4)
            c1.metric(t("matches"), f"{len(matches):,}")
            c2.metric(t("confirmed_eq"), f"{confirmed:,}")
            c3.metric(t("only_in_a"), f"{only_a:,}")
            c4.metric(t("matching_size"), human(total_bytes))

            display = [
                {k: v for k, v in m.items() if k != "_bytes"}
                for m in matches[:1000]
            ]
            st.dataframe(
                display, use_container_width=True, hide_index=True,
            )
            if len(matches) > 1000:
                st.caption(t("matches_not_shown", n=len(matches) - 1000))
