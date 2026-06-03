from __future__ import annotations

from pathlib import Path

EXTENSIONS = {
    ".bash": "bash",
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cs": "c-sharp",
    ".css": "css",
    ".go": "go",
    ".h": "c",
    ".hpp": "cpp",
    ".html": "html",
    ".java": "java",
    ".json": "json",
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".lua": "lua",
    ".mk": "make",
    ".md": "markdown",
    ".rs": "rust",
    ".regex": "regex",
    ".sql": "sql",
    ".sh": "bash",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
}


def detect_language(path: str, text: str = "") -> str | None:
    suffix = Path(path).suffix.lower()
    if suffix in EXTENSIONS:
        return EXTENSIONS[suffix]
    if Path(path).name == "Dockerfile":
        return "dockerfile"
    if Path(path).name in {"Makefile", "makefile"}:
        return "make"
    first = text.splitlines()[0] if text else ""
    if "python" in first and first.startswith("#!"):
        return "python"
    if first.startswith("#!") and ("bash" in first or "sh" in first):
        return "bash"
    return None
