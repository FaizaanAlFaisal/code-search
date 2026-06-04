from __future__ import annotations

import sys
from pathlib import Path

from codesearch.db.sqlite import connect, init_db
from codesearch.formatters import format_explore, format_find, format_open, format_search, print_json
from codesearch.indexing.refresh import index_repo
from codesearch.model.pipeline import run_embeddings
from codesearch.retrieval.exact import find
from codesearch.retrieval.explore import explore
from codesearch.retrieval.resolve import current_hash, repo, resolve_target
from codesearch.retrieval.search import search


def usage() -> str:
    return (
        "Usage:\n"
        "  explore <project> \"<question>\"   verbatim source of the most-relevant symbols, grouped by file — default; one call usually answers it.\n"
        "  open <project> <symbol|r_NN|path> full body of one symbol plus its callers/callees — use when a block says \"… (truncated)\".\n"
        "  find <project> \"<token>\" [--kind symbol|call|literal|path]   every exact definition and call site of a name/literal/path.\n"
        "  <project> \"<query>\"              ranked locations only, no source.\n"
        "  refresh <project>                reindex changed files.\n"
        "  Trust the top results and treat their source as read; only open/find when you need more. Add --json for structured output."
    )


def split_flags(argv: list[str]) -> tuple[list[str], bool]:
    return [arg for arg in argv if arg != "--json"], "--json" in argv


def handle(argv: list[str]) -> int:
    argv, as_json = split_flags(argv)
    conn = connect()
    init_db(conn)
    try:
        if not argv or argv[0] in {"-h", "--help", "help"}:
            print(usage())
            return 0
        if not argv:
            raise ValueError(usage())
        command = argv[0]
        if command == "open":
            if len(argv) != 3:
                raise ValueError(usage())
            result = open_target(conn, argv[1], argv[2])
            print_json(result) if as_json else print(format_open(result, argv[1]))
            return 0 if result.get("ok") else 1
        if command == "find":
            args = argv[1:]
            kind = None
            if "--kind" in args:
                i = args.index("--kind")
                kind = args[i + 1] if i + 1 < len(args) else None
                args = args[:i] + args[i + 2 :]
            if len(args) != 2:
                raise ValueError(usage())
            result = find(conn, args[0], args[1], kind=kind)
            print_json(result) if as_json else print(format_find(result, args[0]))
            return 0 if result.get("ok") else 1
        if command == "explore":
            if len(argv) != 3:
                raise ValueError(usage())
            result = explore(conn, argv[1], argv[2])
            print_json(result) if as_json else print(format_explore(result, argv[1]))
            return 0 if result.get("ok") else 1
        if command == "refresh":
            if len(argv) != 2:
                raise ValueError(usage())
            result = index_repo(conn, argv[1], changed_only=True)
            if result.get("ok"):
                result["secondary"] = run_embeddings(conn)
            print_json(result) if as_json else print(_format_refresh(result, argv[1]))
            return 0 if result.get("ok") else 1
        if len(argv) != 2:
            raise ValueError(usage())
        result = search(conn, argv[0], argv[1])
        print_json(result) if as_json else print(format_search(result, argv[0]))
        return 0 if result.get("ok") else 1
    except ValueError as exc:
        if as_json:
            print_json({"ok": False, "error": "usage", "message": str(exc)})
        else:
            print(str(exc))
        return 1
    finally:
        conn.close()


def open_target(conn, slug: str, target: str) -> dict:
    repo_row = repo(conn, slug)
    if not repo_row:
        return {"ok": False, "error": "repo_not_registered", "message": f"No repo registered as {slug}."}
    resolved = resolve_target(conn, repo_row, target)
    if not resolved.get("ok"):
        return resolved
    path = Path(repo_row["root_path"]) / resolved["path"]
    if resolved["type"] == "module" or path.is_dir():
        rows = conn.execute(
            """
            SELECT f.path, group_concat(s.symbol_ref, ', ') AS symbols
            FROM files f
            LEFT JOIN symbols s ON s.file_id = f.id
            WHERE f.repo_id = ? AND f.path LIKE ?
            GROUP BY f.id
            ORDER BY f.path
            LIMIT 80
            """,
            (repo_row["id"], f"{resolved['path'].rstrip('/')}/%"),
        ).fetchall()
        excerpt = "\n".join(f"{row['path']}: {row['symbols'] or ''}" for row in rows) or "(empty module)"
        return {
            "ok": True,
            "path": resolved["path"],
            "symbol_ref": resolved.get("symbol_ref"),
            "line_start": 1,
            "line_end": max(1, len(rows)),
            "stale": False,
            "excerpt": excerpt,
        }
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {"ok": False, "error": "file_not_found", "message": f"File not found: {resolved['path']}"}
    context = 2 if resolved["type"] == "symbol" else 0
    start = max(1, int(resolved["line_start"]) - context)
    end = min(len(lines), int(resolved["line_end"]) + context)
    file_row = conn.execute("SELECT sha256 FROM files WHERE repo_id = ? AND path = ?", (repo_row["id"], resolved["path"])).fetchone()
    current = current_hash(path)
    indexed = file_row["sha256"] if file_row else None
    return {
        "ok": True,
        "path": resolved["path"],
        "symbol_ref": resolved.get("symbol_ref"),
        "line_start": start,
        "line_end": end,
        "stale": bool(indexed and current and indexed != current),
        "excerpt": "\n".join(lines[start - 1 : end]),
        "trail": _symbol_trail(conn, repo_row["id"], resolved["id"]) if resolved["type"] == "symbol" else None,
    }


def _symbol_trail(conn, repo_id: int, symbol_id: int) -> dict:
    # name-based callers/callees + flags, locations only (no resolver)
    row = conn.execute("SELECT name FROM symbols WHERE id = ?", (symbol_id,)).fetchone()
    name = row["name"] if row else ""
    callers = conn.execute(
        "SELECT DISTINCT s.symbol_ref FROM symbol_calls sc JOIN symbols s ON s.id = sc.symbol_id "
        "WHERE s.repo_id = ? AND sc.symbol_id != ? AND (sc.function_name = ? OR sc.call_text = ? OR sc.call_text LIKE ?) LIMIT 10",
        (repo_id, symbol_id, name, name, f"%.{name}"),
    ).fetchall()
    callees = conn.execute("SELECT DISTINCT call_text FROM symbol_calls WHERE symbol_id = ? LIMIT 12", (symbol_id,)).fetchall()
    flags = conn.execute("SELECT DISTINCT flag FROM heuristic_flags WHERE target_type = 'symbol' AND target_id = ? LIMIT 6", (symbol_id,)).fetchall()
    return {
        "callers": [r["symbol_ref"] for r in callers],
        "callees": [r["call_text"] for r in callees],
        "flags": [r["flag"] for r in flags],
    }


def _format_refresh(result: dict, slug: str) -> str:
    if not result.get("ok"):
        return result.get("message") or result.get("error", "Refresh failed.")
    changed = result.get("indexed", 0)
    unchanged = result.get("unchanged", 0)
    deleted = result.get("deleted", 0)
    total = changed + unchanged
    detail = f"{unchanged} unchanged (skipped)"
    if deleted:
        detail += f", {deleted} removed"
    lines = [f"Refreshed {slug}: reindexed {changed} changed of {total} files — {detail}."]
    secondary = result.get("secondary") or {}
    if secondary.get("ran"):
        lines.append(f"Model jobs: {secondary.get('enrichment', {}).get('processed', 0)} enrichment, {secondary.get('embedding', {}).get('processed', 0)} embedding.")
    else:
        reason = secondary.get("reason")
        if reason:
            lines.append(f"Secondary model/vector work queued but not run: {reason}.")
    return "\n".join(lines)


def main() -> None:
    raise SystemExit(handle(sys.argv[1:]))


if __name__ == "__main__":
    main()
