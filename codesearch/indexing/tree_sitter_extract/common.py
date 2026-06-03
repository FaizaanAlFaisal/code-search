from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tree_sitter import Parser, Query, QueryCursor

from codesearch.indexing.tree_sitter_extract.models import ExtractedSymbol, ParsedTree


@dataclass
class QueryCapture:
    pattern_index: int
    name: str
    node: Any


def parse_tree(language: str, text: str) -> ParsedTree:
    from tree_sitter_language_pack import get_language, get_parser

    source_bytes = text.encode("utf-8")
    native_root = None
    parser_available = False
    parser_name = "tree-sitter-fallback"
    try:
        native_tree = get_parser(language).parse(text)
        native_root = _root_node(native_tree)
        parser_available = True
        parser_name = f"tree-sitter-{language}"
    except Exception:
        native_root = None

    query_root = None
    try:
        ts_language = get_language(language)
        parser = Parser(ts_language)
        query_root = parser.parse(source_bytes).root_node
        parser_available = True
        parser_name = f"tree-sitter-{language}"
    except Exception:
        query_root = native_root

    return ParsedTree(
        language=language,
        text=text,
        source_bytes=source_bytes,
        root_node=query_root,
        native_root_node=native_root,
        parse_error_count=count_error_nodes(query_root or native_root),
        parser_available=parser_available,
        parser_name=parser_name,
    )


def walk(node: Any):
    if node is None:
        return
    yield node
    for child in named_children(node):
        yield from walk(child)


def node_kind(node: Any) -> str:
    value = getattr(node, "type", None)
    if value is not None:
        return value() if callable(value) else value
    value = getattr(node, "kind", None)
    if value is not None:
        return value() if callable(value) else value
    return ""


def node_text(source_bytes: bytes, node: Any) -> str:
    return source_bytes[node_start_byte(node) : node_end_byte(node)].decode("utf-8", errors="replace")


def node_lines(node: Any) -> tuple[int, int]:
    start = _point(node, "start_point", "start_position")
    end = _point(node, "end_point", "end_position")
    if start is None or end is None:
        return 1, 1
    return int(_point_coord(start, "row")) + 1, int(_point_coord(end, "row")) + 1


def child_by_field_name(node: Any, field: str) -> Any | None:
    func = getattr(node, "child_by_field_name", None)
    if not callable(func):
        return None
    try:
        return func(field)
    except Exception:
        return None


def named_children(node: Any) -> list[Any]:
    count = _call_attr(node, "named_child_count", None)
    if isinstance(count, int):
        child = getattr(node, "named_child", None)
        if callable(child):
            return [child(idx) for idx in range(count)]
    children = getattr(node, "children", None)
    if children is not None:
        return list(children() if callable(children) else children)
    return []


def count_error_nodes(root: Any) -> int:
    if root is None:
        return 0
    count = 0
    for node in walk(root):
        is_error = bool(_call_attr(node, "is_error", False))
        has_error = bool(_call_attr(node, "has_error", False))
        if node_kind(node) == "ERROR" or is_error or has_error and node is root:
            count += 1
    return count


def run_query(language: str, query_text: str, root: Any) -> list[QueryCapture]:
    from tree_sitter_language_pack import get_language

    if root is None or not hasattr(root, "type"):
        return []
    try:
        query = Query(get_language(language), query_text)
    except Exception:
        return []
    captures: list[QueryCapture] = []
    for pattern_index, capture_map in QueryCursor(query).matches(root):
        for name, nodes in capture_map.items():
            for node in nodes:
                captures.append(QueryCapture(pattern_index=pattern_index, name=name, node=node))
    return captures


def query_matches(language: str, query_text: str, root: Any) -> list[tuple[int, dict[str, list[Any]]]]:
    from tree_sitter_language_pack import get_language

    if root is None or not hasattr(root, "type"):
        return []
    try:
        query = Query(get_language(language), query_text)
        return list(QueryCursor(query).matches(root))
    except Exception:
        return []


def primary_text(
    path: str,
    name: str,
    signature: str,
    decorators: str,
    docstring: str,
    calls: list[tuple[str, str | None, str, int, int]],
    literals: list[tuple[str, str, int, int]],
    source: str,
) -> str:
    call_text = " ".join(call[0] for call in calls)
    literal_text = " ".join(lit[1] for lit in literals[:60])
    return "\n".join([path, name, signature, decorators, docstring, f"calls: {call_text}", f"literals: {literal_text}", source[:8000]])


def path_module_ref(rel_path: str) -> str:
    return ".".join(Path(rel_path).with_suffix("").parts)


def fallback_symbol_ref(rel_path: str, name: str) -> str:
    return f"{rel_path}::{name}"


def node_start_byte(node: Any) -> int:
    return int(_call_attr(node, "start_byte", 0))


def node_end_byte(node: Any) -> int:
    return int(_call_attr(node, "end_byte", 0))


def source_for_node(text: str, node: Any) -> str:
    start, end = node_lines(node)
    lines = text.splitlines()
    return "\n".join(lines[start - 1 : end])


def make_symbol(
    rel_path: str,
    symbol_ref: str,
    name: str,
    qualified_name: str,
    symbol_type: str,
    signature: str,
    decorators: str,
    docstring: str,
    node: Any,
    text: str,
    calls: list[tuple[str, str | None, str, int, int]],
    literals: list[tuple[str, str, int, int]],
) -> ExtractedSymbol:
    line_start, line_end = node_lines(node)
    source = source_for_node(text, node)
    return ExtractedSymbol(
        name=name,
        qualified_name=qualified_name,
        symbol_ref=symbol_ref,
        symbol_type=symbol_type,
        signature=signature,
        decorators=decorators,
        docstring=docstring,
        line_start=line_start,
        line_end=line_end,
        byte_start=node_start_byte(node),
        byte_end=node_end_byte(node),
        primary_text=primary_text(rel_path, qualified_name, signature, decorators, docstring, calls, literals, source),
        calls=calls,
        literals=literals,
    )


def _root_node(tree: Any) -> Any:
    node = getattr(tree, "root_node", None)
    return node() if callable(node) else node


def _point(node: Any, modern: str, native: str) -> Any | None:
    value = getattr(node, modern, None)
    if value is None:
        value = getattr(node, native, None)
    return value() if callable(value) else value


def _point_coord(point: Any, name: str) -> int:
    value = getattr(point, name, 0)
    return value() if callable(value) else value


def _call_attr(obj: Any, name: str, default: Any) -> Any:
    value = getattr(obj, name, None)
    if value is None:
        return default
    try:
        return value() if callable(value) else value
    except Exception:
        return default
