from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from codesearch.config import settings
from codesearch.indexing.discovery import IGNORED_PARTS

# per-root strategy: ("git", allowed_set) | ("spec", _GitignoreSpec) | ("none", None)
_cache: dict[str, tuple[str, object]] = {}


@dataclass(frozen=True)
class _Rule:
    base: str  # dir the pattern is anchored under ("" = root)
    regex: re.Pattern
    negation: bool
    dir_only: bool


class _GitignoreSpec:
    def __init__(self, rules: list[_Rule]) -> None:
        self.rules = rules

    def match(self, rel_path: str) -> bool:
        parts = rel_path.split("/")
        ignored = False
        for i in range(1, len(parts) + 1):
            candidate = "/".join(parts[:i])
            is_dir = i < len(parts)  # ancestors are directories; the final part is the file
            for rule in self.rules:
                if rule.dir_only and not is_dir:
                    continue
                target = _relative_to_base(candidate, rule.base)
                if target is None:
                    continue
                if rule.regex.match(target):
                    ignored = not rule.negation
        return ignored


def _relative_to_base(candidate: str, base: str) -> str | None:
    if not base:
        return candidate
    if candidate == base:
        return ""
    if candidate.startswith(base + "/"):
        return candidate[len(base) + 1 :]
    return None


def _translate(pattern: str) -> str:
    # gitignore glob body -> regex for one path candidate
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                if i + 2 < n and pattern[i + 2] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "/":
            out.append("/")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def _compile_rule(base: str, raw: str) -> _Rule | None:
    line = raw.rstrip("\n")
    if not line.strip() or line.lstrip().startswith("#"):
        return None
    line = line.rstrip()
    negation = line.startswith("!")
    if negation:
        line = line[1:]
    dir_only = line.endswith("/")
    if dir_only:
        line = line[:-1]
    anchored = line.startswith("/") or "/" in line
    body = _translate(line.lstrip("/"))
    prefix = "^" if anchored else "^(?:.*/)?"
    return _Rule(base, re.compile(prefix + body + "$"), negation, dir_only)


def _load_spec(root: Path) -> _GitignoreSpec | None:
    rules: list[_Rule] = []
    found = False
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in IGNORED_PARTS]
        if ".gitignore" not in files:
            continue
        found = True
        base = Path(current).relative_to(root).as_posix()
        base = "" if base == "." else base
        try:
            text = (Path(current) / ".gitignore").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            rule = _compile_rule(base, raw)
            if rule is not None:
                rules.append(rule)
    return _GitignoreSpec(rules) if found else None


def _git_allowed_set(root: Path) -> frozenset[str] | None:
    try:
        check = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
        )
        if check.returncode != 0 or check.stdout.strip() != "true":
            return None
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            check=True,
        )
    except Exception:
        return None
    return frozenset(chunk for chunk in result.stdout.decode("utf-8", errors="replace").split("\0") if chunk)


def _resolve(root: Path) -> tuple[str, object]:
    allowed = _git_allowed_set(root)
    if allowed is not None:
        return ("git", allowed)
    spec = _load_spec(root)
    if spec is not None:
        return ("spec", spec)
    return ("none", None)


def refresh(root: Path) -> None:
    """Resolve and cache the ignore strategy for root. Call once per index run."""
    _cache[str(root)] = _resolve(root)


def status(root: Path) -> dict:
    """Report the active ignore strategy for surfacing to the operator."""
    key = str(root)
    if key not in _cache:
        _cache[key] = _resolve(root)
    mode, payload = _cache[key]
    if mode == "git":
        return {"mode": "git", "tracked_or_unignored": len(payload)}  # type: ignore[arg-type]
    if mode == "spec":
        return {"mode": "gitignore_fallback", "rules": len(payload.rules)}  # type: ignore[attr-defined]
    return {"mode": "none", "note": "no git work tree and no .gitignore; nothing ignored"}


def is_gitignored(root: Path, rel_path: str) -> bool:
    """True if rel_path is excluded by git/.gitignore. False when disabled or unknown."""
    if not settings.respect_gitignore:
        return False
    key = str(root)
    if key not in _cache:
        _cache[key] = _resolve(root)
    mode, payload = _cache[key]
    if mode == "git":
        return rel_path not in payload  # type: ignore[operator]
    if mode == "spec":
        return payload.match(rel_path)  # type: ignore[attr-defined]
    return False
