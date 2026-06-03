from __future__ import annotations

from codesearch.indexing.tree_sitter_extract.common import make_symbol, node_lines, node_text, path_module_ref, query_matches, run_query
from codesearch.indexing.tree_sitter_extract.models import ExtractedFile, ParsedTree

LANGUAGE = "css"
QUERY = """
(rule_set
  (selectors) @symbol.name) @symbol.rule
(comment) @comment
(plain_value) @literal.value
"""


def extract(text: str, rel_path: str, parsed: ParsedTree) -> ExtractedFile:
    facts = []
    symbols = []
    for capture in run_query(LANGUAGE, QUERY, parsed.root_node):
        start, end = node_lines(capture.node)
        if capture.name == "comment":
            facts.append(("comment", node_text(parsed.source_bytes, capture.node).strip()[:500], start, end))
        elif capture.name == "literal.value":
            facts.append(("css_value", node_text(parsed.source_bytes, capture.node).strip()[:300], start, end))
    for _pattern, captures in query_matches(LANGUAGE, QUERY, parsed.root_node):
        node = _first(captures, "symbol.rule")
        name_node = _first(captures, "symbol.name")
        if node is None or name_node is None:
            continue
        name = node_text(parsed.source_bytes, name_node).strip().split(",", 1)[0]
        module = path_module_ref(rel_path)
        symbols.append(make_symbol(rel_path, f"{module}.{name.lstrip('.')}", name, name, "selector", name, "", "", node, text, [], []))
    return ExtractedFile(LANGUAGE, parsed.parser_name, parsed.parser_available, parsed.parse_error_count, facts, symbols)


def _first(captures, *names):
    for name in names:
        if captures.get(name):
            return captures[name][0]
    return None

