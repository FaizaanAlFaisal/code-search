from __future__ import annotations

import sqlite3

from codesearch.config import settings


def model_queue(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT model, job_type, status, count(*) AS count FROM model_jobs GROUP BY model, job_type, status ORDER BY model, job_type, status"
    ).fetchall()
    return {"ok": True, "jobs": [dict(row) for row in rows], "summary": _queue_summary(rows)}


def _queue_summary(rows: list[sqlite3.Row]) -> dict:
    # roll grouped rows up per job_type into done/total + percent — the "how much is left" view
    kinds: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = kinds.setdefault(row["job_type"], {"pending": 0, "running": 0, "done": 0, "error": 0})
        bucket[row["status"]] = bucket.get(row["status"], 0) + row["count"]
    out: dict[str, dict] = {}
    for job_type, counts in kinds.items():
        total = sum(counts.values())
        done = counts.get("done", 0)
        out[job_type] = {
            **counts,
            "total": total,
            "remaining": total - done,
            "percent_done": round(done / total * 100, 1) if total else 100.0,
        }
    return out


def expected_models() -> dict:
    return {"summary_model": settings.summary_model, "embed_model": settings.embed_model}

