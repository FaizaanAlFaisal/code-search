from __future__ import annotations

import sqlite3

from codesearch.db.fts import rebuild_repo_fts
from codesearch.indexing.jobs import model_queue
from codesearch.model.embed import qdrant_health, run_embedding_batch
from codesearch.model.enrich import run_enrichment_batch
from codesearch.model.ollama import health as ollama_health, loaded_models, unload_model, wait_until_unloaded
from codesearch.config import settings


def _pending_enrichment(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT count(*) FROM model_jobs WHERE status = 'pending' AND job_type IN ('symbol_enrichment', 'file_enrichment', 'module_enrichment')"
        ).fetchone()[0]
    )


def run_embeddings(conn: sqlite3.Connection) -> dict:
    # default lightweight path: embeddings only, no enrichment
    if not ollama_health():
        return {"ran": False, "reason": "ollama_unavailable", "queue": model_queue(conn)["jobs"]}
    if not qdrant_health():
        return {"ran": False, "reason": "qdrant_unavailable", "queue": model_queue(conn)["jobs"]}
    embedding = run_embedding_batch(conn)
    return {"ran": True, "embedding": embedding, "queue": model_queue(conn)["jobs"]}


def run_queued_jobs(conn: sqlite3.Connection, repo_id: int | None = None, repo_slug: str | None = None) -> dict:
    # opt-in --enrich path: enrichment first, then embeddings (gated on enrichment finishing)
    if not ollama_health():
        return {"ran": False, "reason": "ollama_unavailable", "queue": model_queue(conn)["jobs"]}
    summary_unload_error = None
    summary_wait_error = None
    embed_evicted = False
    enrichment: dict = {"skipped": True, "reason": "no_pending_enrichment"}
    if _pending_enrichment(conn):
        # evict warm embed model so the reasoning model gets the full gpu; reloaded below
        loaded, _ = loaded_models()
        if settings.embed_model in loaded:
            embed_evicted = unload_model(settings.embed_model) is None
            if embed_evicted:
                wait_until_unloaded(settings.embed_model)
        enrichment = run_enrichment_batch(conn)
        summary_unload_error = unload_model(settings.summary_model)
        summary_wait_error = None if summary_unload_error else wait_until_unloaded(settings.summary_model)
    if repo_id is not None and repo_slug is not None:
        rebuild_repo_fts(conn, repo_id, repo_slug)
        conn.commit()
    elif repo_id is None:
        for repo in conn.execute("SELECT id, slug FROM repos ORDER BY slug").fetchall():
            rebuild_repo_fts(conn, repo["id"], repo["slug"])
        conn.commit()
    if summary_unload_error or summary_wait_error:
        return {
            "ran": False,
            "reason": "summary_model_unload_failed",
            "enrichment": enrichment,
            "summary_unload_error": summary_unload_error,
            "summary_wait_error": summary_wait_error,
            "queue": model_queue(conn)["jobs"],
        }
    # gate: only embed once enrichment is fully done (skip if it didn't finish)
    if bool(enrichment.get("interrupted")) or _pending_enrichment(conn) > 0:
        return {
            "ran": False,
            "reason": "enrichment_incomplete",
            "enrichment": enrichment,
            "embed_evicted_for_enrichment": embed_evicted,
            "embedding": {"skipped": True, "reason": "enrichment_incomplete"},
            "queue": model_queue(conn)["jobs"],
        }
    if not qdrant_health():
        return {
            "ran": False,
            "reason": "qdrant_unavailable",
            "enrichment": enrichment,
            "summary_unload_error": summary_unload_error,
            "summary_wait_error": summary_wait_error,
            "queue": model_queue(conn)["jobs"],
        }
    embedding = run_embedding_batch(conn)
    return {
        "ran": True,
        "enrichment": enrichment,
        "summary_unload_error": summary_unload_error,
        "summary_wait_error": summary_wait_error,
        "embed_evicted_for_enrichment": embed_evicted,
        "embedding": embedding,
        "queue": model_queue(conn)["jobs"],
    }
