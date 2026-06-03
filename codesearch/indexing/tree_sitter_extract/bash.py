from __future__ import annotations

from codesearch.indexing.tree_sitter_extract.common import make_symbol, node_lines, node_text, path_module_ref, query_matches, run_query
from codesearch.indexing.tree_sitter_extract.fallback import strip_quotes
from codesearch.indexing.tree_sitter_extract.models import ExtractedFile, ParsedTree

LANGUAGE = "bash"
QUERY = """
(function_definition name: (word) @symbol.name) @symbol.function
(command name: (_) @call.name) @call
(comment) @comment
(raw_string) @literal.string
(string) @literal.string
"""


def extract(text: str, rel_path: str, parsed: ParsedTree) -> ExtractedFile:
    facts = []
    symbols = []
    for capture in run_query(LANGUAGE, QUERY, parsed.root_node):
        start, end = node_lines(capture.node)
        if capture.name == "comment":
            facts.append(("comment", node_text(parsed.source_bytes, capture.node).strip()[:500], start, end))
        elif capture.name == "literal.string":
            facts.append(("string_literal", strip_quotes(node_text(parsed.source_bytes, capture.node))[:300], start, end))
    for _pattern, captures in query_matches(LANGUAGE, QUERY, parsed.root_node):
        node = _first(captures, "symbol.function")
        name_node = _first(captures, "symbol.name")
        if node is None or name_node is None:
            continue
        name = node_text(parsed.source_bytes, name_node).strip()
        calls = _calls(parsed.source_bytes, node)
        literals = []
        module = path_module_ref(rel_path)
        symbols.append(make_symbol(rel_path, f"{module}.{name}", name, name, "function", f"{name}()", "", "", node, text, calls, literals))
    return ExtractedFile(LANGUAGE, parsed.parser_name, parsed.parser_available, parsed.parse_error_count, facts, symbols)


def _calls(source_bytes, root):
    calls = []
    for _pattern, captures in query_matches(LANGUAGE, "(command name: (_) @call.name) @call", root):
        call_node = _first(captures, "call")
        name_node = _first(captures, "call.name")
        if call_node is None or name_node is None:
            continue
        call_text = node_text(source_bytes, name_node).strip()
        start, end = node_lines(call_node)
        calls.append((call_text, None, call_text, start, end))
    return calls


def _first(captures, *names):
    for name in names:
        if captures.get(name):
            return captures[name][0]
    return None

