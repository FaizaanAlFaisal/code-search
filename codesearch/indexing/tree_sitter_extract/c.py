from __future__ import annotations

from codesearch.indexing.tree_sitter_extract.common import make_symbol, node_lines, node_text, path_module_ref, query_matches, run_query
from codesearch.indexing.tree_sitter_extract.fallback import strip_quotes
from codesearch.indexing.tree_sitter_extract.models import ExtractedFile, ParsedTree

LANGUAGE = "c"
QUERY = """
(function_definition declarator: (function_declarator declarator: (identifier) @symbol.name)) @symbol.function
(call_expression function: (_) @call.name) @call
(preproc_include) @import
(comment) @comment
(string_literal) @literal.string
"""


def extract(text: str, rel_path: str, parsed: ParsedTree) -> ExtractedFile:
    return _extract_c_like(text, rel_path, parsed, LANGUAGE)


def _extract_c_like(text: str, rel_path: str, parsed: ParsedTree, language: str) -> ExtractedFile:
    facts = []
    symbols = []
    for capture in run_query(language, QUERY, parsed.root_node):
        start, end = node_lines(capture.node)
        if capture.name == "import":
            facts.append(("import", node_text(parsed.source_bytes, capture.node).strip(), start, end))
        elif capture.name == "comment":
            facts.append(("comment", node_text(parsed.source_bytes, capture.node).strip()[:500], start, end))
        elif capture.name == "literal.string":
            facts.append(("string_literal", strip_quotes(node_text(parsed.source_bytes, capture.node))[:300], start, end))
    for _pattern, captures in query_matches(language, QUERY, parsed.root_node):
        node = _first(captures, "symbol.function")
        name_node = _first(captures, "symbol.name")
        if node is None or name_node is None:
            continue
        name = node_text(parsed.source_bytes, name_node).strip()
        signature = node_text(parsed.source_bytes, node).split("{", 1)[0].strip()[:240]
        calls = _calls(parsed.source_bytes, node, language)
        literals = _literals(parsed.source_bytes, node, language)
        module = path_module_ref(rel_path)
        symbols.append(make_symbol(rel_path, f"{module}::{name}", name, name, "function", signature, "", "", node, text, calls, literals))
    return ExtractedFile(language, parsed.parser_name, parsed.parser_available, parsed.parse_error_count, facts, symbols)


def _calls(source_bytes: bytes, root, language: str):
    calls = []
    for _pattern, captures in query_matches(language, "(call_expression function: (_) @call.name) @call", root):
        call_node = _first(captures, "call")
        name_node = _first(captures, "call.name")
        if call_node is None or name_node is None:
            continue
        call_text = node_text(source_bytes, name_node).strip()
        start, end = node_lines(call_node)
        calls.append((call_text, None, call_text.rsplit("::", 1)[-1].rsplit(".", 1)[-1], start, end))
    return calls


def _literals(source_bytes: bytes, root, language: str):
    literals = []
    for capture in run_query(language, "(string_literal) @literal.string (number_literal) @literal.number", root):
        start, end = node_lines(capture.node)
        literals.append((capture.name.rsplit(".", 1)[-1], strip_quotes(node_text(source_bytes, capture.node))[:300], start, end))
    return literals


def _first(captures, *names):
    for name in names:
        if captures.get(name):
            return captures[name][0]
    return None

