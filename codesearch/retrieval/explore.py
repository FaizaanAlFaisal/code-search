from __future__ import annotations

import sqlite3
from collections import OrderedDict
from pathlib import Path

from codesearch.model.embed import fetch_vectors
from codesearch.retrieval.dedup import collapse_families
from codesearch.retrieval.rank import _is_test_target, rerank
from codesearch.retrieval.resolve import cache_results, repo, stale_paths
from codesearch.retrieval.search import _collect_candidates

SEED_LIMIT = 5
SEED_SCORE_RATIO = 0.5  # drop seeds scoring far below the top hit (kills weak/off-target fill)
MAX_SYMBOLS = 12        # stop at the relevant spine instead of padding to budget
PER_SYMBOL_CAP = 1600   # chars of body for a seed
NEIGHBOR_CAP = 600      # spine neighbors get a preview, not a full body


def explore(conn: sqlite3.Connection, slug: str, query: str) -> dict:
    repo_row = repo(conn, slug)
    if not repo_row:
        return {"ok": False, "error": "repo_not_registered", "message": f"No repo registered as {slug}."}
    repo_id = repo_row["id"]
    root = Path(repo_row["root_path"])
    tier = _tier(conn, repo_id)

    ranked = rerank(_collect_candidates(conn, repo_id, slug, query), query)
    symbol_hits = [r for r in ranked if r.get("record_type") == "symbol" and not _is_test_target(r.get("path"), r.get("symbol"))]
    seeds = symbol_hits[:SEED_LIMIT]
    if not seeds:
        return {"ok": True, "query": query, "files": [], "blast": [], "truncated": False, "note": "no symbol matches"}
    # relevance gate: keep only seeds within SEED_SCORE_RATIO of the top hit
    cutoff = seeds[0].get("score", 0) * SEED_SCORE_RATIO
    seeds = [s for s in seeds if s.get("score", 0) >= cutoff]
    seed_ids = [r["target_id"] for r in seeds]
    seed_set = set(seed_ids)

    # spine = seeds + direct call neighbors, seeds first
    order = list(seed_ids)
    seen = set(seed_ids)
    for sid in seed_ids:
        for nid in _neighbors(conn, repo_id, sid):
            if nid not in seen:
                seen.add(nid)
                order.append(nid)

    collapsed = _dedup(conn, repo_id, order, set(seed_ids), tier)
    rep_ref = _rep_refs(conn, set(collapsed.values()))

    used = 0
    files: "OrderedDict[str, list]" = OrderedDict()
    included: list[dict] = []
    stale_seen: set[str] = set()
    stale_cache: dict[str, bool] = {}
    for sid in order:
        sym = _symbol(conn, sid)
        if not sym:
            continue
        path = sym["path"]
        if path not in stale_cache:
            stale_cache[path] = bool(stale_paths(conn, repo_id, str(root), [path]))
        is_stale = stale_cache[path]
        cap = PER_SYMBOL_CAP if sid in seed_set else NEIGHBOR_CAP
        block, size = _render(root, sym, rep_ref.get(collapsed.get(sid)), cap, stale=is_stale)
        if included and (len(included) >= MAX_SYMBOLS or used + size > tier["budget"]):
            break  # stop at the relevant spine; don't pad to budget
        used += size
        if is_stale:
            stale_seen.add(path)
        files.setdefault(path, []).append(block)
        included.append(sym)

    cache_results(conn, repo_id, [_cache_row(s) for s in included], query)
    return {
        "ok": True,
        "query": query,
        "files": [{"path": path, "blocks": blocks} for path, blocks in files.items()],
        "blast": _blast(conn, repo_id, seed_ids),
        "truncated": len(included) < len(order),
        "stale_count": len(stale_seen),
    }


def _tier(conn: sqlite3.Connection, repo_id: int) -> dict:
    # one ladder drives budget + dedup aggressiveness. min_size is the ">N" gate
    # (collapse when family larger than min_size). Floors loosen as repos grow.
    files = conn.execute("SELECT count(*) FROM files WHERE repo_id = ? AND skipped_reason IS NULL", (repo_id,)).fetchone()[0]
    if files < 500:
        return {"budget": 13000, "floor": 0.90, "min_size": 5}   # near-off
    if files < 5000:
        return {"budget": 20000, "floor": 0.82, "min_size": 3}
    return {"budget": 24000, "floor": 0.78, "min_size": 3}


def _dedup(conn: sqlite3.Connection, repo_id: int, order: list[int], protected: set[int], tier: dict) -> dict[int, int]:
    # no family can exceed min_size with too few candidates -> skip the vector fetch entirely
    if len(order) <= tier["min_size"]:
        return {}
    vectors = fetch_vectors(conn, repo_id, "symbol", order)
    if not vectors:
        return {}
    return collapse_families(order, vectors, tier["floor"], tier["min_size"], protected)


def _rep_refs(conn: sqlite3.Connection, rep_ids: set[int]) -> dict[int, str]:
    refs: dict[int, str] = {}
    for rid in rep_ids:
        row = conn.execute("SELECT symbol_ref FROM symbols WHERE id = ?", (rid,)).fetchone()
        if row:
            refs[rid] = row["symbol_ref"]
    return refs


def _neighbors(conn: sqlite3.Connection, repo_id: int, symbol_id: int) -> list[int]:
    row = conn.execute("SELECT name FROM symbols WHERE id = ?", (symbol_id,)).fetchone()
    name = row["name"] if row else ""
    ids: list[int] = []
    for callee in conn.execute("SELECT DISTINCT function_name FROM symbol_calls WHERE symbol_id = ? AND function_name IS NOT NULL", (symbol_id,)).fetchall():
        for d in conn.execute("SELECT id FROM symbols WHERE repo_id = ? AND name = ? LIMIT 2", (repo_id, callee["function_name"])).fetchall():
            ids.append(d["id"])
    for caller in conn.execute(
        "SELECT DISTINCT sc.symbol_id FROM symbol_calls sc JOIN symbols s ON s.id = sc.symbol_id "
        "WHERE s.repo_id = ? AND sc.symbol_id != ? AND (sc.function_name = ? OR sc.call_text LIKE ?) LIMIT 6",
        (repo_id, symbol_id, name, f"%.{name}"),
    ).fetchall():
        ids.append(caller["symbol_id"])
    return ids


def _symbol(conn: sqlite3.Connection, symbol_id: int) -> dict | None:
    row = conn.execute(
        "SELECT s.id, s.symbol_ref, s.signature, s.line_start, s.line_end, f.path, "
        "(SELECT group_concat(flag, ', ') FROM (SELECT DISTINCT flag FROM heuristic_flags WHERE target_type='symbol' AND target_id=s.id LIMIT 4)) AS flags "
        "FROM symbols s JOIN files f ON f.id = s.file_id WHERE s.id = ?",
        (symbol_id,),
    ).fetchone()
    return dict(row) if row else None


def _render(root: Path, sym: dict, collapsed_to: str | None = None, cap: int = PER_SYMBOL_CAP, stale: bool = False) -> tuple[dict, int]:
    base = {
        "symbol_ref": sym["symbol_ref"],
        "signature": sym["signature"],
        "line_start": sym["line_start"],
        "line_end": sym["line_end"],
        "flags": sym.get("flags"),
        "collapsed_to": collapsed_to,
        "stale": stale,
    }
    if stale:
        # file edited since indexing: line numbers are misaligned, so showing live
        # source here would be wrong. Withhold the body; agent decides whether to refresh.
        base["body"] = None
        return base, len(sym["symbol_ref"] or "") + len(sym.get("signature") or "")
    if collapsed_to:
        # near-identical family member: signature only, recoverable via open
        base["body"] = None
        return base, len(sym["symbol_ref"] or "") + len(sym.get("signature") or "")
    body = _read_lines(root / sym["path"], sym["line_start"], sym["line_end"])
    if len(body) > cap:
        body = body[:cap] + "\n… (truncated — open for full body)"
    base["body"] = body
    return base, len(body) + len(sym["symbol_ref"] or "") + len(sym.get("signature") or "")


def _read_lines(path: Path, start: int, end: int) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[max(0, start - 1) : end])


def _blast(conn: sqlite3.Connection, repo_id: int, seed_ids: list[int]) -> list[dict]:
    out = []
    for sid in seed_ids:
        row = conn.execute("SELECT name, symbol_ref FROM symbols WHERE id = ?", (sid,)).fetchone()
        if not row:
            continue
        callers = conn.execute(
            "SELECT DISTINCT s.symbol_ref, f.path FROM symbol_calls sc JOIN symbols s ON s.id = sc.symbol_id JOIN files f ON f.id = s.file_id "
            "WHERE s.repo_id = ? AND sc.symbol_id != ? AND (sc.function_name = ? OR sc.call_text LIKE ?) LIMIT 8",
            (repo_id, sid, row["name"], f"%.{row['name']}"),
        ).fetchall()
        tests = [c["symbol_ref"] for c in callers if _is_test_target(c["path"], c["symbol_ref"])]
        non_test = [c["symbol_ref"] for c in callers if not _is_test_target(c["path"], c["symbol_ref"])]
        out.append({"symbol_ref": row["symbol_ref"], "callers": non_test, "tests": tests})
    return out


def _cache_row(sym: dict) -> dict:
    return {"record_type": "symbol", "target_id": sym["id"], "path": sym["path"], "symbol": sym["symbol_ref"], "line_start": sym["line_start"], "line_end": sym["line_end"]}
