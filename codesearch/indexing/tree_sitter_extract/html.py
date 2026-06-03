from __future__ import annotations

from codesearch.indexing.tree_sitter_extract.common import node_lines, node_text, run_query
from codesearch.indexing.tree_sitter_extract.models import ExtractedFile, ParsedTree

LANGUAGE = "html"
QUERY = """
(comment) @comment
(start_tag (tag_name) @tag.name) @tag
(script_element) @section.script
(style_element) @section.style
"""


def extract(text: str, rel_path: str, parsed: ParsedTree) -> ExtractedFile:
    facts = []
    for capture in run_query(LANGUAGE, QUERY, parsed.root_node):
        start, end = node_lines(capture.node)
        if capture.name == "comment":
            facts.append(("comment", node_text(parsed.source_bytes, capture.node).strip()[:500], start, end))
        elif capture.name == "tag.name":
            facts.append(("html_tag", node_text(parsed.source_bytes, capture.node).strip(), start, end))
        elif capture.name.startswith("section."):
            facts.append((capture.name.replace(".", "_"), node_text(parsed.source_bytes, capture.node).strip()[:500], start, end))
    return ExtractedFile(LANGUAGE, parsed.parser_name, parsed.parser_available, parsed.parse_error_count, facts, [])

