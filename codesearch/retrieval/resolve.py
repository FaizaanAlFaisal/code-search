from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from codesearch.config import settings


def repo(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM repos WHERE slug = ?", (slug,)).fetchone()


def cache_results(conn: sqlite3.Connection, repo_id: int, ranked: list[dict], query: str) -> None:
    # assign r_NN handles for the session and persist so `open r_NN` resolves
    conn.execute("DELETE FROM search_results_cache WHERE repo_id = ? AND session_key = ?", (repo_id, settings.cache_session_key))
    for idx, row in enumerate(ranked, start=1):
        result_id = f"r_{idx:02d}"
        row["result_id"] = result_id
        conn.execute(
            "INSERT INTO search_results_cache(id, repo_id, session_key, target_type, target_id, path, symbol_ref, line_start, line_end, query, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (result_id, repo_id, settings.cache_session_key, row["record_type"], row["target_id"], row["path"], row.get("symbol"), row["line_start"], row["line_end"], query, datetime.now(timezone.utc).isoformat()),
        )
    conn.commit()


def resolve_target(conn: sqlite3.Connection, repo_row: sqlite3.Row, target: str) -> dict:
    cached = conn.execute(
        "SELECT * FROM search_results_cache WHERE repo_id = ? AND id = ? AND session_key = ?",
        (repo_row["id"], target, settings.cache_session_key),
    ).fetchone()
    if cached:
        return {"ok": True, "type": cached["target_type"], "id": cached["target_id"], "path": cached["path"], "symbol_ref": cached["symbol_ref"], "line_start": cached["line_start"], "line_end": cached["line_end"]}
    symbol = conn.execute("SELECT s.*, f.path FROM symbols s JOIN files f ON f.id = s.file_id WHERE s.repo_id = ? AND s.symbol_ref = ?", (repo_row["id"], target)).fetchone()
    if symbol:
        return {"ok": True, "type": "symbol", "id": symbol["id"], "path": symbol["path"], "symbol_ref": symbol["symbol_ref"], "line_start": symbol["line_start"], "line_end": symbol["line_end"]}
    file_row = conn.execute("SELECT * FROM files WHERE repo_id = ? AND path = ?", (repo_row["id"], target)).fetchone()
    if file_row:
        return {"ok": True, "type": "file", "id": file_row["id"], "path": file_row["path"], "symbol_ref": None, "line_start": 1, "line_end": min(file_row["line_count"], 80)}
    module = conn.execute("SELECT * FROM modules WHERE repo_id = ? AND path_prefix = ?", (repo_row["id"], target.rstrip("/"))).fetchone()
    if module:
        return {"ok": True, "type": "module", "id": module["id"], "path": module["path_prefix"], "symbol_ref": None, "line_start": 1, "line_end": 1}
    matches = conn.execute("SELECT s.*, f.path FROM symbols s JOIN files f ON f.id = s.file_id WHERE s.repo_id = ? AND s.name = ? ORDER BY f.path, s.line_start", (repo_row["id"], target)).fetchall()
    if len(matches) > 1:
        return {"ok": False, "error": "ambiguous", "matches": [dict(row) for row in matches]}
    if len(matches) == 1:
        row = matches[0]
        return {"ok": True, "type": "symbol", "id": row["id"], "path": row["path"], "symbol_ref": row["symbol_ref"], "line_start": row["line_start"], "line_end": row["line_end"]}
    fuzzy = conn.execute(
        """
        SELECT s.*, f.path FROM symbols s
        JOIN files f ON f.id = s.file_id
        WHERE s.repo_id = ? AND (s.symbol_ref LIKE ? OR s.qualified_name LIKE ?)
        ORDER BY length(s.symbol_ref)
        LIMIT 6
        """,
        (repo_row["id"], f"%{target}%", f"%{target}%"),
    ).fetchall()
    if len(fuzzy) == 1:
        row = fuzzy[0]
        return {"ok": True, "type": "symbol", "id": row["id"], "path": row["path"], "symbol_ref": row["symbol_ref"], "line_start": row["line_start"], "line_end": row["line_end"]}
    if fuzzy:
        return {"ok": False, "error": "ambiguous", "matches": [dict(row) for row in fuzzy]}
    return {"ok": False, "error": "not_found", "message": f"No result, symbol, or path matched {target}."}


def current_hash(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def stale_paths(conn: sqlite3.Connection, repo_id: int, root: str, paths) -> set[str]:
    # subset of paths whose on-disk content differs from what was indexed
    out: set[str] = set()
    for path in set(p for p in paths if p):
        row = conn.execute("SELECT sha256 FROM files WHERE repo_id = ? AND path = ?", (repo_id, path)).fetchone()
        if not row:
            continue
        current = current_hash(Path(root) / path)
        if current and row["sha256"] != current:
            out.add(path)
    return out
