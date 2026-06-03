from __future__ import annotations

from codesearch.indexing.tree_sitter_extract.common import make_symbol, node_lines, node_text, path_module_ref, query_matches, run_query
from codesearch.indexing.tree_sitter_extract.fallback import strip_quotes
from codesearch.indexing.tree_sitter_extract.models import ExtractedFile, ParsedTree

QUERY = """
(function_declaration name: (identifier) @symbol.name) @symbol.function
(class_declaration name: (type_identifier) @symbol.name) @symbol.class
(method_definition name: [(property_identifier) (identifier)] @symbol.name) @symbol.method
(variable_declarator name: (identifier) @symbol.name value: (arrow_function) @symbol.value) @symbol.function
(import_statement) @import
(call_expression function: (_) @call.name) @call
(string) @literal.string
(template_string) @literal.string
(comment) @comment
"""


def extract(text: str, rel_path: str, parsed: ParsedTree) -> ExtractedFile:
    language = parsed.language
    facts = []
    symbols = []
    for capture in run_query(language, QUERY, parsed.root_node):
        start, end = node_lines(capture.node)
        if capture.name == "import":
            facts.append(("import", node_text(parsed.source_bytes, capture.node).strip().rstrip(";"), start, end))
        elif capture.name == "literal.string":
            facts.append(("string_literal", strip_quotes(node_text(parsed.source_bytes, capture.node))[:300], start, end))
        elif capture.name == "comment":
            facts.append(("comment", node_text(parsed.source_bytes, capture.node).strip()[:500], start, end))
    seen = set()
    for _pattern, captures in query_matches(language, QUERY, parsed.root_node):
        node = _first(captures, "symbol.function", "symbol.class", "symbol.method")
        name_node = _first(captures, "symbol.name")
        if node is None or name_node is None or id(node) in seen:
            continue
        seen.add(id(node))
        name = node_text(parsed.source_bytes, name_node).strip()
        kind = "class" if captures.get("symbol.class") else "function"
        module = path_module_ref(rel_path)
        signature = node_text(parsed.source_bytes, node).splitlines()[0].strip()[:240]
        calls = _calls(parsed.source_bytes, node, language)
        literals = _literals(parsed.source_bytes, node, language)
        symbols.append(make_symbol(rel_path, f"{module}.{name}", name, name, kind, signature, "", "", node, text, calls, literals))
    return ExtractedFile(language, parsed.parser_name, parsed.parser_available, parsed.parse_error_count, facts, symbols)


def _calls(source_bytes, root, language):
    calls = []
    for _pattern, captures in query_matches(language, "(call_expression function: (_) @call.name) @call", root):
        call_node = _first(captures, "call")
        name_node = _first(captures, "call.name")
        if call_node is None or name_node is None:
            continue
        call_text = node_text(source_bytes, name_node).strip()
        receiver, _, function_name = call_text.rpartition(".")
        start, end = node_lines(call_node)
        calls.append((call_text, receiver or None, function_name, start, end))
    return calls


def _literals(source_bytes, root, language):
    literals = []
    for capture in run_query(language, "(string) @literal.string (template_string) @literal.string (number) @literal.number", root):
        start, end = node_lines(capture.node)
        literals.append((capture.name.rsplit(".", 1)[-1], strip_quotes(node_text(source_bytes, capture.node))[:300], start, end))
    return literals


def _first(captures, *names):
    for name in names:
        if captures.get(name):
            return captures[name][0]
    return None
