from __future__ import annotations

import re
import sqlite3

_RUN = re.compile(r"[A-Za-z0-9_-]+")
# split a chunk into camelCase / acronym / number subwords
_CAMEL = re.compile(r"[0-9]+|[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+")


def _split_identifier(run: str) -> list[str]:
    parts: list[str] = []
    for chunk in re.split(r"[_-]+", run):
        parts.extend(_CAMEL.findall(chunk))
    return [p for p in parts if p]


def tokenize_identifiers(text: str | None) -> str:
    # append camelCase/snake/kebab/SCREAMING subword splits so a fragment (login) hits loginAdmin
    if not text:
        return text or ""
    extra: list[str] = []
    for match in _RUN.finditer(text):
        parts = _split_identifier(match.group(0))
        if len(parts) > 1:
            extra.extend(parts)
    return text if not extra else f"{text} {' '.join(extra)}"


def quote_fts_query(query: str) -> str:
    # prefix-match each alnum term; tokenization at index time makes fragments hit
    terms = [t for t in re.split(r"[^A-Za-z0-9]+", query) if t]
    return " OR ".join(f"{t}*" for t in terms) or '""'


def rebuild_repo_fts(conn: sqlite3.Connection, repo_id: int, repo_slug: str) -> None:
    conn.execute("DELETE FROM code_fts WHERE repo_slug = ?", (repo_slug,))
    file_rows = conn.execute(
        """
        SELECT f.id, f.path, f.language,
               group_concat(ff.fact_type || ':' || ff.value, ' ') AS facts,
               group_concat(mo.json_text, ' ') AS secondary
        FROM files f
        LEFT JOIN file_facts ff ON ff.file_id = f.id
        LEFT JOIN model_outputs mo ON mo.target_type = 'file' AND mo.target_id = f.id AND mo.accepted = 1
        WHERE f.repo_id = ? AND f.skipped_reason IS NULL
        GROUP BY f.id
        """,
        (repo_id,),
    ).fetchall()
    for row in file_rows:
        conn.execute(
            "INSERT INTO code_fts(record_type, repo_slug, target_id, path, symbol, module, primary_text, heuristic_text, secondary_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("file", repo_slug, row["id"], row["path"], "", _module_for_path(row["path"]), tokenize_identifiers(f"{row['path']} {row['language'] or ''} {row['facts'] or ''}"), "", row["secondary"] or ""),
        )
    symbol_rows = conn.execute(
        """
        SELECT s.id, s.symbol_ref, s.primary_text, s.name, f.path,
               group_concat(h.flag || ':' || h.evidence_value, ' ') AS flags,
               group_concat(mo.json_text, ' ') AS secondary
        FROM symbols s
        JOIN files f ON f.id = s.file_id
        LEFT JOIN heuristic_flags h ON h.target_type = 'symbol' AND h.target_id = s.id
        LEFT JOIN model_outputs mo ON mo.target_type = 'symbol' AND mo.target_id = s.id AND mo.accepted = 1
        WHERE s.repo_id = ?
        GROUP BY s.id
        """,
        (repo_id,),
    ).fetchall()
    for row in symbol_rows:
        conn.execute(
            "INSERT INTO code_fts(record_type, repo_slug, target_id, path, symbol, module, primary_text, heuristic_text, secondary_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("symbol", repo_slug, row["id"], row["path"], row["symbol_ref"], _module_for_path(row["path"]), tokenize_identifiers(f"{row['primary_text']} {row['symbol_ref'] or ''}"), row["flags"] or "", row["secondary"] or ""),
        )
    module_rows = conn.execute(
        """
        SELECT m.id, m.name, m.path_prefix, group_concat(f.path, ' ') AS paths, group_concat(s.name, ' ') AS symbols,
               group_concat(mo.json_text, ' ') AS secondary
        FROM modules m
        LEFT JOIN module_files mf ON mf.module_id = m.id
        LEFT JOIN files f ON f.id = mf.file_id
        LEFT JOIN symbols s ON s.file_id = f.id
        LEFT JOIN model_outputs mo ON mo.target_type = 'module' AND mo.target_id = m.id AND mo.accepted = 1
        WHERE m.repo_id = ?
        GROUP BY m.id
        """,
        (repo_id,),
    ).fetchall()
    for row in module_rows:
        conn.execute(
            "INSERT INTO code_fts(record_type, repo_slug, target_id, path, symbol, module, primary_text, heuristic_text, secondary_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("module", repo_slug, row["id"], row["path_prefix"], "", row["name"], tokenize_identifiers(f"{row['name']} {row['path_prefix']} {row['paths'] or ''} {row['symbols'] or ''}"), "", row["secondary"] or ""),
        )


def _module_for_path(path: str) -> str:
    parts = path.split("/")
    return ".".join(parts[:-1]) if len(parts) > 1 else ""
