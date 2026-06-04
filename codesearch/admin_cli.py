from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from codesearch.config import settings
from codesearch.db.sqlite import connect, init_db
from codesearch.indexing.classify_paths import add_rule, audit_path, classify_path, matching_paths_for_rule, normalize_rel_path, remove_rule
from codesearch.indexing.jobs import expected_models, model_queue
from codesearch.indexing.refresh import exclude_path_records, get_repo, index_repo, re_evaluate_paths, register_repo
from codesearch.model.embed import qdrant_health
from codesearch.model.ollama import health as ollama_health
from codesearch.model.pipeline import run_embeddings, run_queued_jobs

FORMATTER = argparse.RawDescriptionHelpFormatter

TOP_LEVEL_HELP = """\
Common workflows:
  code-search-admin init
  code-search-admin register /path/to/repo repo-slug
  code-search-admin index repo-slug

  code-search-admin inspect-paths repo-slug --excluded
  code-search-admin inspect-file repo-slug path/to/file.py
  code-search-admin model-queue

Manual indexing rules:
  code-search-admin exclude-path repo-slug build --dir --reason "generated output"
  code-search-admin include-path repo-slug vendor/schema.sql --reason "important schema"
  code-search-admin inspect-paths repo-slug --rules

Batching model work:
  Add --no-model to index/reindex/rule commands to queue model work without running it.
  Later run: code-search-admin run-queued-jobs

Default database:
  storage/code-search/code-search.db
  Override with CODE_SEARCH_DB_PATH=/tmp/code-search-test.db

Rule priority, lowest to highest:
  auto classification < exclude dir < include dir < exclude file < include file

Use the public code-search CLI for normal lookup:
  code-search repo-slug "natural language query"
  code-search open repo-slug r_01

Output:
  inspect-paths --included and --excluded print paths only by default.
  Add --json to any command for full structured output.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code-search-admin",
        description="Admin maintenance for the local code-search index.",
        epilog=TOP_LEVEL_HELP,
        formatter_class=FORMATTER,
    )
    sub = parser.add_subparsers(dest="command", required=True, title="commands", metavar="<command>")
    sub.add_parser(
        "status",
        help="Show database, model, and vector service status.",
        description="Show database path, SQLite availability, Qdrant health, Ollama health, and configured model names.",
        formatter_class=FORMATTER,
    )
    sub.add_parser(
        "init",
        help="Create or migrate the SQLite database.",
        description="Create or migrate the code-search SQLite database at CODE_SEARCH_DB_PATH, defaulting to storage/code-search/code-search.db.",
        formatter_class=FORMATTER,
    )
    register = sub.add_parser(
        "register",
        help="Register a repository path under a search slug.",
        description="Register a source repository so public and admin commands can refer to it by slug.",
        epilog="""\
Examples:
  code-search-admin register /path/to/repo myrepo
  code-search-admin register . myrepo
""",
        formatter_class=FORMATTER,
    )
    register.add_argument("repo_path", help="Repository root path to index.")
    register.add_argument("slug", nargs="?", help="Optional stable project slug. Defaults to the repo directory name.")
    index = sub.add_parser(
        "index",
        help="Index a repository, then run queued model/vector jobs unless --no-model is used.",
        description="Discover files broadly, audit include/exclude decisions, index included files, rebuild modules/FTS, queue model jobs, then run the model/vector pipeline unless disabled.",
        epilog="""\
Examples:
  code-search-admin index myrepo
  code-search-admin index myrepo --no-model

Use --no-model when batching several admin changes. Deterministic indexing still runs.
""",
        formatter_class=FORMATTER,
    )
    index.add_argument("repo_slug", help="Registered repository slug.")
    index.add_argument("--no-model", action="store_true", help="Queue model/vector jobs but do not run them.")
    index.add_argument("--enrich", action="store_true", help="Also run the opt-in summary-model enrichment layer (slow). Default: deterministic + embeddings only.")
    reindex = sub.add_parser(
        "reindex",
        help="Reindex all included files and refresh search storage.",
        description="Re-evaluate all discovered paths, remove newly excluded records, rebuild deterministic records, queue jobs, and refresh FTS.",
        formatter_class=FORMATTER,
    )
    reindex.add_argument("repo_slug", help="Registered repository slug.")
    reindex.add_argument("--no-model", action="store_true", help="Queue model/vector jobs but do not run them.")
    reindex.add_argument("--enrich", action="store_true", help="Also run the opt-in summary-model enrichment layer (slow). Default: deterministic + embeddings only.")
    rf = sub.add_parser(
        "reindex-file",
        help="Re-evaluate and reindex one repo-relative path.",
        description="Classify one repo-relative path. If included, reindex it; if excluded, delete its indexed records and audit the reason.",
        epilog="Example:\n  code-search-admin reindex-file myrepo codesearch/indexing/discovery.py",
        formatter_class=FORMATTER,
    )
    rf.add_argument("repo_slug", help="Registered repository slug.")
    rf.add_argument("path", help="Repo-relative file path to re-evaluate.")
    rf.add_argument("--no-model", action="store_true", help="Queue model/vector jobs but do not run them.")
    rf.add_argument("--enrich", action="store_true", help="Also run the opt-in summary-model enrichment layer (slow). Default: deterministic + embeddings only.")
    include = sub.add_parser(
        "include-path",
        help="Add a manual include rule and re-evaluate affected paths.",
        description="Force a file or directory prefix to be included unless a higher-priority manual file exclusion exists.",
        epilog="""\
Examples:
  code-search-admin include-path myrepo vendor/schema.sql --reason "important schema"
  code-search-admin include-path myrepo docs --dir --reason "documentation"
""",
        formatter_class=FORMATTER,
    )
    _path_rule_args(include)
    exclude = sub.add_parser(
        "exclude-path",
        help="Add a manual exclude rule and immediately delete matching indexed records.",
        description="Exclude a file or directory prefix. Matching indexed records are deleted from files, symbols, FTS, cache, model jobs/outputs, vector_records, and Qdrant points when reachable.",
        epilog="""\
Examples:
  code-search-admin exclude-path myrepo build --dir --reason "generated output"
  code-search-admin exclude-path myrepo docs/huge_dump.log --reason "raw log"
""",
        formatter_class=FORMATTER,
    )
    _path_rule_args(exclude)
    remove_include = sub.add_parser(
        "remove-include",
        help="Remove a manual include rule and re-evaluate affected paths.",
        description="Remove a repo-scoped include rule, then re-classify and reindex/delete affected paths based on remaining rules and auto classification.",
        formatter_class=FORMATTER,
    )
    _path_remove_args(remove_include)
    remove_exclude = sub.add_parser(
        "remove-exclude",
        help="Remove a manual exclude rule and re-evaluate affected paths.",
        description="Remove a repo-scoped exclude rule, then re-classify and reindex/delete affected paths based on remaining rules and auto classification.",
        formatter_class=FORMATTER,
    )
    _path_remove_args(remove_exclude)
    inspect_paths = sub.add_parser(
        "inspect-paths",
        help="Inspect path classification/audit decisions or manual rules.",
        description="Show deterministic path-audit rows. By default the indexer excludes gitignored files (reason 'gitignored') and structural hard-skips such as .git, .venv, node_modules, __pycache__, and .code-search; a manual include rule overrides gitignore, and CODE_SEARCH_RESPECT_GITIGNORE=false disables gitignore filtering entirely.",
        epilog="""\
Examples:
  code-search-admin inspect-paths myrepo --excluded
  code-search-admin inspect-paths myrepo --included
  code-search-admin inspect-paths myrepo --rules

Common exclusion reasons: binary_sample, cache_file, database_file, env_file,
generated_file, log_file, too_large, unreadable, unknown_kind.
""",
        formatter_class=FORMATTER,
    )
    inspect_paths.add_argument("repo_slug", help="Registered repository slug.")
    group = inspect_paths.add_mutually_exclusive_group()
    group.add_argument("--included", action="store_true", help="Show included paths.")
    group.add_argument("--excluded", action="store_true", help="Show excluded paths.")
    group.add_argument("--skipped", action="store_true", help="Show skipped paths.")
    group.add_argument("--rules", action="store_true", help="Show manual include/exclude rules.")
    sub.add_parser("list-repos", help="List registered repositories.", description="List registered repositories and their last indexed timestamp.", formatter_class=FORMATTER)
    inspect_file = sub.add_parser(
        "inspect-file",
        help="Inspect one indexed file and its audit decision.",
        description="Show file metadata, audit decision, extracted facts, and symbols for a repo-relative path. If the file is excluded, the audit row is still shown.",
        formatter_class=FORMATTER,
    )
    inspect_file.add_argument("repo_slug", help="Registered repository slug.")
    inspect_file.add_argument("path", help="Repo-relative path to inspect.")
    inspect_symbol = sub.add_parser(
        "inspect-symbol",
        help="Inspect a symbol by symbol_ref or result id.",
        description="Show deterministic symbol metadata, calls, literals, and heuristic flags for a symbol reference or recent result id.",
        formatter_class=FORMATTER,
    )
    inspect_symbol.add_argument("repo_slug", help="Registered repository slug.")
    inspect_symbol.add_argument("symbol_or_result_id", help="Full symbol_ref or recent result id such as r_01.")
    inspect_record = sub.add_parser("inspect-record", help="Inspect one FTS record by rowid.", description="Inspect one raw FTS row by rowid.", formatter_class=FORMATTER)
    inspect_record.add_argument("record_id", help="FTS rowid to inspect.")
    sub.add_parser("model-queue", help="Show pending/running/done model and embedding jobs.", description="Summarize queued, running, done, and errored model/embedding jobs by model and job type.", formatter_class=FORMATTER)
    run_jobs = sub.add_parser("run-queued-jobs", help="Run queued embedding jobs (and enrichment with --enrich), then rebuild FTS.", description="Drain queued model work. By default runs only embeddings (the lightweight semantic layer). With --enrich, runs the opt-in summary-model enrichment first (gated: embeddings wait until enrichment completes), rebuilds FTS, then embeds. Requires Ollama and Qdrant.", formatter_class=FORMATTER)
    run_jobs.add_argument("--enrich", action="store_true", help="Run the opt-in summary-model enrichment layer before embeddings (slow).")
    sub.add_parser("parser-smoke", help="Run Tree-sitter parser smoke tests for installed parsers.", description="Run small parser/extractor smoke snippets for every installed parser language.", formatter_class=FORMATTER)
    sub.add_parser("vacuum", help="Vacuum the SQLite database.", description="Run SQLite VACUUM on the code-search database.", formatter_class=FORMATTER)
    delete_repo = sub.add_parser(
        "delete-repo-index",
        help="Delete all SQLite index records for one repo. Does not delete source files.",
        description="Delete one registered repository and cascading index records from SQLite. Does not delete source files. Requires --yes.",
        formatter_class=FORMATTER,
    )
    delete_repo.add_argument("repo_slug", help="Registered repository slug to delete from the index.")
    delete_repo.add_argument("--yes", action="store_true", help="Required confirmation; deletes the repo index, FTS rows, cache rows, and queued jobs.")
    reset = sub.add_parser(
        "reset",
        help="Delete the entire code-search SQLite database and recreate an empty one.",
        description="Delete the code-search SQLite database and recreate an empty schema. Does not delete source repos, Qdrant storage dirs, or Ollama models. Requires --yes.",
        formatter_class=FORMATTER,
    )
    reset.add_argument("--yes", action="store_true", help="Required confirmation; deletes all repo indexes, jobs, outputs, audit rows, and FTS rows.")
    return parser


def _path_rule_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("repo_slug", help="Registered repository slug.")
    parser.add_argument("path", help="Repo-relative file path, or directory prefix when --dir is set.")
    parser.add_argument("--dir", action="store_true", help="Treat path as a directory prefix and apply recursively.")
    parser.add_argument("--reason", help="Stable human reason stored with the manual rule.")
    parser.add_argument("--no-model", action="store_true", help="Queue model/vector jobs but do not run them.")


def _path_remove_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("repo_slug", help="Registered repository slug.")
    parser.add_argument("path", help="Repo-relative file path, or directory prefix when --dir is set.")
    parser.add_argument("--dir", action="store_true", help="Treat path as a directory prefix.")
    parser.add_argument("--no-model", action="store_true", help="Queue model/vector jobs but do not run them.")


def main() -> None:
    parser = build_parser()
    argv = sys.argv[1:]
    as_json = "--json" in argv
    args = parser.parse_args([arg for arg in argv if arg != "--json"])
    args.json = as_json
    conn = connect()
    init_db(conn)
    try:
        result = dispatch(conn, args)
        print_result(args, result)
        if result.get("ok") is False:
            raise SystemExit(1)
    finally:
        conn.close()


def print_result(args, result: dict) -> None:
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return
    if args.command == "inspect-paths" and (args.included or args.excluded) and result.get("ok"):
        paths = [row["path"] for row in result.get("paths", [])]
        if paths:
            sys.stdout.write("\n".join(paths) + "\n")
        return
    print(json.dumps(result, indent=2, sort_keys=True, default=str))


def dispatch(conn, args) -> dict:
    if args.command == "status":
        return {
            "ok": True,
            "db_path": str(settings.db_path),
            "sqlite": True,
            "qdrant": qdrant_health(),
            "ollama": ollama_health(),
            "models": expected_models(),
        }
    if args.command == "init":
        return {"ok": True, "db_path": str(settings.db_path)}
    if args.command == "register":
        return register_repo(conn, args.repo_path, args.slug)
    if args.command == "index":
        return _with_pipeline(conn, index_repo(conn, args.repo_slug, enrich=args.enrich), args.repo_slug, args.no_model, args.enrich)
    if args.command == "reindex":
        return _with_pipeline(conn, index_repo(conn, args.repo_slug, changed_only=False, enrich=args.enrich), args.repo_slug, args.no_model, args.enrich)
    if args.command == "reindex-file":
        return _with_pipeline(conn, index_repo(conn, args.repo_slug, changed_only=False, only_path=args.path, enrich=args.enrich), args.repo_slug, args.no_model, args.enrich)
    if args.command == "include-path":
        return _manual_rule(conn, args.repo_slug, args.path, "dir" if args.dir else "file", "include", args.reason, args.no_model)
    if args.command == "exclude-path":
        return _manual_rule(conn, args.repo_slug, args.path, "dir" if args.dir else "file", "exclude", args.reason, args.no_model)
    if args.command == "remove-include":
        return _remove_manual_rule(conn, args.repo_slug, args.path, "dir" if args.dir else "file", "include", args.no_model)
    if args.command == "remove-exclude":
        return _remove_manual_rule(conn, args.repo_slug, args.path, "dir" if args.dir else "file", "exclude", args.no_model)
    if args.command == "inspect-paths":
        return inspect_paths(conn, args)
    if args.command == "list-repos":
        rows = conn.execute("SELECT slug, root_path, display_name, vcs_head, last_indexed_at FROM repos ORDER BY slug").fetchall()
        return {"ok": True, "repos": [dict(row) for row in rows]}
    if args.command == "inspect-file":
        return inspect_file(conn, args.repo_slug, args.path)
    if args.command == "inspect-symbol":
        return inspect_symbol(conn, args.repo_slug, args.symbol_or_result_id)
    if args.command == "inspect-record":
        return inspect_record(conn, args.record_id)
    if args.command == "model-queue":
        return model_queue(conn)
    if args.command == "run-queued-jobs":
        return run_queued_jobs(conn) if args.enrich else run_embeddings(conn)
    if args.command == "parser-smoke":
        from codesearch.indexing.tree_sitter_extract.smoke import parser_smoke

        return parser_smoke()
    if args.command == "vacuum":
        conn.execute("VACUUM")
        return {"ok": True}
    if args.command == "delete-repo-index":
        if not args.yes:
            return {"ok": False, "error": "confirmation_required", "message": "Pass --yes to delete a repo index."}
        row = get_repo(conn, args.repo_slug)
        if row:
            conn.execute("DELETE FROM repos WHERE id = ?", (row["id"],))
            conn.execute("DELETE FROM code_fts WHERE repo_slug = ?", (args.repo_slug,))
            conn.commit()
        return {"ok": True, "deleted": args.repo_slug}
    if args.command == "reset":
        if not args.yes:
            return {"ok": False, "error": "confirmation_required", "message": "Pass --yes to reset the entire code-search database."}
        conn.close()
        settings.db_path.unlink(missing_ok=True)
        fresh = connect()
        try:
            init_db(fresh)
        finally:
            fresh.close()
        return {"ok": True}
    return {"ok": False, "error": "unknown_command"}


def _with_pipeline(conn, result: dict, slug: str, no_model: bool, enrich: bool = False) -> dict:
    if not result.get("ok"):
        return result
    repo = get_repo(conn, slug)
    if no_model:
        result["model_pipeline"] = {"ran": False, "reason": "no_model", "queue": model_queue(conn)["jobs"]}
    elif repo and enrich:
        result["model_pipeline"] = run_queued_jobs(conn, repo["id"], slug)
    elif repo:
        result["model_pipeline"] = run_embeddings(conn)
    return result


def _manual_rule(conn, slug: str, path: str, path_kind: str, action: str, reason: str | None, no_model: bool) -> dict:
    repo = get_repo(conn, slug)
    if not repo:
        return {"ok": False, "error": "repo_not_registered"}
    rule_id = add_rule(conn, repo["id"], path, path_kind, action, reason)
    paths = _affected_paths(conn, repo, path, path_kind)
    if action == "exclude":
        result = exclude_path_records(conn, slug, path, recursive=path_kind == "dir")
        root = Path(repo["root_path"])
        for rel_path in paths:
            classification = classify_path(conn, repo["id"], root, rel_path)
            audit_path(conn, repo["id"], rel_path, classification, "excluded")
        conn.commit()
    else:
        result = re_evaluate_paths(conn, slug, paths)
    result.update({"rule_id": rule_id, "action": action, "path_kind": path_kind, "path": normalize_rel_path(path)})
    return _with_pipeline(conn, result, slug, no_model)


def _remove_manual_rule(conn, slug: str, path: str, path_kind: str, action: str, no_model: bool) -> dict:
    repo = get_repo(conn, slug)
    if not repo:
        return {"ok": False, "error": "repo_not_registered"}
    paths = _affected_paths(conn, repo, path, path_kind)
    removed = remove_rule(conn, repo["id"], path, path_kind, action)
    result = re_evaluate_paths(conn, slug, paths)
    result.update({"removed": removed, "action": action, "path_kind": path_kind, "path": normalize_rel_path(path)})
    return _with_pipeline(conn, result, slug, no_model)


def _affected_paths(conn, repo, path: str, path_kind: str) -> list[str]:
    root = Path(repo["root_path"])
    paths = set(matching_paths_for_rule(root, path, path_kind))
    rel_path = normalize_rel_path(path).rstrip("/")
    if path_kind == "dir":
        for row in conn.execute("SELECT path FROM files WHERE repo_id = ? AND (path = ? OR path LIKE ?)", (repo["id"], rel_path, f"{rel_path}/%")).fetchall():
            paths.add(row["path"])
        for row in conn.execute("SELECT path FROM path_audit WHERE repo_id = ? AND (path = ? OR path LIKE ?)", (repo["id"], rel_path, f"{rel_path}/%")).fetchall():
            paths.add(row["path"])
    return sorted(paths)


def inspect_file(conn, slug: str, path: str) -> dict:
    repo = get_repo(conn, slug)
    if not repo:
        return {"ok": False, "error": "repo_not_registered"}
    file_row = conn.execute("SELECT * FROM files WHERE repo_id = ? AND path = ?", (repo["id"], path)).fetchone()
    audit = conn.execute("SELECT * FROM path_audit WHERE repo_id = ? AND path = ?", (repo["id"], path)).fetchone()
    if not file_row:
        return {"ok": False, "error": "file_not_found", "audit": dict(audit) if audit else None}
    symbols = conn.execute("SELECT symbol_ref, symbol_type, line_start, line_end FROM symbols WHERE file_id = ? ORDER BY line_start", (file_row["id"],)).fetchall()
    facts = conn.execute("SELECT fact_type, value, line_start, line_end FROM file_facts WHERE file_id = ? LIMIT 100", (file_row["id"],)).fetchall()
    return {"ok": True, "file": dict(file_row), "audit": dict(audit) if audit else None, "symbols": [dict(row) for row in symbols], "facts": [dict(row) for row in facts]}


def inspect_paths(conn, args) -> dict:
    repo = get_repo(conn, args.repo_slug)
    if not repo:
        return {"ok": False, "error": "repo_not_registered"}
    if args.rules:
        rows = conn.execute("SELECT * FROM indexing_rules WHERE repo_id = ? ORDER BY path_kind, path, action", (repo["id"],)).fetchall()
        return {"ok": True, "rules": [dict(row) for row in rows]}
    decision = "included" if args.included else "excluded" if args.excluded else "skipped" if args.skipped else None
    if decision:
        rows = conn.execute("SELECT * FROM path_audit WHERE repo_id = ? AND decision = ? ORDER BY path LIMIT 500", (repo["id"], decision)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM path_audit WHERE repo_id = ? ORDER BY path LIMIT 500", (repo["id"],)).fetchall()
    return {"ok": True, "paths": [dict(row) for row in rows]}


def inspect_symbol(conn, slug: str, target: str) -> dict:
    repo = get_repo(conn, slug)
    if not repo:
        return {"ok": False, "error": "repo_not_registered"}
    from codesearch.retrieval.resolve import resolve_target

    resolved = resolve_target(conn, repo, target)
    if not resolved.get("ok"):
        return resolved
    if resolved["type"] != "symbol":
        return {"ok": False, "error": "not_symbol"}
    symbol = conn.execute("SELECT * FROM symbols WHERE id = ?", (resolved["id"],)).fetchone()
    calls = conn.execute("SELECT * FROM symbol_calls WHERE symbol_id = ?", (resolved["id"],)).fetchall()
    literals = conn.execute("SELECT * FROM symbol_literals WHERE symbol_id = ?", (resolved["id"],)).fetchall()
    flags = conn.execute("SELECT * FROM heuristic_flags WHERE target_type = 'symbol' AND target_id = ?", (resolved["id"],)).fetchall()
    return {"ok": True, "symbol": dict(symbol), "calls": [dict(row) for row in calls], "literals": [dict(row) for row in literals], "heuristic_flags": [dict(row) for row in flags]}


def inspect_record(conn, record_id: str) -> dict:
    rowid = int(record_id)
    row = conn.execute("SELECT rowid, * FROM code_fts WHERE rowid = ?", (rowid,)).fetchone()
    return {"ok": bool(row), "record": dict(row) if row else None}


if __name__ == "__main__":
    main()
