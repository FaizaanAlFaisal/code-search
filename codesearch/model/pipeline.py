from __future__ import annotations

import sqlite3

from codesearch.indexing.jobs import model_queue
from codesearch.model.embed import qdrant_health, run_embedding_batch
from codesearch.model.ollama import health as ollama_health


def run_embeddings(conn: sqlite3.Connection) -> dict:
    # default lightweight path: embeddings only, no enrichment
    if not ollama_health():
        return {"ran": False, "reason": "ollama_unavailable", "queue": model_queue(conn)["jobs"]}
    if not qdrant_health():
        return {"ran": False, "reason": "qdrant_unavailable", "queue": model_queue(conn)["jobs"]}
    embedding = run_embedding_batch(conn)
    return {"ran": True, "embedding": embedding, "queue": model_queue(conn)["jobs"]}
