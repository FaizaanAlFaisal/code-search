from __future__ import annotations

import re

from codesearch.indexing.tree_sitter_extract.common import node_lines, node_text, walk
from codesearch.indexing.tree_sitter_extract.models import ExtractedFile, ParsedTree


def extract_file_facts(parsed: ParsedTree, rel_path: str) -> ExtractedFile:
    facts: list[tuple[str, str, int | None, int | None]] = []
    root = parsed.root_node
    if root is not None:
        for node in walk(root):
            kind = getattr(node, "type", "")
            if kind in {"comment", "line_comment", "block_comment"}:
                start, end = node_lines(node)
                facts.append(("comment", node_text(parsed.source_bytes, node).strip()[:500], start, end))
            elif kind in {"string", "string_literal", "raw_string_literal", "interpreted_string_literal"}:
                start, end = node_lines(node)
                facts.append(("string_literal", strip_quotes(node_text(parsed.source_bytes, node))[:300], start, end))
    if not facts:
        facts.extend(regex_facts(parsed.text))
    return ExtractedFile(parsed.language, parsed.parser_name, parsed.parser_available, parsed.parse_error_count, facts, [])


def regex_facts(text: str) -> list[tuple[str, str, int | None, int | None]]:
    facts: list[tuple[str, str, int | None, int | None]] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            facts.append(("heading" if stripped.startswith("# ") else "comment", stripped[:300], idx, idx))
        for literal in re.findall(r"['\"]([^'\"]{2,300})['\"]", line):
            facts.append(("string_literal", literal, idx, idx))
        if ":" in stripped and re.match(r"^[A-Za-z0-9_.-]+:", stripped):
            facts.append(("config_key", stripped.split(":", 1)[0], idx, idx))
    return facts


def strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] in {"'", '"', "`"} and value[-1] == value[0]:
        return value[1:-1]
    return value

