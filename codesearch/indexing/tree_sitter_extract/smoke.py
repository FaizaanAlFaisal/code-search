from __future__ import annotations

from pathlib import Path

from tree_sitter_language_pack import downloaded_languages, has_language

from codesearch.indexing.tree_sitter_extract import extract_file

SNIPPETS = {
    "python": ("sample.py", "def delete_collection(collection, confirmed=False):\n    qdrant.delete_collection(collection)\n", "delete_collection", "qdrant.delete_collection"),
    "javascript": ("sample.js", "function store(){ qdrant.upsert(point); }\n", "store", "qdrant.upsert"),
    "typescript": ("sample.ts", "export function fetchUser(){ requests.get(url); }\n", "fetchUser", "requests.get"),
    "tsx": ("sample.tsx", "export const Widget = () => <div>{embed_text()}</div>;\n", "Widget", "embed_text"),
    "rust": ("sample.rs", "fn cleanup(){ qdrant.delete_collection(collection); }\n", "cleanup", "qdrant.delete_collection"),
    "c": ("sample.c", "int main(){ call_me(); return 0; }\n", "main", "call_me"),
    "cpp": ("sample.cpp", "int main(){ call_me(); return 0; }\n", "main", "call_me"),
    "bash": ("sample.sh", "deploy(){ curl http://example; }\n", "deploy", "curl"),
    "lua": ("sample.lua", "function save() qdrant.upsert(point) end\n", "save", "qdrant.upsert"),
    "sql": ("sample.sql", "CREATE TABLE users (id int);\n", "users", None),
    "html": ("sample.html", "<html><script>hello()</script></html>\n", None, None),
    "css": ("sample.css", ".button { color: red; }\n", ".button", None),
    "json": ("sample.json", "{\"name\": \"code-search\"}\n", None, None),
    "make": ("Makefile", "all:\n\techo hi\n", None, None),
    "yaml": ("sample.yaml", "name: code-search\n", None, None),
    "toml": ("sample.toml", "name = \"code-search\"\n", None, None),
    "markdown": ("sample.md", "# Title\n\nRun `code-search`.\n", None, None),
    "regex": ("sample.regex", "foo|bar\n", None, None),
    "dockerfile": ("Dockerfile", "FROM python:3.12\nRUN echo hi\n", None, None),
}


def parser_smoke() -> dict:
    installed = set(downloaded_languages())
    results = []
    ok = True
    for language in sorted(installed):
        path, text, expected_symbol, expected_call = SNIPPETS.get(language, (f"sample.{language}", "value: test\n", None, None))
        try:
            available = bool(has_language(language))
            extracted = extract_file(Path("."), path, text)
            symbols = [symbol.name for symbol in extracted.symbols]
            calls = [call[0] for symbol in extracted.symbols for call in symbol.calls]
            symbol_ok = expected_symbol is None or expected_symbol in symbols
            call_ok = expected_call is None or expected_call in calls
            language_ok = available and extracted.parser_available and symbol_ok and call_ok
            ok = ok and language_ok
            results.append(
                {
                    "language": language,
                    "ok": language_ok,
                    "parser_available": extracted.parser_available,
                    "parse_error_count": extracted.parse_error_count,
                    "symbols": symbols,
                    "calls": calls,
                    "expected_symbol": expected_symbol,
                    "expected_call": expected_call,
                }
            )
        except Exception as exc:
            ok = False
            results.append({"language": language, "ok": False, "error": str(exc)})
    return {"ok": ok, "results": results}
