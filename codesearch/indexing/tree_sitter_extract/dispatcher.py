from __future__ import annotations

from pathlib import Path

from codesearch.indexing.languages import detect_language
from codesearch.indexing.tree_sitter_extract import (
    c,
    cpp,
    fallback,
    javascript,
    python,
    rust,
    typescript,
)
from codesearch.indexing.tree_sitter_extract.common import parse_tree
from codesearch.indexing.tree_sitter_extract.models import ExtractedFile

LANGUAGE_EXTRACTORS = {
    "python": python.extract,
    "javascript": javascript.extract,
    "typescript": typescript.extract,
    "tsx": typescript.extract,
    "rust": rust.extract,
    "c": c.extract,
    "cpp": cpp.extract,
}


def extract_file(repo_root: Path, rel_path: str, text: str) -> ExtractedFile:
    language = detect_language(rel_path, text)
    if not language:
        return ExtractedFile(None, None, False, 0, fallback.regex_facts(text), [])
    parsed = parse_tree(language, text)
    extractor = LANGUAGE_EXTRACTORS.get(language)
    if extractor:
        return extractor(text, rel_path, parsed)
    return fallback.extract_file_facts(parsed, rel_path)
