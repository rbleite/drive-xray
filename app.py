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
import pandas as pd

from drive_xray import (
    open_db, fill_full_hashes, compute_dir_hashes, human,
    get_hash_version, HASH_VERSION, _duplicate_rows,
    compute_folder_sizes, generate_cleanup_script,
    CLEANUP_STRATEGIES, CLEANUP_ACTIONS,
    latest_snapshot_id, list_snapshots, diff_snapshots,
    registry_list, registry_remove, registry_register,
    cross_dedupe, read_drive_index_opts,
    verify_file, execute_file_action, QUARANTINE_DIR,
    read_config, write_config, get_db_dir, import_folder,
)

DB_DIR = get_db_dir()
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
        "cross_firmlink_warn": "ℹ️ **{drives}** — têm múltiplas cópias internas do mesmo conteúdo (backups ou cópias manuais). Os grupos marcados com ⚠️ incluem essas cópias.",
        "cross_firmlink_groups": "Grupos com cópias internas duplicadas na mesma drive:",
        "cross_matrix_title": "📊 Sobreposição entre drives",
        "cross_matrix_caption": "GB partilhados entre cada par de drives. Quanto mais escuro, mais duplicados.",
        "cross_download_csv": "⬇️ Exportar CSV (todos os grupos)",
        "cross_details": "📋 Detalhes (top 500 grupos)",
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
        # interactive delete
        "del_title": "🗑️ Apagar seleccionados",
        "del_caption": "Selecciona as cópias a eliminar (por defeito: mantém o caminho mais curto). Antes de apagar, verifica-se que a cópia a manter ainda existe em disco.",
        "del_col": "apagar",
        "del_action_label": "Acção",
        "del_verify_btn": "Verificar e apagar {n} ficheiro(s) · {size}",
        "del_none_selected": "Marca pelo menos um ficheiro para apagar.",
        "del_root_unmounted": "⚠️ Drive não montada — não é possível verificar nem apagar ficheiros.",
        "del_dialog_title": "Verificação antes de apagar",
        "del_ok_summary": "✅ {n} grupo(s) verificados · {f} ficheiro(s) · {size} a libertar",
        "del_no_keeper": "🚫 {n} grupo(s) ignorados — todas as cópias marcadas (ficaria sem nenhuma)",
        "del_keeper_missing": "⚠️ {n} grupo(s) ignorados — cópia a manter não encontrada em disco",
        "del_already_gone": "ℹ️ {n} ficheiro(s) já não existem — serão ignorados",
        "del_nothing_to_do": "Nenhum ficheiro verificado para apagar.",
        "del_detail": "Ver detalhes por grupo",
        "del_keeper_label": "manter",
        "del_execute_btn": "Executar · {n} ficheiro(s) · {size}",
        "del_done": "✅ {n} ficheiro(s) processados · {size} libertados",
        "del_errors": "❌ {n} erro(s)",
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
        # settings / sync
        "settings_title": "⚙️ Configurações",
        "settings_db_dir": "Pasta dos índices (.db)",
        "settings_db_dir_help": "Pasta onde são guardados os ficheiros .db. Usa uma pasta do OneDrive/Google Drive/Dropbox para partilha automática entre máquinas.",
        "settings_save": "Guardar",
        "settings_saved": "✅ Pasta actualizada. A reiniciar…",
        "settings_invalid": "Pasta inválida ou sem permissão de escrita.",
        "settings_import_btn": "Importar .db desta pasta",
        "settings_import_help": "Regista todos os ficheiros .db encontrados na pasta configurada.",
        "settings_imported": "✅ {n} drive(s) importada(s) · {s} já existiam",
        "settings_import_none": "Nenhum .db válido encontrado nessa pasta.",
        "settings_current_dir": "Pasta actual",
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
        "cross_firmlink_warn": "ℹ️ **{drives}** — have multiple internal copies of the same content (backups or manual copies). Groups marked ⚠️ include these copies.",
        "cross_firmlink_groups": "Groups with internal duplicate copies on the same drive:",
        "cross_matrix_title": "📊 Drive overlap",
        "cross_matrix_caption": "Shared GB between each pair of drives. Darker = more duplicates.",
        "cross_download_csv": "⬇️ Export CSV (all groups)",
        "cross_details": "📋 Details (top 500 groups)",
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
        # interactive delete
        "del_title": "🗑️ Delete selected",
        "del_caption": "Select which copies to remove (default: keep shortest path). Before deleting, the tool verifies the copy to keep still exists on disk.",
        "del_col": "delete",
        "del_action_label": "Action",
        "del_verify_btn": "Verify and delete {n} file(s) · {size}",
        "del_none_selected": "Mark at least one file for deletion.",
        "del_root_unmounted": "⚠️ Drive not mounted — cannot verify or delete files.",
        "del_dialog_title": "Pre-delete verification",
        "del_ok_summary": "✅ {n} group(s) verified · {f} file(s) · {size} to free",
        "del_no_keeper": "🚫 {n} group(s) skipped — all copies marked (would leave zero copies)",
        "del_keeper_missing": "⚠️ {n} group(s) skipped — keeper not found on disk",
        "del_already_gone": "ℹ️ {n} file(s) already gone — will be skipped",
        "del_nothing_to_do": "No files verified for deletion.",
        "del_detail": "Show details per group",
        "del_keeper_label": "keep",
        "del_execute_btn": "Execute · {n} file(s) · {size}",
        "del_done": "✅ {n} file(s) processed · {size} freed",
        "del_errors": "❌ {n} error(s)",
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
        # settings / sync
        "settings_title": "⚙️ Settings",
        "settings_db_dir": "Index folder (.db files)",
        "settings_db_dir_help": "Folder where .db files are saved. Point to a OneDrive/Google Drive/Dropbox folder for automatic multi-machine sync.",
        "settings_save": "Save",
        "settings_saved": "✅ Folder updated. Restarting…",
        "settings_invalid": "Invalid folder or no write permission.",
        "settings_import_btn": "Import .db files from this folder",
        "settings_import_help": "Register all valid .db files found in the configured folder.",
        "settings_imported": "✅ {n} drive(s) imported · {s} already existed",
        "settings_import_none": "No valid .db files found in that folder.",
        "settings_current_dir": "Current folder",
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
    """Return groups of duplicate files within one snapshot. Hardlink-aware.

    Uses a single JOIN query (no N+1) to fetch all candidate entries at once,
    then groups in Python. Drive need not be mounted.

    confirmed=True  (=)  all copies share the same full_hash
    confirmed=False (≈)  full_hash not yet computed for some/all copies
    Partial collisions (same partial, different full) are silently skipped.
    """
    conn = open_db(db)
    sid = snapshot_id if snapshot_id is not None else latest_snapshot_id(conn)
    if sid is None:
        conn.close()
        return []

    # Single query: fetch every file that belongs to a candidate group.
    # The inner SELECT identifies (size, partial_hash) pairs with >1 entry;
    # the outer JOIN retrieves all their fields in one round-trip.
    rows = conn.execute(
        "SELECT e.size, e.partial_hash, e.rel_path, e.inode, e.device, e.full_hash"
        " FROM entries e"
        " JOIN ("
        "   SELECT size, partial_hash FROM entries"
        "   WHERE snapshot_id=? AND is_dir=0"
        "     AND partial_hash IS NOT NULL AND size>=?"
        "   GROUP BY size, partial_hash HAVING COUNT(*)>1"
        " ) c ON e.size=c.size AND e.partial_hash=c.partial_hash"
        " WHERE e.snapshot_id=? AND e.is_dir=0",
        (sid, min_size, sid),
    ).fetchall()
    conn.close()

    # Group rows by (size, partial_hash) in Python
    by_key: dict[tuple, list] = defaultdict(list)
    for size, partial, rel, ino, dev, fh in rows:
        by_key[(size, partial)].append((rel, ino, dev, fh))

    out: list[dict] = []
    for (size, partial), members in by_key.items():
        # inode-dedup: hardlinks share storage, count only once
        seen_inodes: set[tuple] = set()
        deduped: list[tuple] = []
        hardlink_count = 0
        for rel, ino, dev, fh in members:
            key = (ino, dev) if ino is not None and dev is not None else None
            if key and key in seen_inodes:
                hardlink_count += 1
            else:
                if key:
                    seen_inodes.add(key)
                deduped.append((rel, fh))

        if len(deduped) < 2:
            continue

        fhs = [fh for _, fh in deduped if fh is not None]
        all_have_fh = len(fhs) == len(deduped)

        if all_have_fh:
            # sub-group by full_hash: each matching sub-group is a confirmed dup
            by_fh: dict = defaultdict(list)
            for rel, fh in deduped:
                by_fh[fh].append(rel)
            for fh, grp_paths in by_fh.items():
                if len(grp_paths) < 2:
                    continue  # partial collision — different content, skip
                wasted = size * (len(grp_paths) - 1)
                out.append({
                    "hash": _hex(fh),
                    "count": len(grp_paths),
                    "size": size,
                    "wasted": wasted,
                    "distinct_inodes": len(grp_paths),
                    "hardlinks": hardlink_count,
                    "paths": [{"path": p, "hardlink": False} for p in grp_paths],
                    "confirmed": True,
                })
        else:
            # approximate: full_hash not yet available for all copies
            wasted = size * (len(deduped) - 1)
            out.append({
                "hash": _hex(partial),
                "count": len(deduped),
                "size": size,
                "wasted": wasted,
                "distinct_inodes": len(deduped),
                "hardlinks": hardlink_count,
                "paths": [{"path": p, "hardlink": False} for p, _ in deduped],
                "confirmed": False,
            })

    out.sort(key=lambda g: -g["wasted"])
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

    # ---------- settings (db folder / cloud sync) ----------
    st.divider()
    with st.expander(t("settings_title")):
        _cfg = read_config()
        _cur_dir = str(get_db_dir())
        st.caption(f"{t('settings_current_dir')}: `{_cur_dir}`")

        _new_dir = st.text_input(
            t("settings_db_dir"),
            value=_cur_dir,
            help=t("settings_db_dir_help"),
            key="cfg_db_dir_input",
        )
        _scol1, _scol2 = st.columns(2)
        if _scol1.button(t("settings_save"), use_container_width=True,
                         key="cfg_save_btn"):
            _p = Path(_new_dir).expanduser()
            try:
                _p.mkdir(parents=True, exist_ok=True)
                _ = (_p / ".dx_write_test").write_text("ok")
                (_p / ".dx_write_test").unlink(missing_ok=True)
                _cfg["db_dir"] = str(_p.resolve())
                write_config(_cfg)
                st.success(t("settings_saved"))
                time.sleep(0.8)
                st.rerun()
            except Exception:
                st.error(t("settings_invalid"))

        if _scol2.button(t("settings_import_btn"), use_container_width=True,
                         key="cfg_import_btn",
                         help=t("settings_import_help")):
            _imp = import_folder(Path(_new_dir))
            _new_count = sum(1 for r in _imp if not r["already_registered"])
            _old_count = sum(1 for r in _imp if r["already_registered"])
            if _imp:
                st.success(t("settings_imported", n=_new_count, s=_old_count))
                time.sleep(0.8)
                st.rerun()
            else:
                st.warning(t("settings_import_none"))


# ---------- pre-delete verification dialog ----------

if "del_plan" in st.session_state:
    _dplan = st.session_state["del_plan"]
    _daction = st.session_state.get("del_action", "quarantine")
    _droot = Path(st.session_state.get("del_root_path",
                  st.session_state.get("db_choice_path", "/")))

    @st.dialog(t("del_dialog_title"), width="large")
    def _del_dialog():
        _ok_grps   = [g for g in _dplan if g["status"] == "ok"]
        _no_keeper = [g for g in _dplan if g["status"] == "no_keeper"]
        _miss      = [g for g in _dplan if g["status"] == "keeper_missing"]
        _exec_files = [d for g in _ok_grps for d in g["to_delete"] if d["exists"]]
        _gone_files = [d for g in _ok_grps for d in g["to_delete"] if not d["exists"]]
        _bytes_free = sum(d["size"] for d in _exec_files)

        if _exec_files:
            st.success(t("del_ok_summary", n=len(_ok_grps),
                         f=len(_exec_files), size=human(_bytes_free)))
        if _no_keeper:
            st.error(t("del_no_keeper", n=len(_no_keeper)))
        if _miss:
            st.warning(t("del_keeper_missing", n=len(_miss)))
        if _gone_files:
            st.info(t("del_already_gone", n=len(_gone_files)))

        with st.expander(t("del_detail"), expanded=len(_ok_grps) <= 10):
            for _g in _ok_grps:
                _k = _g["keeper"]
                _ico = "✅" if _k["ok"] else "⚠️"
                st.markdown(
                    f"**#{_g['group']}** — {_ico} `{_k['path']}` "
                    f"*({t('del_keeper_label')})*"
                )
                for _d in _g["to_delete"]:
                    _di = "🗑️" if _d["exists"] else "👻"
                    st.markdown(f"  {_di} `{_d['path']}`"
                                + ("" if _d["exists"] else " *(já eliminado)*"))

        if not _exec_files:
            st.warning(t("del_nothing_to_do"))
            if st.button(t("cancel"), use_container_width=True, key="dd_cancel"):
                st.session_state.pop("del_plan", None)
                st.rerun()
        else:
            c1, c2 = st.columns(2)
            if c1.button(t("cancel"), use_container_width=True, key="dd_cancel"):
                st.session_state.pop("del_plan", None)
                st.rerun()
            if c2.button(
                t("del_execute_btn", n=len(_exec_files), size=human(_bytes_free)),
                type="primary", use_container_width=True, key="dd_exec",
            ):
                _results = [execute_file_action(d["full_path"], _daction)
                            for d in _exec_files]
                _ok_count = sum(1 for r in _results if r["ok"])
                _freed = sum(d["size"] for d, r in zip(_exec_files, _results)
                             if r["ok"])
                st.session_state["del_result"] = {
                    "ok": _ok_count,
                    "freed_bytes": _freed,
                    "errors": [r for r in _results if not r["ok"]],
                }
                st.session_state.pop("del_plan", None)
                st.session_state.pop("dupes_ready", None)  # force re-scan
                st.rerun()

    _del_dialog()


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
        st.info(t("drive_not_mounted", root=str(root_path)))

    if st.button(t("find_dupes"), type="primary"):
        # clear cached results so we recompute after fill_full_hashes
        for k in list(st.session_state.keys()):
            if k.startswith("dupes_cache_"):
                del st.session_state[k]
        with st.status(t("calculating"), expanded=True) as status:
            conn = open_db(selected_db)
            if root_mounted:
                # confirm candidates: upgrades ≈ → = for files on this drive
                st.write(t("confirming_candidates"))
                n = fill_full_hashes(conn, root_path, min_size)
                st.write(t("files_hashed", n=n))
            else:
                st.write("A usar partial_hash (≈) — drive não montada.")
            st.write(t("computing_merkle"))
            compute_dir_hashes(conn)
            conn.close()
            status.update(label=t("done"), state="complete")
        st.session_state["dupes_ready"] = str(selected_db)

    if st.session_state.get("dupes_ready") == str(selected_db):
        # cache results — recompute only when db or min_size changes
        _cache_key = f"dupes_cache_{selected_db}_{min_size}"
        if _cache_key not in st.session_state:
            st.session_state[_cache_key] = (
                dup_file_groups(selected_db, min_size),
                dup_folder_groups(selected_db),
            )
        files, folders = st.session_state[_cache_key]

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
            match_tag = "=" if g.get("confirmed") else "≈"
            with st.expander(
                f"{match_tag} {g['count']}× {human(g['size'])}  ·  "
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

        # ----- Interactive delete -----
        st.divider()
        st.subheader(t("del_title"))
        st.caption(t("del_caption"))

        if not root_mounted:
            st.warning(t("del_root_unmounted"))
        elif files:
            # Build editor dataframe — cap at 200 groups (same as display)
            _del_rows = []
            for _gi, _g in enumerate(files[:200], 1):
                _sorted_paths = sorted(_g["paths"], key=lambda p: len(p["path"]))
                for _pi, _p in enumerate(_sorted_paths):
                    _del_rows.append({
                        t("del_col"): _pi > 0,      # keep shortest, mark rest
                        "#": _gi,
                        t("cross_col_size"): human(_g["size"]),
                        t("cross_col_path"): _p["path"],
                        "_size": _g["size"],
                    })
            _del_df = pd.DataFrame(_del_rows)
            _edited = st.data_editor(
                _del_df,
                column_config={
                    t("del_col"): st.column_config.CheckboxColumn(
                        t("del_col"), default=False),
                    "#": st.column_config.NumberColumn(
                        "#", disabled=True, width="small"),
                    t("cross_col_size"): st.column_config.TextColumn(
                        t("cross_col_size"), disabled=True, width="small"),
                    t("cross_col_path"): st.column_config.TextColumn(
                        t("cross_col_path"), disabled=True, width="large"),
                    "_size": None,
                },
                disabled=["#", t("cross_col_size"), t("cross_col_path")],
                hide_index=True,
                use_container_width=True,
                key="del_editor",
            )

            _del_action = st.selectbox(
                t("del_action_label"),
                options=list(CLEANUP_ACTIONS),
                format_func=lambda a: t(f"action_{a}"),
                key="del_action_sel",
            )

            _marked = _edited[_edited[t("del_col")] == True]
            _n_marked = len(_marked)
            _bytes_marked = int(_marked["_size"].sum()) if _n_marked else 0

            if _n_marked == 0:
                st.caption(t("del_none_selected"))
            else:
                if st.button(
                    t("del_verify_btn", n=_n_marked, size=human(_bytes_marked)),
                    type="primary", key="del_verify_btn",
                ):
                    _plan = []
                    for _gid in _marked["#"].unique():
                        _gdf = _edited[_edited["#"] == _gid]
                        _keepers = _gdf[_gdf[t("del_col")] == False]
                        _to_del = _gdf[_gdf[t("del_col")] == True]

                        if _keepers.empty:
                            _plan.append({"group": int(_gid),
                                          "status": "no_keeper",
                                          "keeper": None, "to_delete": []})
                            continue

                        _kr = _keepers.iloc[0]
                        _kcheck = verify_file(
                            root_path, _kr[t("cross_col_path")], int(_kr["_size"]))
                        _del_checks = []
                        for _, _dr in _to_del.iterrows():
                            _dc = verify_file(
                                root_path, _dr[t("cross_col_path")], int(_dr["_size"]))
                            _del_checks.append({
                                "path": _dr[t("cross_col_path")],
                                "full_path": _dc["full_path"],
                                "exists": _dc["ok"],
                                "size": int(_dr["_size"]),
                            })
                        _plan.append({
                            "group": int(_gid),
                            "status": "ok" if _kcheck["ok"] else "keeper_missing",
                            "keeper": {"path": _kr[t("cross_col_path")],
                                       "full_path": _kcheck["full_path"],
                                       "ok": _kcheck["ok"],
                                       "reason": _kcheck["reason"]},
                            "to_delete": _del_checks,
                        })
                    st.session_state["del_plan"] = _plan
                    st.session_state["del_action"] = _del_action
                    st.session_state["del_root_path"] = str(root_path)
                    st.rerun()

        # show execution result banner
        if "del_result" in st.session_state:
            _res = st.session_state.pop("del_result")
            st.success(t("del_done", n=_res["ok"],
                         size=human(_res["freed_bytes"])))
            if _res["errors"]:
                st.error(t("del_errors", n=len(_res["errors"])))

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
                # ── metrics ─────────────────────────────────────────────────
                _total_wasted = sum(g["wasted_bytes"] for g in _xgroups)
                _confirmed = sum(1 for g in _xgroups if g["confirmed"])
                _approx = len(_xgroups) - _confirmed
                xc1, xc2, xc3, xc4 = st.columns(4)
                xc1.metric(t("cross_groups"), f"{len(_xgroups):,}")
                xc2.metric(t("cross_wasted"), human(_total_wasted))
                xc3.metric(t("cross_confirmed"), f"{_confirmed:,}")
                xc4.metric(t("cross_approx"), f"{_approx:,}")

                # ── firmlink / no-one-fs warning ─────────────────────────────
                _intra_drives: set[str] = set()
                _intra_groups: list[int] = []
                for _gi2, _g2 in enumerate(_xgroups, 1):
                    if _g2.get("intra_drives"):
                        _intra_drives.update(_g2["intra_drives"])
                        _intra_groups.append(_gi2)

                _no_x_drives: set[str] = {
                    lbl for lbl, opts in read_drive_index_opts(db_labels).items()
                    if not opts.get("one_fs")
                }
                _suspect = _intra_drives | _no_x_drives
                if _suspect:
                    st.info(t("cross_firmlink_warn",
                               drives=", ".join(f"**{d}**" for d in sorted(_suspect))))
                    if _intra_groups:
                        with st.expander(t("cross_firmlink_groups"), expanded=False):
                            st.write(f"Grupos: {_intra_groups[:50]}"
                                     + (f" … +{len(_intra_groups)-50}" if len(_intra_groups) > 50 else ""))

                # ── N×N heatmap ──────────────────────────────────────────────
                st.subheader(t("cross_matrix_title"))
                st.caption(t("cross_matrix_caption"))

                # collect unique drive labels that appear in results
                _drive_set: set[str] = set()
                for _g in _xgroups:
                    for _c in _g["copies"]:
                        _drive_set.add(_c["drive"])
                _drive_order = sorted(_drive_set)
                _didx = {d: i for i, d in enumerate(_drive_order)}
                _n = len(_drive_order)

                # build symmetric matrix (bytes)
                _mat = [[0.0] * _n for _ in range(_n)]
                for _g in _xgroups:
                    _ud = list({_c["drive"] for _c in _g["copies"]})
                    for _i in range(len(_ud)):
                        for _j in range(_i + 1, len(_ud)):
                            _di, _dj = _didx[_ud[_i]], _didx[_ud[_j]]
                            _mat[_di][_dj] += _g["size"]
                            _mat[_dj][_di] += _g["size"]

                # convert to GB, mask diagonal as None (shows as grey)
                _mat_gb = [
                    [None if _i == _j else _mat[_i][_j] / 1e9
                     for _j in range(_n)]
                    for _i in range(_n)
                ]
                _text = [
                    ["" if _i == _j
                     else (f"{_mat[_i][_j]/1e9:.1f} GB"
                           if _mat[_i][_j] > 0 else "0")
                     for _j in range(_n)]
                    for _i in range(_n)
                ]

                import plotly.graph_objects as go
                _fig = go.Figure(go.Heatmap(
                    z=_mat_gb,
                    x=_drive_order,
                    y=_drive_order,
                    colorscale="Blues",
                    showscale=True,
                    colorbar=dict(title="GB"),
                    text=_text,
                    texttemplate="%{text}",
                    hovertemplate=(
                        "%{y} ↔ %{x}<br>%{text}<extra></extra>"
                    ),
                ))
                _fig.update_layout(
                    height=max(300, 80 * _n),
                    margin=dict(l=10, r=10, t=10, b=10),
                    xaxis=dict(side="bottom"),
                )
                st.plotly_chart(_fig, use_container_width=True)

                # ── flat table + CSV download ─────────────────────────────────
                st.subheader(t("cross_details"))

                import csv, io as _io
                _csv_buf = _io.StringIO()
                _csv_w = csv.writer(_csv_buf)
                _csv_w.writerow(["group", "match", "size_bytes", "size_human",
                                 "drive", "path"])
                _rows = []
                for _i, _g in enumerate(_xgroups, 1):
                    _tag = "=" if _g["confirmed"] else "≈"
                    _has_intra = bool(_g.get("intra_drives"))
                    _intra_set = set(_g.get("intra_drives", []))
                    for _c in _g["copies"]:
                        _drive_label = (_c["drive"] + " ⚠️"
                                        if _c["drive"] in _intra_set else _c["drive"])
                        _match_tag = _tag + (" ⚠️" if _has_intra else "")
                        _row = {
                            t("cross_col_group"): _i,
                            t("cross_col_match"): _match_tag,
                            t("cross_col_size"): human(_g["size"]),
                            t("cross_col_drive"): _drive_label,
                            t("cross_col_path"): _c["path"],
                        }
                        _rows.append(_row)
                        _csv_w.writerow([_i, _match_tag, _g["size"],
                                         human(_g["size"]),
                                         _c["drive"], _c["path"]])

                st.download_button(
                    t("cross_download_csv"),
                    data=_csv_buf.getvalue().encode(),
                    file_name="cross-dedupe.csv",
                    mime="text/csv",
                    key="xdp_csv",
                )
                st.dataframe(
                    _rows[:500 * 10],  # rows are per-copy, groups×copies
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
