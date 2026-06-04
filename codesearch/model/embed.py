from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import datetime, timezone

import requests

from codesearch.config import settings
from codesearch.model.ollama import embed, embed_batch
from codesearch.progress import Progress


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_embedding_batch(conn: sqlite3.Connection, limit: int | None = None) -> dict:
    # Batched: embed up to embed_batch_size jobs per Ollama call and bulk-upsert them to
    # Qdrant in one request. Chunks never span models or repos (different collections).
    jobs = conn.execute(
        """
        SELECT * FROM model_jobs
        WHERE status = 'pending' AND job_type = 'embedding'
        ORDER BY model, repo_id, priority, id
        LIMIT ?
        """,
        (limit or 100000,),
    ).fetchall()
    total = len(jobs)
    done = 0
    errors = 0
    since_commit = 0
    progress = Progress("embedding", total)
    ensured: set[str] = set()
    batch_size = max(1, settings.embed_batch_size)

    def commit_tick() -> None:
        nonlocal since_commit
        since_commit += 1
        if since_commit >= settings.model_commit_every:
            conn.commit()
            since_commit = 0

    i = 0
    while i < total:
        model = jobs[i]["model"]
        repo_id = jobs[i]["repo_id"]
        chunk: list[sqlite3.Row] = []
        while i < total and len(chunk) < batch_size and jobs[i]["model"] == model and jobs[i]["repo_id"] == repo_id:
            chunk.append(jobs[i])
            i += 1
        texts = [_text_for_job(conn, job) for job in chunk]
        conn.executemany(
            "UPDATE model_jobs SET status = 'running', started_at = ?, attempts = attempts + 1 WHERE id = ?",
            [(now(), job["id"]) for job in chunk],
        )

        vectors, error = embed_batch(texts, model)

        # connection-level failure: leave the whole chunk pending and abort (interrupt-safe)
        if error and _is_service_error(error):
            conn.executemany(
                "UPDATE model_jobs SET status = 'pending', error = ?, finished_at = ? WHERE id = ?",
                [(error, now(), job["id"]) for job in chunk],
            )
            conn.commit()
            for _ in chunk:
                progress.update(ok=False)
            progress.close()
            return {"ok": False, "processed": done, "errors": errors + len(chunk), "interrupted": True, "error": error}

        # non-connection batch failure or shape mismatch: degrade to per-item for this chunk
        if error or vectors is None or len(vectors) != len(chunk):
            for job, text in zip(chunk, texts):
                status, err = _embed_one(conn, job, text, repo_id, ensured)
                if status == "interrupted":
                    conn.commit()
                    progress.update(ok=False)
                    progress.close()
                    return {"ok": False, "processed": done, "errors": errors + 1, "interrupted": True, "error": err}
                if status == "done":
                    done += 1
                    progress.update(ok=True)
                else:
                    errors += 1
                    progress.update(ok=False)
                commit_tick()
            continue

        # happy path: error out any empty vectors, bulk-upsert the rest in one call
        good: list[tuple[sqlite3.Row, list[float], str]] = []
        for job, text, vector in zip(chunk, texts, vectors):
            if not vector:
                conn.execute("UPDATE model_jobs SET status = 'error', error = ?, finished_at = ? WHERE id = ?", ("empty_embedding", now(), job["id"]))
                errors += 1
                progress.update(ok=False)
            else:
                good.append((job, vector, text))
        if good:
            qerr = _upsert_points(conn, repo_id, good, ensured)
            if qerr:
                # transport/Qdrant failure: leave these pending (retryable) rather than error
                conn.executemany(
                    "UPDATE model_jobs SET status = 'pending', error = ?, finished_at = ? WHERE id = ?",
                    [(qerr, now(), job["id"]) for (job, _v, _t) in good],
                )
                for _ in good:
                    errors += 1
                    progress.update(ok=False)
                    commit_tick()
            else:
                for (job, _v, _t) in good:
                    conn.execute("UPDATE model_jobs SET status = 'done', finished_at = ? WHERE id = ?", (now(), job["id"]))
                    done += 1
                    progress.update(ok=True)
                    commit_tick()
    conn.commit()
    progress.close()
    return {"ok": errors == 0, "processed": done, "errors": errors}


def _embed_one(conn: sqlite3.Connection, job: sqlite3.Row, text: str, repo_id: int, ensured: set[str]) -> tuple[str, str | None]:
    # single-item fallback used when a batch call fails non-fatally
    vector, error = embed(text, job["model"])
    if error and _is_service_error(error):
        conn.execute("UPDATE model_jobs SET status = 'pending', error = ?, finished_at = ? WHERE id = ?", (error, now(), job["id"]))
        return "interrupted", error
    if error or not vector:
        conn.execute("UPDATE model_jobs SET status = 'error', error = ?, finished_at = ? WHERE id = ?", (error or "empty_embedding", now(), job["id"]))
        return "error", error
    qerr = _upsert_points(conn, repo_id, [(job, vector, text)], ensured)
    if qerr:
        conn.execute("UPDATE model_jobs SET status = 'pending', error = ?, finished_at = ? WHERE id = ?", (qerr, now(), job["id"]))
        return "error", qerr
    conn.execute("UPDATE model_jobs SET status = 'done', finished_at = ? WHERE id = ?", (now(), job["id"]))
    return "done", None


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


def vector_search(conn: sqlite3.Connection, repo_id: int, query: str, limit: int = 10) -> list[dict]:
    vector, error = embed(query, settings.embed_model)
    if error or not vector:
        return []
    collection = _collection_name(repo_id)
    try:
        response = requests.post(
            f"{settings.qdrant_url}/collections/{collection}/points/search",
            json={"vector": vector, "limit": limit, "with_payload": True},
            timeout=10,
        )
        if not response.ok:
            return []
        return response.json().get("result", [])
    except Exception:
        return []


def fetch_vectors(conn: sqlite3.Connection, repo_id: int, target_type: str, target_ids: list[int]) -> dict[int, list[float]]:
    # pull stored vectors for the given targets (used by explore dedup); {} on any miss
    if not target_ids:
        return {}
    placeholders = ",".join("?" for _ in target_ids)
    rows = conn.execute(
        f"SELECT target_id, vector_point_id FROM vector_records WHERE repo_id = ? AND target_type = ? AND target_id IN ({placeholders})",
        (repo_id, target_type, *target_ids),
    ).fetchall()
    pid_to_target = {row["vector_point_id"]: row["target_id"] for row in rows}
    if not pid_to_target:
        return {}
    try:
        response = requests.post(
            f"{settings.qdrant_url}/collections/{_collection_name(repo_id)}/points",
            json={"ids": list(pid_to_target), "with_vector": True},
            timeout=10,
        )
        if not response.ok:
            return {}
    except Exception:
        return {}
    out: dict[int, list[float]] = {}
    for point in response.json().get("result", []):
        target = pid_to_target.get(str(point.get("id")))
        vector = point.get("vector")
        if target is not None and isinstance(vector, list):
            out[target] = vector
    return out


def qdrant_health() -> bool:
    try:
        return requests.get(f"{settings.qdrant_url}/collections", timeout=3).ok
    except Exception:
        return False


def delete_vectors(conn: sqlite3.Connection, repo_id: int, target_type: str | None = None, target_ids: list[int] | None = None) -> int:
    params: list[object] = [repo_id]
    where = ["repo_id = ?"]
    if target_type is not None:
        where.append("target_type = ?")
        params.append(target_type)
    if target_ids is not None:
        if not target_ids:
            return 0
        where.append(f"target_id IN ({','.join('?' for _ in target_ids)})")
        params.extend(target_ids)
    rows = conn.execute(f"SELECT vector_point_id FROM vector_records WHERE {' AND '.join(where)}", params).fetchall()
    point_ids = [row["vector_point_id"] for row in rows]
    if point_ids:
        collection = _collection_name(repo_id)
        try:
            requests.post(
                f"{settings.qdrant_url}/collections/{collection}/points/delete",
                json={"points": point_ids},
                timeout=10,
            )
        except Exception:
            pass
    conn.execute(f"DELETE FROM vector_records WHERE {' AND '.join(where)}", params)
    return len(point_ids)


def _point_id(job: sqlite3.Row) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{job['target_type']}:{job['target_id']}:{job['input_hash']}"))


def _ensure_collection(repo_id: int, size: int, ensured: set[str]) -> None:
    # create the per-repo collection once per run; best-effort (idempotent on re-create)
    collection = _collection_name(repo_id)
    if collection in ensured:
        return
    try:
        requests.put(f"{settings.qdrant_url}/collections/{collection}", json={"vectors": {"size": size, "distance": "Cosine"}}, timeout=10)
    except Exception:
        pass
    ensured.add(collection)


def _upsert_points(conn: sqlite3.Connection, repo_id: int, items: list[tuple[sqlite3.Row, list[float], str]], ensured: set[str]) -> str | None:
    # bulk-upsert all (job, vector, text) tuples to the repo's collection in one Qdrant call
    if not items:
        return None
    collection = _collection_name(repo_id)
    _ensure_collection(repo_id, len(items[0][1]), ensured)
    points = [
        {
            "id": _point_id(job),
            "vector": vector,
            "payload": {"repo_id": job["repo_id"], "target_type": job["target_type"], "target_id": job["target_id"], "text_kind": _text_kind(job)},
        }
        for (job, vector, _text) in items
    ]
    try:
        response = requests.put(
            f"{settings.qdrant_url}/collections/{collection}/points",
            json={"points": points},
            timeout=max(20, len(points)),
        )
        response.raise_for_status()
    except Exception as exc:
        return str(exc)
    for (job, _vector, text) in items:
        conn.execute(
            """
            INSERT OR REPLACE INTO vector_records(repo_id, target_type, target_id, vector_collection, vector_point_id, text_hash, text_kind, embedded_model, embedded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (job["repo_id"], job["target_type"], job["target_id"], collection, _point_id(job), hashlib.sha256(text.encode("utf-8")).hexdigest(), _text_kind(job), job["model"], now()),
        )
    return None


def _collection_name(repo_id: int) -> str:
    return f"code_search_repo_{repo_id}"


def _text_kind(job: sqlite3.Row) -> str:
    if job["target_type"] == "symbol":
        return "symbol_primary"
    if job["target_type"] == "file":
        return "file_assembled"
    return "module_assembled"


def _text_for_job(conn: sqlite3.Connection, job: sqlite3.Row) -> str:
    # deterministic text only; embeddings stay decoupled from enrichment
    if job["target_type"] == "symbol":
        row = conn.execute("SELECT primary_text FROM symbols WHERE id = ?", (job["target_id"],)).fetchone()
        return row["primary_text"] if row else ""
    if job["target_type"] == "file":
        return _file_text(conn, job["target_id"])
    if job["target_type"] == "module":
        return _module_text(conn, job["target_id"])
    return ""


def _file_text(conn: sqlite3.Connection, file_id: int) -> str:
    file_row = conn.execute("SELECT path, language FROM files WHERE id = ?", (file_id,)).fetchone()
    if not file_row:
        return ""
    facts = conn.execute("SELECT fact_type, value FROM file_facts WHERE file_id = ? LIMIT 100", (file_id,)).fetchall()
    symbols = conn.execute("SELECT symbol_ref, symbol_type, line_start, line_end FROM symbols WHERE file_id = ? ORDER BY line_start", (file_id,)).fetchall()
    return "\n".join(
        [
            f"path: {file_row['path']}",
            f"language: {file_row['language'] or ''}",
            "facts: " + "; ".join(f"{row['fact_type']}={row['value']}" for row in facts),
            "symbols: " + "; ".join(f"{row['symbol_ref']} {row['symbol_type']} {row['line_start']}-{row['line_end']}" for row in symbols),
        ]
    )


def _module_text(conn: sqlite3.Connection, module_id: int) -> str:
    module = conn.execute("SELECT name, path_prefix FROM modules WHERE id = ?", (module_id,)).fetchone()
    if not module:
        return ""
    files = conn.execute(
        """
        SELECT f.path, group_concat(s.symbol_ref, ', ') AS symbols
        FROM module_files mf
        JOIN files f ON f.id = mf.file_id
        LEFT JOIN symbols s ON s.file_id = f.id
        WHERE mf.module_id = ?
        GROUP BY f.id
        ORDER BY f.path
        """,
        (module_id,),
    ).fetchall()
    return "\n".join([f"module: {module['name']}", f"path_prefix: {module['path_prefix']}", "files: " + "; ".join(f"{row['path']} [{row['symbols'] or ''}]" for row in files)])
