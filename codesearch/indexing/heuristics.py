from __future__ import annotations

import re
import sqlite3

from codesearch.db.fts import _split_identifier

# match verb in a call's (tokenized) function name. ambiguous verbs are
# gated on a coarse receiver class. calls can have multiple flags

VERB_FLAGS: dict[str, set[str]] = {
    "destructive_clear": {"drop", "truncate", "destroy", "purge", "wipe", "clear", "erase", "reset"}, # more aggressive than delete
    "destructive_delete": {"delete", "remove", "discard", "unlink", "prune", "pop"}, # soft delete
    "append_write": {"append", "push", "extend", "concat"},
    "insert_write": {"insert", "create", "register"},
    "update_write": {"update", "upsert", "patch", "modify", "edit", "replace"},
    "overwrite_write": {"write", "overwrite", "save", "store", "persist", "dump", "flush", "commit"},
    "data_read": {"read", "load", "fetch", "select", "query", "find", "list", "scan"},
    "network_call": {"request", "send", "download", "upload", "connect"},
    "vector_search": {"search", "knn", "ann", "similarity", "retrieve", "cosine", "euclidean", "manhattan"},
    "embedding_call": {"embed", "embedding", "vectorize"},
    "generation_call": {"generate", "complete", "completion", "chat", "prompt", "llm", "agent"},
}

RECEIVER_CLASSES: dict[str, set[str]] = {
    "db": {"db", "database", "session", "sess", "repo", "repository", "store", "dao", "cursor",
           "conn", "connection", "table", "collection", "coll", "model", "orm", "sql", "redis",
           "cache", "mongo", "postgres", "pg", "sqlite", "engine"},
    "net": {"client", "http", "https", "api", "request", "requests", "urllib", "httpx", "axios",
            "socket", "sock", "ws", "websocket", "url", "rpc", "grpc", "channel", "session",
            "gateway", "service", "svc", "backend", "remote", "upstream", "endpoint",
            "bucket", "transport", "gql", "graphql"},
    "fs": {"path", "file", "fs", "os", "io", "dir", "directory", "stream", "handle", "fh", "fp",
           "shutil", "tempfile", "glob"},
}

GATED_RULES: list[tuple[str, set[str], str]] = [
    ("net", {"get", "post", "put", "patch", "head", "delete", "fetch", "request", "send",
             "connect", "open", "stream", "download", "upload"}, "network_call"),
    ("db", {"get", "find", "list", "scan", "load", "fetch", "select", "query", "read"}, "data_read"),
    ("db", {"set", "put", "save", "store", "write"}, "overwrite_write"),
    ("fs", {"open", "read", "write", "close", "create", "remove", "copy", "move", "rename",
            "mkdir", "makedirs", "scandir", "listdir"}, "filesystem_access"),
]

_JOB_WORKER = {"job", "jobs", "task", "tasks", "worker", "workers"}
_JOB_DEFER = {"queue", "enqueue", "background", "async", "defer", "dispatch", "schedule", "spawn", "delay"}
_JOB_STRONG = {"enqueue", "dispatch", "schedule", "spawn"}

_RUN = re.compile(r"[A-Za-z0-9_-]+")


def _tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    out: set[str] = set()
    for run in _RUN.findall(text):
        out.update(part.lower() for part in _split_identifier(run))
    return out


def _flags_for(function_name: str, receiver: str | None) -> list[str]:
    ftoks = _tokens(function_name)
    if not ftoks:
        return []
    flags: list[str] = [flag for flag, verbs in VERB_FLAGS.items() if ftoks & verbs]
    if (ftoks & _JOB_STRONG) or (ftoks & _JOB_WORKER and ftoks & _JOB_DEFER):
        flags.append("background_job")
    rclasses = {cls for cls, terms in RECEIVER_CLASSES.items() if _tokens(receiver) & terms}
    for cls, verbs, flag in GATED_RULES:
        if cls in rclasses and ftoks & verbs:
            flags.append(flag)
    return list(dict.fromkeys(flags))


def apply_heuristics(conn: sqlite3.Connection, repo_id: int) -> None:
    conn.execute(
        "DELETE FROM heuristic_flags WHERE target_type = 'symbol' AND target_id IN (SELECT id FROM symbols WHERE repo_id = ?)",
        (repo_id,),
    )
    calls = conn.execute(
        """
        SELECT sc.symbol_id, sc.call_text, sc.function_name, sc.receiver, sc.line_start, sc.line_end
        FROM symbol_calls sc
        JOIN symbols s ON s.id = sc.symbol_id
        WHERE s.repo_id = ?
        """,
        (repo_id,),
    ).fetchall()
    seen: set[tuple[int, str, str]] = set()
    for call in calls:
        text = call["call_text"]
        function_name = call["function_name"] or text.rsplit(".", 1)[-1]
        for flag in _flags_for(function_name, call["receiver"]):
            key = (call["symbol_id"], flag, text)
            if key in seen:
                continue
            seen.add(key)
            conn.execute(
                """
                INSERT INTO heuristic_flags(target_type, target_id, flag, evidence_type, evidence_value, line_start, line_end, rule_id)
                VALUES ('symbol', ?, ?, 'call', ?, ?, ?, ?)
                """,
                (call["symbol_id"], flag, text, call["line_start"], call["line_end"], flag),
            )
    guarded = conn.execute(
        """
        SELECT DISTINCT s.id, s.line_start, s.line_end
        FROM symbols s
        JOIN heuristic_flags h ON h.target_type = 'symbol' AND h.target_id = s.id AND h.flag IN ('destructive_clear', 'destructive_delete')
        WHERE s.repo_id = ? AND (s.signature LIKE '%yes%' OR s.signature LIKE '%confirm%' OR s.primary_text LIKE '%--yes%' OR s.primary_text LIKE '%confirmed%')
        """,
        (repo_id,),
    ).fetchall()
    for row in guarded:
        conn.execute(
            """
            INSERT INTO heuristic_flags(target_type, target_id, flag, evidence_type, evidence_value, line_start, line_end, rule_id)
            VALUES ('symbol', ?, 'confirmation_gate', 'signature_or_text', 'confirmed/yes guard near destructive call', ?, ?, 'confirmation_gate')
            """,
            (row["id"], row["line_start"], row["line_end"]),
        )
