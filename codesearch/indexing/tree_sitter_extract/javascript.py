from __future__ import annotations

from codesearch.indexing.tree_sitter_extract.common import (
    child_by_field_name,
    make_symbol,
    named_children,
    node_lines,
    node_text,
    path_module_ref,
    query_matches,
    run_query,
)
from codesearch.indexing.tree_sitter_extract.fallback import strip_quotes
from codesearch.indexing.tree_sitter_extract.models import ExtractedFile, ExtractedSymbol, ParsedTree

LANGUAGE = "javascript"
QUERY = """
(function_declaration name: (identifier) @symbol.name) @symbol.function
(class_declaration name: (identifier) @symbol.name) @symbol.class
(method_definition name: [(property_identifier) (identifier)] @symbol.name) @symbol.method
(variable_declarator
  name: (identifier) @symbol.name
  value: [(arrow_function) (function_expression)] @symbol.value) @symbol.function
(import_statement) @import
(call_expression function: (_) @call.name) @call
(string) @literal.string
(template_string) @literal.string
(comment) @comment
"""


def extract(text: str, rel_path: str, parsed: ParsedTree) -> ExtractedFile:
    return extract_javascript_like(text, rel_path, parsed, LANGUAGE)


def extract_javascript_like(text: str, rel_path: str, parsed: ParsedTree, language: str) -> ExtractedFile:
    facts: list[tuple[str, str, int | None, int | None]] = []
    symbols: list[ExtractedSymbol] = []
    root = parsed.root_node
    if root is None:
        return ExtractedFile(language, parsed.parser_name, parsed.parser_available, parsed.parse_error_count, facts, symbols)

    for capture in run_query(language, QUERY, root):
        start, end = node_lines(capture.node)
        if capture.name == "import":
            facts.append(("import", node_text(parsed.source_bytes, capture.node).strip().rstrip(";"), start, end))
        elif capture.name == "literal.string":
            facts.append(("string_literal", strip_quotes(node_text(parsed.source_bytes, capture.node))[:300], start, end))
        elif capture.name == "comment":
            facts.append(("comment", node_text(parsed.source_bytes, capture.node).strip()[:500], start, end))

    seen: set[int] = set()
    for _pattern, captures in query_matches(language, QUERY, root):
        symbol_node = _first_capture(captures, "symbol.function", "symbol.class", "symbol.method")
        name_node = _first_capture(captures, "symbol.name")
        if symbol_node is None or name_node is None or id(symbol_node) in seen:
            continue
        seen.add(id(symbol_node))
        symbol_type = "class" if captures.get("symbol.class") else "function"
        symbols.append(_build_symbol(text, rel_path, parsed, language, symbol_node, name_node, symbol_type))
    return ExtractedFile(language, parsed.parser_name, parsed.parser_available, parsed.parse_error_count, facts, symbols)


def _build_symbol(text: str, rel_path: str, parsed: ParsedTree, language: str, node, name_node, symbol_type: str) -> ExtractedSymbol:
    name = node_text(parsed.source_bytes, name_node).strip()
    qualified = _qualified_name(parsed.source_bytes, node, name)
    module = path_module_ref(rel_path)
    signature = node_text(parsed.source_bytes, node).splitlines()[0].strip()[:240]
    calls = _calls(parsed.source_bytes, node, language)
    literals = _literals(parsed.source_bytes, node, language)
    return make_symbol(rel_path, f"{module}.{qualified}", name, qualified, symbol_type, signature, "", "", node, text, calls, literals)


def _qualified_name(source_bytes: bytes, node, name: str) -> str:
    names = [name]
    parent = getattr(node, "parent", None)
    while parent is not None:
        if getattr(parent, "type", "") == "class_declaration":
            name_node = child_by_field_name(parent, "name")
            if name_node is not None:
                names.append(node_text(source_bytes, name_node).strip())
        parent = getattr(parent, "parent", None)
    return ".".join(reversed(names))


def _calls(source_bytes: bytes, root, language: str) -> list[tuple[str, str | None, str, int, int]]:
    calls = []
    for _pattern, captures in query_matches(language, "(call_expression function: (_) @call.name) @call", root):
        call_node = _first_capture(captures, "call")
        name_node = _first_capture(captures, "call.name")
        if call_node is None or name_node is None:
            continue
        call_text = node_text(source_bytes, name_node).strip()
        receiver, _, function_name = call_text.rpartition(".")
        start, end = node_lines(call_node)
        calls.append((call_text, receiver or None, function_name, start, end))
    return calls


def _literals(source_bytes: bytes, root, language: str) -> list[tuple[str, str, int, int]]:
    literals = []
    for capture in run_query(language, "(string) @literal.string (template_string) @literal.string (number) @literal.number", root):
        start, end = node_lines(capture.node)
        literals.append((capture.name.rsplit(".", 1)[-1], strip_quotes(node_text(source_bytes, capture.node))[:300], start, end))
    return literals


def _first_capture(captures: dict, *names: str):
    for name in names:
        nodes = captures.get(name)
        if nodes:
            return nodes[0]
    return None

