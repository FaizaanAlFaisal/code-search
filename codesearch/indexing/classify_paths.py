from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from codesearch.config import settings
from codesearch.indexing.discovery import IGNORED_PARTS
from codesearch.indexing.gitignore import is_gitignored
from codesearch.indexing.languages import detect_language

SAMPLE_BYTES = 8192

SOURCE_EXTENSIONS = {
    ".bash",
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".java",
    ".js",
    ".jsx",
    ".lua",
    ".mjs",
    ".cjs",
    ".py",
    ".rs",
    ".sh",
    ".sql",
    ".ts",
    ".tsx",
}
CONFIG_EXTENSIONS = {".cfg", ".conf", ".env", ".ini", ".json", ".lock", ".mk", ".toml", ".yaml", ".yml"}
DOC_EXTENSIONS = {".md", ".mdx", ".rst", ".txt"}
ARCHIVE_EXTENSIONS = {".7z", ".bz2", ".gz", ".rar", ".tar", ".tgz", ".xz", ".zip", ".zst"}
MEDIA_EXTENSIONS = {
    ".apng",
    ".avif",
    ".bmp",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".mov",
    ".mp3",
    ".mp4",
    ".ogg",
    ".png",
    ".wav",
    ".webm",
    ".webp",
}
FONT_EXTENSIONS = {".eot", ".otf", ".ttf", ".woff", ".woff2"}
MODEL_EXTENSIONS = {".bin", ".gguf", ".h5", ".onnx", ".pt", ".pth", ".safetensors"}
DATABASE_EXTENSIONS = {".db", ".sqlite", ".sqlite3"}
DUMP_EXTENSIONS = {".bak", ".dump", ".dmp", ".har", ".pcap"}
CACHE_EXTENSIONS = {".coverage", ".pyc", ".pyo", ".swp", ".tmp"}
USEFUL_NAMES = {
    "Dockerfile",
    "Containerfile",
    "Makefile",
    "makefile",
    "README",
    "LICENSE",
    "CHANGELOG",
    "NOTICE",
    "requirements.txt",
    "Pipfile",
    "pyproject.toml",
    "package.json",
    "tsconfig.json",
}
GENERATED_PARTS = {"dist", "coverage", ".coverage", ".cache", "cache", "target", "build"}
GENERATED_SUFFIXES = (".generated.", ".gen.", ".min.")
LOG_NAMES = {"nohup.out"}
ENV_NAMES = {".env", ".env.local", ".envrc"}


@dataclass(frozen=True)
class Classification:
    decision: str
    reason: str
    detected_kind: str
    manual_rule_id: int | None = None
    size_bytes: int | None = None


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_rel_path(path: str) -> str:
    raw = path.replace("\\", "/").strip("/")
    parts = [part for part in PurePosixPath(raw).parts if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise ValueError(f"Path must stay within the repo: {path}")
    return "/".join(parts)


def classify_path(conn: sqlite3.Connection, repo_id: int, root: Path, rel_path: str) -> Classification:
    rel_path = normalize_rel_path(rel_path)
    manual = _matching_manual_rule(conn, repo_id, rel_path)
    path = root / rel_path
    if manual and manual["action"] == "include":
        size = _size(path)
        return Classification("include", f"manual_{manual['path_kind']}_include", _kind_from_path(path), int(manual["id"]), size)
    if is_gitignored(root, rel_path):
        return Classification("exclude", "gitignored", _kind_from_path(path), size_bytes=_size(path))
    auto = _auto_classify(path, rel_path)
    if manual and manual["action"] == "exclude":
        return Classification("exclude", f"manual_{manual['path_kind']}_exclude", auto.detected_kind, int(manual["id"]), auto.size_bytes)
    return auto


def audit_path(conn: sqlite3.Connection, repo_id: int, rel_path: str, classification: Classification, decision: str | None = None) -> None:
    conn.execute(
        """
        INSERT INTO path_audit(repo_id, path, decision, reason, rule_id, size_bytes, detected_kind, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo_id, path) DO UPDATE SET
          decision = excluded.decision,
          reason = excluded.reason,
          rule_id = excluded.rule_id,
          size_bytes = excluded.size_bytes,
          detected_kind = excluded.detected_kind,
          updated_at = excluded.updated_at
        """,
        (
            repo_id,
            normalize_rel_path(rel_path),
            decision or ("included" if classification.decision == "include" else "excluded"),
            classification.reason,
            classification.manual_rule_id,
            classification.size_bytes,
            classification.detected_kind,
            now(),
        ),
    )


def add_rule(conn: sqlite3.Connection, repo_id: int, path: str, path_kind: str, action: str, reason: str | None) -> int:
    rel_path = normalize_rel_path(path)
    if path_kind == "dir":
        rel_path = rel_path.rstrip("/")
    cur = conn.execute(
        """
        INSERT INTO indexing_rules(repo_id, path, path_kind, action, reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo_id, path, path_kind, action) DO UPDATE SET reason = excluded.reason
        """,
        (repo_id, rel_path, path_kind, action, reason, now()),
    )
    row = conn.execute(
        "SELECT id FROM indexing_rules WHERE repo_id = ? AND path = ? AND path_kind = ? AND action = ?",
        (repo_id, rel_path, path_kind, action),
    ).fetchone()
    conn.commit()
    return int(row["id"] if row else cur.lastrowid)


def remove_rule(conn: sqlite3.Connection, repo_id: int, path: str, path_kind: str, action: str) -> int:
    rel_path = normalize_rel_path(path)
    if path_kind == "dir":
        rel_path = rel_path.rstrip("/")
    cur = conn.execute(
        "DELETE FROM indexing_rules WHERE repo_id = ? AND path = ? AND path_kind = ? AND action = ?",
        (repo_id, rel_path, path_kind, action),
    )
    conn.commit()
    return int(cur.rowcount)


def matching_paths_for_rule(root: Path, path: str, path_kind: str) -> list[str]:
    rel_path = normalize_rel_path(path)
    target = root / rel_path
    if path_kind == "file":
        return [rel_path]
    if not target.exists():
        return [rel_path.rstrip("/")]
    return sorted(str(child.relative_to(root)).replace("\\", "/") for child in target.rglob("*") if child.is_file())


def _matching_manual_rule(conn: sqlite3.Connection, repo_id: int, rel_path: str) -> sqlite3.Row | None:
    rules = conn.execute(
        "SELECT * FROM indexing_rules WHERE repo_id = ? ORDER BY length(path) DESC, id DESC",
        (repo_id,),
    ).fetchall()
    best: tuple[int, sqlite3.Row] | None = None
    for rule in rules:
        priority = _rule_priority(rule, rel_path)
        if priority is None:
            continue
        if best is None or priority > best[0]:
            best = (priority, rule)
    return best[1] if best else None


def _rule_priority(rule: sqlite3.Row, rel_path: str) -> int | None:
    path = rule["path"].rstrip("/")
    if rule["path_kind"] == "file":
        if rel_path != path:
            return None
        return 4 if rule["action"] == "include" else 3
    if rel_path != path and not rel_path.startswith(f"{path}/"):
        return None
    return 2 if rule["action"] == "include" else 1


def _auto_classify(path: Path, rel_path: str) -> Classification:
    parts = PurePosixPath(rel_path).parts
    if any(part in IGNORED_PARTS for part in parts):
        return Classification("exclude", "structural_ignored_part", "cache", size_bytes=_size(path))
    if not path.exists():
        return Classification("exclude", "missing", "unknown")
    if not path.is_file():
        return Classification("exclude", "not_regular_file", "unknown")
    size = path.stat().st_size
    kind = _kind_from_path(path)
    if size > settings.max_file_bytes:
        return Classification("exclude", "too_large", kind, size_bytes=size)
    if path.name in ENV_NAMES or path.name.lower().startswith(".env."):
        return Classification("exclude", "env_file", "config", size_bytes=size)
    if kind in {"archive", "media", "font", "model", "database", "dump", "log", "cache", "generated"}:
        return Classification("exclude", f"{kind}_file", kind, size_bytes=size)
    sample = _sample(path)
    if sample is None:
        return Classification("exclude", "unreadable", "unknown", size_bytes=size)
    if _is_binary(sample):
        return Classification("exclude", "binary_sample", "binary", size_bytes=size)
    name = path.name
    suffix = path.suffix.lower()
    language = detect_language(rel_path, sample.decode("utf-8", errors="replace"))
    if suffix in SOURCE_EXTENSIONS:
        return Classification("include", "known_source_or_language", "source", size_bytes=size)
    if suffix in CONFIG_EXTENSIONS or name in USEFUL_NAMES:
        return Classification("include", "known_config", "config", size_bytes=size)
    if suffix in DOC_EXTENSIONS:
        return Classification("include", "known_docs", "docs", size_bytes=size)
    if language:
        kind = "config" if language in {"dockerfile", "json", "make", "toml", "yaml"} else "source"
        return Classification("include", "known_source_or_language", kind, size_bytes=size)
    if _looks_text(sample) and size <= min(settings.max_file_bytes, 65536):
        return Classification("include", "small_text_fallback", "docs", size_bytes=size)
    return Classification("exclude", "unknown_kind", "unknown", size_bytes=size)


def _kind_from_path(path: Path) -> str:
    name = path.name
    lower_name = name.lower()
    suffix = path.suffix.lower()
    parts = {part.lower() for part in path.parts}
    if lower_name in LOG_NAMES or suffix == ".log" or lower_name.endswith(".log.txt"):
        return "log"
    if name in ENV_NAMES or lower_name.startswith(".env."):
        return "config"
    if suffix in ARCHIVE_EXTENSIONS:
        return "archive"
    if suffix in MEDIA_EXTENSIONS:
        return "media"
    if suffix in FONT_EXTENSIONS:
        return "font"
    if suffix in MODEL_EXTENSIONS:
        return "model"
    if suffix in DATABASE_EXTENSIONS:
        return "database"
    if suffix in DUMP_EXTENSIONS or "dump" in lower_name:
        return "dump"
    if suffix in CACHE_EXTENSIONS:
        return "cache"
    if lower_name.endswith(".map") or lower_name.endswith(".min.js") or lower_name.endswith(".min.css"):
        return "generated"
    if any(part in GENERATED_PARTS for part in parts) or any(token in lower_name for token in GENERATED_SUFFIXES):
        return "generated"
    if suffix in SOURCE_EXTENSIONS:
        return "source"
    if suffix in CONFIG_EXTENSIONS or name in USEFUL_NAMES:
        return "config"
    if suffix in DOC_EXTENSIONS:
        return "docs"
    return "unknown"


def _sample(path: Path) -> bytes | None:
    try:
        with path.open("rb") as handle:
            return handle.read(SAMPLE_BYTES)
    except OSError:
        return None


def _size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _is_binary(sample: bytes) -> bool:
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    control = sum(1 for byte in sample if byte < 9 or (13 < byte < 32))
    return control / max(1, len(sample)) > 0.30


def _looks_text(sample: bytes) -> bool:
    if _is_binary(sample):
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False
