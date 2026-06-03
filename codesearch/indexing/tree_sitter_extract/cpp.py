from __future__ import annotations

from codesearch.indexing.tree_sitter_extract.c import _extract_c_like
from codesearch.indexing.tree_sitter_extract.models import ExtractedFile, ParsedTree


def extract(text: str, rel_path: str, parsed: ParsedTree) -> ExtractedFile:
    return _extract_c_like(text, rel_path, parsed, "cpp")

