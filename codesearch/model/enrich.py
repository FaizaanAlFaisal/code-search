from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from codesearch.config import settings
from codesearch.model.ollama import generate_json
from codesearch.progress import Progress

ALLOWED_FIELDS = {"purpose", "summary", "behavior_aliases", "likely_queries", "search_tags"}
REJECTED_FIELDS = {"exact_terms", "safety_tags", "line_ranges", "symbol_inventories", "import_inventories"}


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


MAX_ENRICHMENT_ATTEMPTS = 3
ENRICHMENT_JOB_TYPES = ("symbol_enrichment", "file_enrichment", "module_enrichment")


def run_enrichment_batch(conn: sqlite3.Connection, limit: int | None = None) -> dict:
    # retry errored jobs (transient timeouts), bounded by attempts
    conn.execute(
        f"UPDATE model_jobs SET status = 'pending' WHERE status = 'error' AND job_type IN ({','.join('?' for _ in ENRICHMENT_JOB_TYPES)}) AND attempts < ?",
        (*ENRICHMENT_JOB_TYPES, MAX_ENRICHMENT_ATTEMPTS),
    )
    conn.commit()
    jobs = conn.execute(
        """
        SELECT * FROM model_jobs
        WHERE status = 'pending' AND job_type IN ('symbol_enrichment', 'file_enrichment', 'module_enrichment')
        ORDER BY model, priority, id
        LIMIT ?
        """,
        (limit or 100000,),
    ).fetchall()
    done = 0
    errors = 0
    since_commit = 0
    progress = Progress("enrichment", len(jobs))
    for job in jobs:
        prompt = _prompt_for_job(conn, job)
        conn.execute("UPDATE model_jobs SET status = 'running', started_at = ?, attempts = attempts + 1 WHERE id = ?", (now(), job["id"]))
        data, error = generate_json(prompt, job["model"])
        if error:
            if _is_service_error(error):
                conn.execute("UPDATE model_jobs SET status = 'pending', error = ?, finished_at = ? WHERE id = ?", (error, now(), job["id"]))
                conn.commit()
                progress.update(ok=False)
                progress.close()
                return {"ok": False, "processed": done, "errors": errors + 1, "interrupted": True, "error": error}
            conn.execute("UPDATE model_jobs SET status = 'error', error = ?, finished_at = ? WHERE id = ?", (error, now(), job["id"]))
            errors += 1
            progress.update(ok=False)
            since_commit += 1
            if since_commit >= settings.model_commit_every:
                conn.commit()
                since_commit = 0
            continue
        accepted, rejected_reason = _validate_output(data or {})
        conn.execute(
            """
            INSERT INTO model_outputs(job_id, target_type, target_id, model, output_type, json_text, accepted, rejected_reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (job["id"], job["target_type"], job["target_id"], job["model"], job["job_type"], json.dumps(accepted or data or {}, sort_keys=True), int(bool(accepted)), rejected_reason, now()),
        )
        conn.execute("UPDATE model_jobs SET status = 'done', finished_at = ? WHERE id = ?", (now(), job["id"]))
        done += 1
        progress.update(ok=True)
        since_commit += 1
        if since_commit >= settings.model_commit_every:
            conn.commit()
            since_commit = 0
    conn.commit()
    progress.close()
    return {"ok": errors == 0, "processed": done, "errors": errors}


def _is_service_error(error: str) -> bool:
    # only connection failures abort the batch; a read timeout just errors that job
    markers = (
        "connection refused",
        "connection reset",
        "connection aborted",
        "remote end closed",
        "failed to establish a new connection",
    )
    lowered = error.lower()
    return any(marker in lowered for marker in markers)


def _validate_output(data: dict) -> tuple[dict | None, str | None]:
    if not isinstance(data, dict):
        return None, "not_json_object"
    if any(field in data for field in REJECTED_FIELDS):
        data = {key: value for key, value in data.items() if key not in REJECTED_FIELDS}
    accepted = {key: value for key, value in data.items() if key in ALLOWED_FIELDS}
    if not accepted:
        return None, "no_accepted_fields"
    return accepted, None


def _prompt_for_job(conn: sqlite3.Connection, job: sqlite3.Row) -> str:
    if job["target_type"] == "symbol":
        row = conn.execute(
            """
            SELECT s.*, f.path, group_concat(sc.call_text, ', ') AS calls, group_concat(sl.value, ', ') AS literals,
                   group_concat(h.flag || ':' || h.evidence_value, ', ') AS flags
            FROM symbols s
            JOIN files f ON f.id = s.file_id
            LEFT JOIN symbol_calls sc ON sc.symbol_id = s.id
            LEFT JOIN symbol_literals sl ON sl.symbol_id = s.id
            LEFT JOIN heuristic_flags h ON h.target_type = 'symbol' AND h.target_id = s.id
            WHERE s.id = ?
            GROUP BY s.id
            """,
            (job["target_id"],),
        ).fetchone()
        return (
            f"path: {row['path']}\nsymbol: {row['symbol_ref']}\nsignature: {row['signature']}\n"
            f"line range: {row['line_start']}-{row['line_end']}\ncalls: {row['calls'] or ''}\n"
            f"literals: {row['literals'] or ''}\nheuristic flags: {row['flags'] or ''}\ndocstring: {row['docstring'] or ''}\n"
            f"raw symbol code:\n{row['primary_text'][:6000]}\n\nReturn JSON with purpose, behavior_aliases, likely_queries only."
        )
    if job["target_type"] == "file":
        return _file_outline(conn, job["target_id"]) + "\n\nReturn JSON with short summary, behavior_aliases, likely_queries only."
    return _module_outline(conn, job["target_id"]) + "\n\nReturn JSON with short summary, behavior_aliases, likely_queries only."


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
