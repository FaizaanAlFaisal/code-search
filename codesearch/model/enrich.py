from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone

from codesearch.config import settings


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def queue_model_jobs(
    conn: sqlite3.Connection,
    repo_id: int,
    file_ids: set[int] | None = None,
    module_ids: set[int] | None = None,
    enrich: bool = False,
) -> None:
    # embeddings always queued, enrichment only when enrich=True; scope = file_ids/module_ids (None = all)
    for row in _scoped_symbols(conn, repo_id, file_ids):
        if enrich:
            _queue_job(conn, repo_id, "symbol", row["id"], "symbol_enrichment", settings.summary_model, row["primary_text"], 50)
        _queue_job(conn, repo_id, "symbol", row["id"], "embedding", settings.embed_model, row["primary_text"], 200)
    for row in _scoped_files(conn, repo_id, file_ids):
        outline = _file_outline(conn, row["id"])
        if enrich:
            _queue_job(conn, repo_id, "file", row["id"], "file_enrichment", settings.summary_model, outline, 100)
        _queue_job(conn, repo_id, "file", row["id"], "embedding", settings.embed_model, outline, 250)
    for row in _scoped_modules(conn, repo_id, module_ids):
        outline = _module_outline(conn, row["id"])
        if enrich:
            _queue_job(conn, repo_id, "module", row["id"], "module_enrichment", settings.summary_model, outline, 150)
        _queue_job(conn, repo_id, "module", row["id"], "embedding", settings.embed_model, outline, 300)


def _scoped_symbols(conn: sqlite3.Connection, repo_id: int, file_ids: set[int] | None) -> list[sqlite3.Row]:
    if file_ids is None:
        return conn.execute("SELECT id, primary_text FROM symbols WHERE repo_id = ?", (repo_id,)).fetchall()
    if not file_ids:
        return []
    placeholders = ",".join("?" for _ in file_ids)
    return conn.execute(
        f"SELECT id, primary_text FROM symbols WHERE repo_id = ? AND file_id IN ({placeholders})",
        (repo_id, *file_ids),
    ).fetchall()


def _scoped_files(conn: sqlite3.Connection, repo_id: int, file_ids: set[int] | None) -> list[sqlite3.Row]:
    if file_ids is None:
        return conn.execute("SELECT id, path FROM files WHERE repo_id = ? AND skipped_reason IS NULL", (repo_id,)).fetchall()
    if not file_ids:
        return []
    placeholders = ",".join("?" for _ in file_ids)
    return conn.execute(
        f"SELECT id, path FROM files WHERE repo_id = ? AND skipped_reason IS NULL AND id IN ({placeholders})",
        (repo_id, *file_ids),
    ).fetchall()


def _scoped_modules(conn: sqlite3.Connection, repo_id: int, module_ids: set[int] | None) -> list[sqlite3.Row]:
    if module_ids is None:
        return conn.execute("SELECT id, name FROM modules WHERE repo_id = ?", (repo_id,)).fetchall()
    if not module_ids:
        return []
    placeholders = ",".join("?" for _ in module_ids)
    return conn.execute(
        f"SELECT id, name FROM modules WHERE repo_id = ? AND id IN ({placeholders})",
        (repo_id, *module_ids),
    ).fetchall()


def _queue_job(conn: sqlite3.Connection, repo_id: int, target_type: str, target_id: int, job_type: str, model: str, text: str, priority: int) -> None:
    input_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    existing = conn.execute(
        "SELECT id FROM model_jobs WHERE target_type = ? AND target_id = ? AND job_type = ? AND model = ? AND input_hash = ?",
        (target_type, target_id, job_type, model, input_hash),
    ).fetchone()
    if existing:
        return
    conn.execute(
        """
        INSERT INTO model_jobs(repo_id, target_type, target_id, job_type, model, status, priority, input_hash, prompt_tokens_estimate, created_at)
        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
        """,
        (repo_id, target_type, target_id, job_type, model, priority, input_hash, max(1, len(text) // 4), now()),
    )


def _file_outline(conn: sqlite3.Connection, file_id: int) -> str:
    file_row = conn.execute("SELECT path, language FROM files WHERE id = ?", (file_id,)).fetchone()
    symbols = conn.execute("SELECT symbol_ref, symbol_type, line_start, line_end FROM symbols WHERE file_id = ? ORDER BY line_start", (file_id,)).fetchall()
    facts = conn.execute("SELECT fact_type, value FROM file_facts WHERE file_id = ? LIMIT 80", (file_id,)).fetchall()
    return "\n".join(
        [
            f"path: {file_row['path']}",
            f"language: {file_row['language'] or ''}",
            "imports/facts: " + "; ".join(f"{row['fact_type']}={row['value']}" for row in facts),
            "symbols: " + "; ".join(f"{row['symbol_ref']} {row['symbol_type']} {row['line_start']}-{row['line_end']}" for row in symbols),
        ]
    )


def _module_outline(conn: sqlite3.Connection, module_id: int) -> str:
    module = conn.execute("SELECT name, path_prefix FROM modules WHERE id = ?", (module_id,)).fetchone()
    files = conn.execute(
        """
        SELECT f.path, group_concat(s.name, ', ') AS symbols
        FROM module_files mf
        JOIN files f ON f.id = mf.file_id
        LEFT JOIN symbols s ON s.file_id = f.id
        WHERE mf.module_id = ?
        GROUP BY f.id
        """,
        (module_id,),
    ).fetchall()
    return "\n".join([f"module: {module['name']}", f"path_prefix: {module['path_prefix']}", "files: " + "; ".join(f"{row['path']} [{row['symbols'] or ''}]" for row in files)])
