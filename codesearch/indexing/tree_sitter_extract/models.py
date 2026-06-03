from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExtractedSymbol:
    name: str
    qualified_name: str
    symbol_ref: str
    symbol_type: str
    signature: str
    decorators: str
    docstring: str
    line_start: int
    line_end: int
    byte_start: int
    byte_end: int
    primary_text: str
    calls: list[tuple[str, str | None, str, int, int]] = field(default_factory=list)
    literals: list[tuple[str, str, int, int]] = field(default_factory=list)


@dataclass
class ExtractedFile:
    language: str | None
    parser_name: str | None
    parser_available: bool
    parse_error_count: int
    facts: list[tuple[str, str, int | None, int | None]]
    symbols: list[ExtractedSymbol]


@dataclass
class ParsedTree:
    language: str
    text: str
    source_bytes: bytes
    root_node: Any
    native_root_node: Any | None
    parse_error_count: int
    parser_available: bool
    parser_name: str

