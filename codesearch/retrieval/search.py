from __future__ import annotations

import sqlite3

from codesearch.config import settings
from codesearch.db.fts import quote_fts_query
from codesearch.model.embed import vector_search
from codesearch.retrieval.rank import rerank
from codesearch.retrieval.resolve import cache_results, repo, stale_paths


def search(conn: sqlite3.Connection, slug: str, query: str, limit: int | None = None) -> dict:
    repo_row = repo(conn, slug)
    if not repo_row:
        return {"ok": False, "error": "repo_not_registered", "message": f"No repo registered as {slug}."}
    wanted = limit or settings.result_limit
    candidates = _collect_candidates(conn, repo_row["id"], slug, query)
    ranked = rerank(candidates, query, limit=wanted)
    stale = stale_paths(conn, repo_row["id"], repo_row["root_path"], [r["path"] for r in ranked])
    for row in ranked:
        row["stale"] = row["path"] in stale
    cache_results(conn, repo_row["id"], ranked, query)
    return {"ok": True, "query": query, "results": ranked, "stale_count": len(stale)}


def _collect_candidates(conn: sqlite3.Connection, repo_id: int, slug: str, query: str) -> list[dict]:
    # gather fts + vector candidates, tagging each with its rank position; rank.py fuses
    by_key: dict[tuple, dict] = {}
    for position, row in enumerate(_fts(conn, slug, query, 30)):
        row["fts_rank"] = position
        row["vector_rank"] = None
        by_key[(row["record_type"], int(row["target_id"]))] = row
    for position, hit in enumerate(vector_search(conn, repo_id, query, limit=10)):
        payload = hit.get("payload") or {}
        key = (payload.get("target_type"), int(payload.get("target_id", 0)))
        existing = by_key.get(key)
        if existing is not None:
            existing["vector_rank"] = position
            continue
        target = _target_for_vector(conn, key[0], key[1])
        if target:
            target["hit"] = "semantic alias match"
            target["why"] = "semantic alias match; inspect before trusting"
            target["fts_rank"] = None
            target["vector_rank"] = position
            by_key[key] = target
    return list(by_key.values())


def _fts(conn: sqlite3.Connection, slug: str, query: str, limit: int) -> list[dict]:
    match = quote_fts_query(query)
    try:
        rows = conn.execute(
            """
            SELECT rowid, record_type, target_id, path, symbol, module,
                   snippet(code_fts, 6, '[', ']', '...', 12) AS hit,
                   bm25(code_fts) AS rank
            FROM code_fts
            WHERE repo_slug = ? AND code_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (slug, match, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    results = []
    for row in rows:
        item = dict(row)
        item.update(_target_lines(conn, item["record_type"], int(item["target_id"])))
        item["why"] = _why(conn, item)
        results.append(item)
    return results


def _target_for_vector(conn: sqlite3.Connection, target_type: str, target_id: int) -> dict | None:
    if target_type == "symbol":
        row = conn.execute("SELECT s.id AS target_id, f.path, s.symbol_ref AS symbol, s.signature, s.line_start, s.line_end FROM symbols s JOIN files f ON f.id = s.file_id WHERE s.id = ?", (target_id,)).fetchone()
        if row:
            return {"record_type": "symbol", **dict(row)}
    if target_type == "file":
        row = conn.execute("SELECT id AS target_id, path, '' AS symbol, 1 AS line_start, min(line_count, 80) AS line_end FROM files WHERE id = ?", (target_id,)).fetchone()
        if row:
            return {"record_type": "file", **dict(row)}
    if target_type == "module":
        row = conn.execute("SELECT id AS target_id, path_prefix AS path, '' AS symbol, 1 AS line_start, 1 AS line_end FROM modules WHERE id = ?", (target_id,)).fetchone()
        if row:
            return {"record_type": "module", **dict(row)}
    return None


def _target_lines(conn: sqlite3.Connection, record_type: str, target_id: int) -> dict:
    if record_type == "symbol":
        row = conn.execute("SELECT line_start, line_end, signature FROM symbols WHERE id = ?", (target_id,)).fetchone()
        return {"line_start": row["line_start"], "line_end": row["line_end"], "signature": row["signature"]} if row else {"line_start": 1, "line_end": 1}
    if record_type == "file":
        row = conn.execute("SELECT line_count FROM files WHERE id = ?", (target_id,)).fetchone()
        return {"line_start": 1, "line_end": min(row["line_count"], 80)} if row else {"line_start": 1, "line_end": 1}
    return {"line_start": 1, "line_end": 1}


def _why(conn: sqlite3.Connection, item: dict) -> str:
    if item["record_type"] == "symbol":
        # flags first (highest-signal behaviour labels), then calls; top 3 total
        flags = conn.execute("SELECT flag, evidence_value FROM heuristic_flags WHERE target_type = 'symbol' AND target_id = ? LIMIT 3", (item["target_id"],)).fetchall()
        calls = conn.execute("SELECT call_text FROM symbol_calls WHERE symbol_id = ? LIMIT 3", (item["target_id"],)).fetchall()
        parts = [f"{row['flag']} ({row['evidence_value']})" for row in flags] + [row["call_text"] for row in calls]
        deduped = list(dict.fromkeys(parts))[:3]
        if deduped:
            return ", ".join(deduped)
    return " ".join((item.get("hit") or "lexical match").split())
