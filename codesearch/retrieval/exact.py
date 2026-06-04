from __future__ import annotations

import sqlite3

from codesearch.retrieval.resolve import cache_results, repo, stale_paths

KINDS = ("symbol", "call", "literal", "path")
DEFAULT_LIMIT = 10


def find(conn: sqlite3.Connection, slug: str, token: str, kind: str | None = None, limit: int | None = None) -> dict:
    repo_row = repo(conn, slug)
    if not repo_row:
        return {"ok": False, "error": "repo_not_registered", "message": f"No repo registered as {slug}."}
    repo_id = repo_row["id"]
    tok = token.strip()
    if not tok:
        return {"ok": False, "error": "empty_query", "message": "find needs a token."}
    like = f"%{tok}%"
    kinds = {kind} if kind else set(KINDS)
    by_key: dict[tuple, dict] = {}

    def add(record_type: str, target_id: int, base: dict, why: str, score: int) -> None:
        key = (record_type, target_id)
        if key not in by_key or score > by_key[key]["score"]:
            by_key[key] = {**base, "record_type": record_type, "target_id": target_id, "why": why, "score": score}

    if "symbol" in kinds:
        for s in conn.execute(
            "SELECT s.id, s.name, s.symbol_ref, s.signature, s.line_start, s.line_end, f.path "
            "FROM symbols s JOIN files f ON f.id = s.file_id "
            "WHERE s.repo_id = ? AND (s.name LIKE ? OR s.qualified_name LIKE ? OR s.symbol_ref LIKE ?)",
            (repo_id, like, like, like),
        ).fetchall():
            add("symbol", s["id"], _symbol_base(s), "definition", _rank(tok, s["name"]) + 5)
    if "call" in kinds:
        for c in conn.execute(
            "SELECT sc.symbol_id, sc.call_text, sc.function_name, s.symbol_ref, s.signature, s.line_start, s.line_end, f.path "
            "FROM symbol_calls sc JOIN symbols s ON s.id = sc.symbol_id JOIN files f ON f.id = s.file_id "
            "WHERE s.repo_id = ? AND (sc.call_text LIKE ? OR sc.function_name LIKE ?)",
            (repo_id, like, like),
        ).fetchall():
            add("symbol", c["symbol_id"], _symbol_base(c), f"call site: {c['call_text']}", _rank(tok, c["function_name"] or c["call_text"]))
    if "literal" in kinds:
        for lit in conn.execute(
            "SELECT sl.symbol_id, sl.value, s.symbol_ref, s.signature, s.line_start, s.line_end, f.path "
            "FROM symbol_literals sl JOIN symbols s ON s.id = sl.symbol_id JOIN files f ON f.id = s.file_id "
            "WHERE s.repo_id = ? AND sl.value LIKE ?",
            (repo_id, like),
        ).fetchall():
            add("symbol", lit["symbol_id"], _symbol_base(lit), f"literal: {_short(lit['value'])}", _rank(tok, lit["value"]) - 10)
    if "path" in kinds:
        for fr in conn.execute(
            "SELECT id, path, line_count FROM files WHERE repo_id = ? AND path LIKE ? AND skipped_reason IS NULL",
            (repo_id, like),
        ).fetchall():
            add("file", fr["id"], {"path": fr["path"], "symbol": None, "signature": None, "line_start": 1, "line_end": min(fr["line_count"], 80)}, "path", _rank(tok, fr["path"]))

    ranked = sorted(by_key.values(), key=lambda r: r["score"], reverse=True)[: (limit or DEFAULT_LIMIT)]
    stale = stale_paths(conn, repo_id, repo_row["root_path"], [r["path"] for r in ranked])
    for row in ranked:
        row["stale"] = row["path"] in stale
    cache_results(conn, repo_id, ranked, token)
    return {"ok": True, "query": token, "results": ranked, "stale_count": len(stale)}


def _symbol_base(row: sqlite3.Row) -> dict:
    return {"path": row["path"], "symbol": row["symbol_ref"], "signature": row["signature"], "line_start": row["line_start"], "line_end": row["line_end"]}


def _rank(token: str, value: str | None) -> int:
    # exact > prefix > substring, case-insensitive
    if not value:
        return 0
    v, t = value.lower(), token.lower()
    if v == t:
        return 100
    if v.startswith(t):
        return 60
    if t in v:
        return 30
    return 0


def _short(value: str) -> str:
    flat = " ".join((value or "").split())
    return flat[:60] + "…" if len(flat) > 60 else flat
