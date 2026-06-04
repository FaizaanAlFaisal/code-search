from __future__ import annotations

import json


def print_json(data: object) -> None:
    print(json.dumps(data, indent=2, sort_keys=True, default=str))


def format_search(result: dict, slug: str) -> str:
    return _format_results(result, slug, "likely locations", "Search failed.")


def format_find(result: dict, slug: str) -> str:
    return _format_results(result, slug, "exact matches", "Find failed.")


def _format_results(result: dict, slug: str, noun: str, fail: str) -> str:
    if not result.get("ok"):
        return result.get("message") or result.get("error", fail)
    rows = result.get("results", [])
    if not rows:
        return f"Found no {noun}."
    lines = [f"Found {len(rows)} {noun}. Open: code-search open {slug} <id|symbol>"]
    for idx, row in enumerate(rows, start=1):
        ref = f"  {row['symbol']}" if row.get("symbol") else ""
        mark = "  (stale)" if row.get("stale") else ""
        lines.append("")
        lines.append(f"{idx}. {row['result_id']}  {row['path']}:{row['line_start']}-{row['line_end']}{ref}{mark}")
        sig = _clean_signature(row.get("signature"))
        if sig:
            lines.append(f"   sig: {sig}")
        lines.append(f"   why: {row.get('why') or 'match'}")
    banner = _stale_banner(slug, result.get("stale_count", 0))
    if banner:
        lines.append("")
        lines.append(banner)
    return "\n".join(lines)


def _stale_banner(slug: str, count: int) -> str:
    # Surfaced whenever results come from files edited since indexing. Refreshing is
    # optional: the agent decides based on how much it needs the affected code.
    if not count:
        return ""
    return (
        f"⚠ {count} result(s) marked (stale): the file changed since indexing, so the "
        f"shown lines/source may be outdated. If any stale hit is central to your task, "
        f"run `code-search refresh {slug}` now and retry; otherwise keep going and refresh "
        f"after the task is done."
    )


def _clean_signature(signature: str | None) -> str:
    if not signature:
        return ""
    flat = " ".join(signature.split())
    return flat[:120] + "…" if len(flat) > 120 else flat


def format_open(result: dict, slug: str = "") -> str:
    if not result.get("ok"):
        if result.get("error") == "ambiguous":
            return _format_ambiguous(result)
        return result.get("message") or result.get("error", "Open failed.")
    stale = "stale" if result.get("stale") else "current"
    out = [f"{result['path']}:{result['line_start']}-{result['line_end']}", f"Indexed hash: {stale}", "", result["excerpt"]]
    if result.get("stale"):
        out.append("")
        out.append(
            f"⚠ This file changed since indexing — the lines above may be misaligned or outdated. "
            f"If this code is central to your task, run `code-search refresh {slug}` and re-open; "
            f"otherwise refresh after the task is done."
        )
    trail = result.get("trail") or {}
    parts = []
    if trail.get("flags"):
        parts.append(f"flags: {', '.join(trail['flags'])}")
    if trail.get("callers"):
        parts.append(f"callers: {', '.join(trail['callers'][:10])}")
    if trail.get("callees"):
        parts.append(f"calls: {', '.join(trail['callees'][:12])}")
    if parts:
        out.append("")
        out.extend(parts)
    return "\n".join(out)


def format_explore(result: dict, slug: str) -> str:
    if not result.get("ok"):
        return result.get("message") or result.get("error", "Explore failed.")
    files = result.get("files", [])
    if not files:
        return result.get("note") or "Found nothing to explore."
    lines: list[str] = []
    for f in files:
        lines.append(f"=== {f['path']} ===")
        for b in f["blocks"]:
            mark = "  (stale)" if b.get("stale") else ""
            lines.append(f"{b['symbol_ref']}  lines {b['line_start']}-{b['line_end']}{mark}")
            if b.get("flags"):
                lines.append(f"flags: {b['flags']}")
            if b.get("stale"):
                sig = _clean_signature(b.get("signature"))
                lines.append(f"  source withheld — file edited since indexing (refresh to view). {sig}".rstrip())
            elif b.get("collapsed_to"):
                sig = _clean_signature(b.get("signature"))
                lines.append(f"  ≈ near-identical to {b['collapsed_to']} — {sig or 'signature only'} (open to expand)")
            else:
                lines.append("```")
                lines.append(b["body"])
                lines.append("```")
        lines.append("")
    blast = result.get("blast") or []
    if blast:
        lines.append("--- relationships (callers ← / tests) ---")
        for entry in blast:
            callers = ", ".join(entry["callers"]) or "(no callers found)"
            line = f"{entry['symbol_ref']} ← {callers}"
            if entry.get("tests"):
                line += f"  | tests: {', '.join(entry['tests'])}"
            lines.append(line)
    if result.get("truncated"):
        lines.append("")
        lines.append("(truncated to budget — open specific symbols for the rest)")
    banner = _stale_banner(slug, result.get("stale_count", 0))
    if banner:
        lines.append("")
        lines.append(banner)
    return "\n".join(lines)


def _format_ambiguous(result: dict) -> str:
    lines = ["Multiple symbols matched:"]
    for idx, row in enumerate(result.get("matches", []), start=1):
        lines.append(f"{idx}. {row['symbol_ref']}")
        lines.append(f"   {row['path']}:{row['line_start']}-{row['line_end']}")
    lines.append("Use the full symbol ref or path.")
    return "\n".join(lines)

