from __future__ import annotations

import sqlite3

RULES = [
    ("qdrant_delete_collection", "destructive_call", "suffix", ".delete_collection"),
    ("delete_collection_function", "destructive_call", "function", "delete_collection"),
    ("qdrant_upsert", "persistent_write", "suffix", ".upsert"),
    ("db_write", "persistent_write", "suffix", ".write"),
    ("requests_get", "network_call", "exact", "requests.get"),
    ("requests_post", "network_call", "exact", "requests.post"),
    ("httpx_get", "network_call", "exact", "httpx.get"),
    ("httpx_post", "network_call", "exact", "httpx.post"),
    ("path_read_text", "filesystem_read", "suffix", ".read_text"),
    ("open_read", "filesystem_read", "exact", "open"),
    ("path_write_text", "filesystem_write", "suffix", ".write_text"),
    ("queue_job", "background_job", "function", "queue_job"),
    ("embed_call", "embedding_call", "function_prefix", "embed"),
    ("generate_call", "generation_call", "function_prefix", "generate"),
    ("qdrant_search", "vector_search", "suffixes", (".search", ".search_points", ".query_points")),
]


def apply_heuristics(conn: sqlite3.Connection, repo_id: int) -> None:
    conn.execute(
        "DELETE FROM heuristic_flags WHERE target_type = 'symbol' AND target_id IN (SELECT id FROM symbols WHERE repo_id = ?)",
        (repo_id,),
    )
    calls = conn.execute(
        """
        SELECT sc.symbol_id, sc.call_text, sc.function_name, sc.line_start, sc.line_end
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
        for rule_id, flag, match_type, needle in RULES:
            if _matches(text, function_name, match_type, needle):
                key = (call["symbol_id"], flag, text)
                if key in seen:
                    continue
                seen.add(key)
                conn.execute(
                    """
                    INSERT INTO heuristic_flags(target_type, target_id, flag, evidence_type, evidence_value, line_start, line_end, rule_id)
                    VALUES ('symbol', ?, ?, 'call', ?, ?, ?, ?)
                    """,
                    (call["symbol_id"], flag, text, call["line_start"], call["line_end"], rule_id),
                )
    guarded = conn.execute(
        """
        SELECT DISTINCT s.id, s.line_start, s.line_end
        FROM symbols s
        JOIN heuristic_flags h ON h.target_type = 'symbol' AND h.target_id = s.id AND h.flag = 'destructive_call'
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


def _matches(call_text: str, function_name: str, match_type: str, needle) -> bool:
    text = call_text.lower()
    function = function_name.lower()
    if match_type == "exact":
        return text == str(needle).lower()
    if match_type == "suffix":
        return text.endswith(str(needle).lower())
    if match_type == "suffixes":
        return any(text.endswith(str(item).lower()) for item in needle)
    if match_type == "function":
        return function == str(needle).lower()
    if match_type == "function_prefix":
        return function.startswith(str(needle).lower())
    return False
