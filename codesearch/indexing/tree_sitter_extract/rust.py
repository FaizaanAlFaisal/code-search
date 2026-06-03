from __future__ import annotations

from codesearch.indexing.tree_sitter_extract.common import make_symbol, node_lines, node_text, path_module_ref, query_matches, run_query
from codesearch.indexing.tree_sitter_extract.fallback import strip_quotes
from codesearch.indexing.tree_sitter_extract.models import ExtractedFile, ExtractedSymbol, ParsedTree

LANGUAGE = "rust"
QUERY = """
(function_item name: (identifier) @symbol.name) @symbol.function
(struct_item name: (type_identifier) @symbol.name) @symbol.class
(impl_item) @symbol.impl
(macro_invocation macro: (identifier) @call.name) @call
(call_expression function: (_) @call.name) @call
(scoped_identifier) @call.name
(line_comment) @comment
(block_comment) @comment
(string_literal) @literal.string
(raw_string_literal) @literal.string
"""


def extract(text: str, rel_path: str, parsed: ParsedTree) -> ExtractedFile:
    facts = _facts(parsed)
    symbols: list[ExtractedSymbol] = []
    for _pattern, captures in query_matches(LANGUAGE, QUERY, parsed.root_node):
        node = _first(captures, "symbol.function", "symbol.class")
        name_node = _first(captures, "symbol.name")
        if node is None or name_node is None:
            continue
        name = node_text(parsed.source_bytes, name_node)
        kind = "class" if captures.get("symbol.class") else "function"
        symbols.append(_symbol(text, rel_path, parsed, node, name, kind))
    return ExtractedFile(LANGUAGE, parsed.parser_name, parsed.parser_available, parsed.parse_error_count, facts, symbols)


def _symbol(text: str, rel_path: str, parsed: ParsedTree, node, name: str, kind: str) -> ExtractedSymbol:
    module = path_module_ref(rel_path)
    signature = node_text(parsed.source_bytes, node).splitlines()[0].strip()[:240]
    calls = _calls(parsed.source_bytes, node)
    literals = _literals(parsed.source_bytes, node)
    return make_symbol(rel_path, f"{module}::{name}", name, name, kind, signature, "", "", node, text, calls, literals)


def _facts(parsed: ParsedTree):
    facts = []
    for capture in run_query(LANGUAGE, QUERY, parsed.root_node):
        start, end = node_lines(capture.node)
        if capture.name == "comment":
            facts.append(("comment", node_text(parsed.source_bytes, capture.node).strip()[:500], start, end))
        elif capture.name == "literal.string":
            facts.append(("string_literal", strip_quotes(node_text(parsed.source_bytes, capture.node))[:300], start, end))
    return facts


def _calls(source_bytes: bytes, root):
    calls = []
    for _pattern, captures in query_matches(LANGUAGE, "(call_expression function: (_) @call.name) @call (macro_invocation macro: (identifier) @call.name) @call", root):
        call_node = _first(captures, "call")
        name_node = _first(captures, "call.name")
        if call_node is None or name_node is None:
            continue
        call_text = node_text(source_bytes, name_node).strip()
        receiver, _, function_name = call_text.rpartition("::")
        if not receiver:
            receiver, _, function_name = call_text.rpartition(".")
        start, end = node_lines(call_node)
        calls.append((call_text, receiver or None, function_name, start, end))
    return calls


def _literals(source_bytes: bytes, root):
    literals = []
    for capture in run_query(LANGUAGE, "(string_literal) @literal.string (raw_string_literal) @literal.string (integer_literal) @literal.number (float_literal) @literal.number", root):
        start, end = node_lines(capture.node)
        literals.append((capture.name.rsplit(".", 1)[-1], strip_quotes(node_text(source_bytes, capture.node))[:300], start, end))
    return literals


def _first(captures, *names):
    for name in names:
        if captures.get(name):
            return captures[name][0]
    return None

