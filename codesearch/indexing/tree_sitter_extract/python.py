from __future__ import annotations

from pathlib import Path

from codesearch.indexing.tree_sitter_extract.common import (
    child_by_field_name,
    make_symbol,
    named_children,
    node_lines,
    node_text,
    query_matches,
    run_query,
    walk,
)
from codesearch.indexing.tree_sitter_extract.fallback import strip_quotes
from codesearch.indexing.tree_sitter_extract.models import ExtractedFile, ExtractedSymbol, ParsedTree

QUERY = """
(function_definition
  name: (identifier) @symbol.name) @symbol.function
(class_definition
  name: (identifier) @symbol.name) @symbol.class
(decorated_definition) @symbol.decorated
(import_statement) @import
(import_from_statement) @import
(call
  function: (_) @call.name) @call
(string) @literal.string
(comment) @comment
"""


def extract(text: str, rel_path: str, parsed: ParsedTree) -> ExtractedFile:
    facts: list[tuple[str, str, int | None, int | None]] = []
    symbols: list[ExtractedSymbol] = []
    root = parsed.root_node
    if root is None:
        return ExtractedFile("python", parsed.parser_name, parsed.parser_available, parsed.parse_error_count, facts, symbols)

    for capture in run_query("python", QUERY, root):
        start, end = node_lines(capture.node)
        if capture.name == "import":
            facts.append(("import", node_text(parsed.source_bytes, capture.node).strip(), start, end))
        elif capture.name == "literal.string":
            facts.append(("string_literal", strip_quotes(node_text(parsed.source_bytes, capture.node))[:300], start, end))
        elif capture.name == "comment":
            facts.append(("comment", node_text(parsed.source_bytes, capture.node).strip()[:500], start, end))

    decorated_targets = _decorated_targets(root)
    seen_nodes: set[int] = set()
    for _pattern, captures in query_matches("python", QUERY, root):
        symbol_node = _single(captures.get("symbol.function")) or _single(captures.get("symbol.class"))
        name_node = _single(captures.get("symbol.name"))
        if symbol_node is None or name_node is None or id(symbol_node) in seen_nodes:
            continue
        seen_nodes.add(id(symbol_node))
        symbol_type = "class" if captures.get("symbol.class") else "function"
        decorators, effective_node = decorated_targets.get(_node_key(symbol_node), ([], symbol_node))
        symbols.append(_build_symbol(text, rel_path, parsed, effective_node, name_node, symbol_type, decorators))

    return ExtractedFile("python", parsed.parser_name, parsed.parser_available, parsed.parse_error_count, facts, symbols)


def _build_symbol(
    text: str,
    rel_path: str,
    parsed: ParsedTree,
    node,
    name_node,
    symbol_type: str,
    decorators: list[str],
) -> ExtractedSymbol:
    name = node_text(parsed.source_bytes, name_node).strip()
    qualified = _qualified_name(node, name)
    module = _python_module_ref(rel_path)
    signature = _signature(parsed.source_bytes, node, symbol_type, name)
    calls = _calls(parsed.source_bytes, node)
    literals = _literals(parsed.source_bytes, node)
    docstring = _docstring(parsed.source_bytes, node)
    return make_symbol(
        rel_path=rel_path,
        symbol_ref=f"{module}.{qualified}" if module else f"{rel_path}::{qualified}",
        name=name,
        qualified_name=qualified,
        symbol_type=symbol_type,
        signature=signature,
        decorators=" ".join(decorators),
        docstring=docstring,
        node=node,
        text=text,
        calls=calls,
        literals=literals,
    )


def _decorated_targets(root) -> dict[tuple[int, int], tuple[list[str], object]]:
    decorated: dict[tuple[int, int], tuple[list[str], object]] = {}
    for node in walk(root):
        if getattr(node, "type", "") != "decorated_definition":
            continue
        decorators = []
        target = None
        for child in named_children(node):
            kind = getattr(child, "type", "")
            if kind == "decorator":
                text = getattr(child, "text", b"")
                decorators.append(text.decode("utf-8", errors="replace").strip() if text else "")
            elif kind in {"function_definition", "class_definition"}:
                target = child
        if target is not None:
            decorated[_node_key(target)] = (decorators, node)
    return decorated


def _node_key(node) -> tuple[int, int]:
    return (int(getattr(node, "start_byte", 0)), int(getattr(node, "end_byte", 0)))


def _qualified_name(node, name: str) -> str:
    names = [name]
    parent = getattr(node, "parent", None)
    while parent is not None:
        if getattr(parent, "type", "") == "class_definition":
            name_node = child_by_field_name(parent, "name")
            if name_node is not None:
                # Source text is unavailable here, so use node text only when exposed.
                text = getattr(name_node, "text", None)
                if text:
                    names.append(text.decode("utf-8", errors="replace"))
        parent = getattr(parent, "parent", None)
    return ".".join(reversed(names))


def _python_module_ref(rel_path: str) -> str:
    path = Path(rel_path)
    if path.name == "__init__.py":
        path = path.parent
    else:
        path = path.with_suffix("")
    return ".".join(part for part in path.parts if part)


def _signature(source_bytes: bytes, node, symbol_type: str, name: str) -> str:
    source = node_text(source_bytes, node)
    first = source.splitlines()[0].strip()
    if symbol_type == "class":
        return first.rstrip(":")
    if first.endswith(":"):
        return first[:-1].strip()
    return first or name


def _calls(source_bytes: bytes, root) -> list[tuple[str, str | None, str, int, int]]:
    calls = []
    for _pattern, captures in query_matches("python", "(call function: (_) @call.name) @call", root):
        call_node = _single(captures.get("call"))
        name_node = _single(captures.get("call.name"))
        if call_node is None or name_node is None:
            continue
        call_text = node_text(source_bytes, name_node).strip()
        receiver, _, function_name = call_text.rpartition(".")
        start, end = node_lines(call_node)
        calls.append((call_text, receiver or None, function_name, start, end))
    return calls


def _literals(source_bytes: bytes, root) -> list[tuple[str, str, int, int]]:
    literals = []
    for capture in run_query("python", "(string) @literal.string (integer) @literal.number (float) @literal.number (true) @literal.bool (false) @literal.bool", root):
        start, end = node_lines(capture.node)
        literal_type = capture.name.rsplit(".", 1)[-1]
        literals.append((literal_type, strip_quotes(node_text(source_bytes, capture.node))[:300], start, end))
    return literals


def _docstring(source_bytes: bytes, root) -> str:
    body = child_by_field_name(root, "body")
    candidates = named_children(body) if body is not None else named_children(root)
    if candidates and getattr(candidates[0], "type", "") == "expression_statement":
        strings = [node for node in walk(candidates[0]) if getattr(node, "type", "") == "string"]
        if strings:
            return strip_quotes(node_text(source_bytes, strings[0]))[:1000]
    return ""


def _single(nodes):
    return nodes[0] if nodes else None
