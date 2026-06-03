from __future__ import annotations

import os
from pathlib import Path

IGNORED_PARTS = {".git", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache", "node_modules", ".code-search"}


def discover_files(root: Path) -> list[str]:
    paths: list[str] = []
    for current, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in IGNORED_PARTS]
        current_path = Path(current)
        for name in files:
            path = current_path / name
            try:
                if not path.is_file():
                    continue
                rel_path = path.relative_to(root).as_posix()
            except OSError:
                continue
            if any(part in IGNORED_PARTS for part in Path(rel_path).parts):
                continue
            paths.append(rel_path)
    return sorted(paths)
