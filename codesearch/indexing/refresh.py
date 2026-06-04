from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from codesearch.config import settings
from codesearch.db.fts import rebuild_repo_fts
from codesearch.indexing.classify_paths import audit_path, classify_path, normalize_rel_path
from codesearch.indexing.discovery import discover_files
from codesearch.indexing.gitignore import refresh as refresh_gitignore
from codesearch.indexing.heuristics import apply_heuristics
from codesearch.indexing.tree_sitter_extract import extract_file
from codesearch.model.embed import delete_vectors
from codesearch.model.enrich import queue_model_jobs


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def git_head(root: Path) -> str | None:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True)
        return result.stdout.strip()
    except Exception:
        return None


def register_repo(conn: sqlite3.Connection, repo_path: str, slug: str | None = None) -> dict:
    root = Path(repo_path).expanduser().resolve()
    if not root.is_dir():
        return {"ok": False, "error": "repo_not_found", "message": f"Repo path not found: {repo_path}"}
    repo_slug = slug or root.name
    stamp = now()
    conn.execute(
        """
        INSERT INTO repos(slug, root_path, display_name, vcs_type, vcs_head, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(slug) DO UPDATE SET root_path = excluded.root_path, display_name = excluded.display_name, vcs_head = excluded.vcs_head, updated_at = excluded.updated_at
        """,
        (repo_slug, str(root), root.name, "git" if (root / ".git").exists() else None, git_head(root), stamp, stamp),
    )
    conn.commit()
    return {"ok": True, "slug": repo_slug, "root_path": str(root)}


def get_repo(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM repos WHERE slug = ?", (slug,)).fetchone()


def index_repo(conn: sqlite3.Connection, slug: str, changed_only: bool = False, only_path: str | None = None, enrich: bool = False) -> dict:
    repo = get_repo(conn, slug)
    if not repo:
        return {"ok": False, "error": "repo_not_registered", "message": f"No repo registered as {slug}."}
    root = Path(repo["root_path"])
    refresh_gitignore(root)
    paths = [normalize_rel_path(only_path)] if only_path else discover_files(root)
    indexed = 0
    skipped = 0
    unchanged = 0
    touched: set[int] = set()
    deleted = _delete_missing(conn, repo["id"], root, set(paths)) if not only_path else 0
    if not only_path:
        deleted += _delete_now_excluded(conn, repo["id"], root)
    for rel_path in paths:
        path = root / rel_path
        classification = classify_path(conn, repo["id"], root, rel_path)
        if classification.decision != "include":
            audit_path(conn, repo["id"], rel_path, classification, "excluded")
            if _delete_file_records(conn, repo["id"], rel_path):
                deleted += 1
            skipped += 1
            continue
        if not path.is_file():
            audit_path(conn, repo["id"], rel_path, classification, "skipped")
            continue
        stat = path.stat()
        try:
            data = path.read_bytes()
        except OSError:
            audit_path(conn, repo["id"], rel_path, classification, "skipped")
            skipped += 1
            continue
        sha = hashlib.sha256(data).hexdigest()
        existing = conn.execute("SELECT id, sha256, mtime_ns FROM files WHERE repo_id = ? AND path = ?", (repo["id"], rel_path)).fetchone()
        if changed_only and existing and existing["sha256"] == sha and existing["mtime_ns"] == stat.st_mtime_ns:
            audit_path(conn, repo["id"], rel_path, classification, "included")
            unchanged += 1
            continue
        text = data.decode("utf-8", errors="replace")
        _delete_file_records(conn, repo["id"], rel_path)
        extracted = extract_file(root, rel_path, text)
        file_id = _insert_file(conn, repo["id"], rel_path, stat, sha, text, extracted)
        touched.add(file_id)
        for fact in extracted.facts:
            conn.execute(
                "INSERT INTO file_facts(file_id, fact_type, value, line_start, line_end, confidence) VALUES (?, ?, ?, ?, ?, 'primary')",
                (file_id, *fact),
            )
        for symbol in extracted.symbols:
            symbol_id = _insert_symbol(conn, repo["id"], file_id, symbol)
            for call in symbol.calls:
                conn.execute(
                    "INSERT INTO symbol_calls(symbol_id, call_text, receiver, function_name, line_start, line_end) VALUES (?, ?, ?, ?, ?, ?)",
                    (symbol_id, *call),
                )
            for lit in symbol.literals:
                conn.execute(
                    "INSERT INTO symbol_literals(symbol_id, literal_type, value, line_start, line_end) VALUES (?, ?, ?, ?, ?)",
                    (symbol_id, *lit),
                )
        indexed += 1
        audit_path(conn, repo["id"], rel_path, classification, "included")
    dirty_modules = _sync_modules(conn, repo["id"])
    apply_heuristics(conn, repo["id"])
    queue_model_jobs(conn, repo["id"], file_ids=touched, module_ids=dirty_modules, enrich=enrich)
    rebuild_repo_fts(conn, repo["id"], slug)
    conn.execute("UPDATE repos SET last_indexed_at = ?, updated_at = ?, vcs_head = ? WHERE id = ?", (now(), now(), git_head(root), repo["id"]))
    conn.commit()
    return {"ok": True, "slug": slug, "indexed": indexed, "skipped": skipped, "deleted": deleted, "unchanged": unchanged}


def re_evaluate_paths(conn: sqlite3.Connection, slug: str, paths: list[str]) -> dict:
    indexed = 0
    skipped = 0
    deleted = 0
    for path in sorted(set(paths)):
        result = index_repo(conn, slug, changed_only=False, only_path=path)
        if not result.get("ok"):
            return result
        indexed += int(result.get("indexed", 0))
        skipped += int(result.get("skipped", 0))
        deleted += int(result.get("deleted", 0))
    return {"ok": True, "slug": slug, "indexed": indexed, "skipped": skipped, "deleted": deleted}


def exclude_path_records(conn: sqlite3.Connection, slug: str, rel_path: str, recursive: bool = False) -> dict:
    repo = get_repo(conn, slug)
    if not repo:
        return {"ok": False, "error": "repo_not_registered", "message": f"No repo registered as {slug}."}
    rel_path = normalize_rel_path(rel_path).rstrip("/")
    if recursive:
        deleted = _delete_directory_records(conn, repo["id"], rel_path)
    else:
        deleted = 1 if _delete_file_records(conn, repo["id"], rel_path) else 0
    dirty_modules = _sync_modules(conn, repo["id"])
    apply_heuristics(conn, repo["id"])
    queue_model_jobs(conn, repo["id"], file_ids=set(), module_ids=dirty_modules)
    rebuild_repo_fts(conn, repo["id"], slug)
    conn.execute("DELETE FROM search_results_cache WHERE repo_id = ?", (repo["id"],))
    conn.commit()
    return {"ok": True, "slug": slug, "deleted": deleted}


def _insert_file(conn: sqlite3.Connection, repo_id: int, rel_path: str, stat, sha: str, text: str, extracted) -> int:
    cur = conn.execute(
        """
        INSERT INTO files(repo_id, path, language, parser_name, parser_available, size_bytes, sha256, mtime_ns, line_count, parse_error_count, indexed_at, stale)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (repo_id, rel_path, extracted.language, extracted.parser_name, int(extracted.parser_available), stat.st_size, sha, stat.st_mtime_ns, len(text.splitlines()), extracted.parse_error_count, now()),
    )
    return int(cur.lastrowid)


def _insert_symbol(conn: sqlite3.Connection, repo_id: int, file_id: int, symbol) -> int:
    cur = conn.execute(
        """
        INSERT INTO symbols(file_id, repo_id, name, qualified_name, symbol_ref, symbol_type, signature, decorators, docstring, line_start, line_end, byte_start, byte_end, primary_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (file_id, repo_id, symbol.name, symbol.qualified_name, symbol.symbol_ref, symbol.symbol_type, symbol.signature, symbol.decorators, symbol.docstring, symbol.line_start, symbol.line_end, symbol.byte_start, symbol.byte_end, symbol.primary_text),
    )
    return int(cur.lastrowid)


def _delete_file_records(conn: sqlite3.Connection, repo_id: int, rel_path: str) -> bool:
    row = conn.execute("SELECT id FROM files WHERE repo_id = ? AND path = ?", (repo_id, rel_path)).fetchone()
    if row:
        symbol_ids = [symbol["id"] for symbol in conn.execute("SELECT id FROM symbols WHERE file_id = ?", (row["id"],)).fetchall()]
        delete_vectors(conn, repo_id, "file", [row["id"]])
        if symbol_ids:
            delete_vectors(conn, repo_id, "symbol", symbol_ids)
        _delete_target_jobs(conn, "file", row["id"])
        for symbol_id in symbol_ids:
            _delete_target_jobs(conn, "symbol", symbol_id)
        conn.execute("DELETE FROM code_fts WHERE target_id = ? AND record_type = 'file'", (row["id"],))
        for symbol_id in symbol_ids:
            conn.execute("DELETE FROM code_fts WHERE target_id = ? AND record_type = 'symbol'", (symbol_id,))
        conn.execute("DELETE FROM search_results_cache WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM files WHERE id = ?", (row["id"],))
        return True
    return False


def _delete_directory_records(conn: sqlite3.Connection, repo_id: int, rel_path: str) -> int:
    rows = conn.execute(
        "SELECT path FROM files WHERE repo_id = ? AND (path = ? OR path LIKE ?)",
        (repo_id, rel_path, f"{rel_path}/%"),
    ).fetchall()
    deleted = 0
    for row in rows:
        if _delete_file_records(conn, repo_id, row["path"]):
            deleted += 1
    return deleted


def _delete_missing(conn: sqlite3.Connection, repo_id: int, root: Path, discovered: set[str]) -> int:
    rows = conn.execute("SELECT path FROM files WHERE repo_id = ?", (repo_id,)).fetchall()
    deleted = 0
    for row in rows:
        if row["path"] not in discovered and not (root / row["path"]).exists():
            _delete_file_records(conn, repo_id, row["path"])
            deleted += 1
    return deleted


def _delete_now_excluded(conn: sqlite3.Connection, repo_id: int, root: Path) -> int:
    rows = conn.execute("SELECT path FROM files WHERE repo_id = ?", (repo_id,)).fetchall()
    deleted = 0
    for row in rows:
        classification = classify_path(conn, repo_id, root, row["path"])
        if classification.decision == "exclude":
            audit_path(conn, repo_id, row["path"], classification, "excluded")
            if _delete_file_records(conn, repo_id, row["path"]):
                deleted += 1
    return deleted


def _sync_modules(conn: sqlite3.Connection, repo_id: int) -> set[int]:
    # reconcile modules (stable ids); return dirty ones and clear their stale jobs/vectors
    stamp = now()
    files = conn.execute("SELECT id, path FROM files WHERE repo_id = ? AND skipped_reason IS NULL", (repo_id,)).fetchall()
    desired: dict[str, set[int]] = {}
    for row in files:
        prefix = str(Path(row["path"]).parent)
        if prefix == ".":
            prefix = ""
        desired.setdefault(prefix, set()).add(row["id"])
    existing = {row["path_prefix"]: row["id"] for row in conn.execute("SELECT id, path_prefix FROM modules WHERE repo_id = ?", (repo_id,)).fetchall()}
    dirty: set[int] = set()
    for prefix, file_ids in desired.items():
        name = prefix.replace("/", ".") or "root"
        conn.execute(
            "INSERT INTO modules(repo_id, name, path_prefix, created_at, updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(repo_id, path_prefix) DO UPDATE SET updated_at = excluded.updated_at",
            (repo_id, name, prefix, stamp, stamp),
        )
        module_id = conn.execute("SELECT id FROM modules WHERE repo_id = ? AND path_prefix = ?", (repo_id, prefix)).fetchone()["id"]
        old_members = {row["file_id"] for row in conn.execute("SELECT file_id FROM module_files WHERE module_id = ?", (module_id,)).fetchall()}
        if old_members != file_ids:
            dirty.add(module_id)
            conn.execute("DELETE FROM module_files WHERE module_id = ?", (module_id,))
            for file_id in file_ids:
                conn.execute("INSERT OR IGNORE INTO module_files(module_id, file_id) VALUES (?, ?)", (module_id, file_id))
    for prefix, module_id in existing.items():
        if prefix not in desired:
            delete_vectors(conn, repo_id, "module", [module_id])
            _delete_target_jobs(conn, "module", module_id)
            conn.execute("DELETE FROM module_files WHERE module_id = ?", (module_id,))
            conn.execute("DELETE FROM modules WHERE id = ?", (module_id,))
    for module_id in dirty:
        delete_vectors(conn, repo_id, "module", [module_id])
        _delete_target_jobs(conn, "module", module_id)
    return dirty


def _delete_target_jobs(conn: sqlite3.Connection, target_type: str, target_id: int) -> None:
    conn.execute("DELETE FROM model_outputs WHERE target_type = ? AND target_id = ?", (target_type, target_id))
    conn.execute("DELETE FROM model_jobs WHERE target_type = ? AND target_id = ?", (target_type, target_id))
