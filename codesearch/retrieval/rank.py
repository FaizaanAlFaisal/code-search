from __future__ import annotations

import re

# rrf fusion: lexical (bm25) -> semantic (cosine) -> evidence (term overlap)
RRF_K = 60

# nudge specific hits over the file/module containing them; only breaks near-ties
TYPE_PRIOR = {"symbol": 0.5, "file": 0.25, "module": 0.0}

# demote test targets (~1 rrf rank-unit) so they stop crowding the top over impls
TEST_PENALTY = 1.0
# pytest/unittest naming: test_*, testFoo, *Test/*Tests/*TestCase, *Spec (case-sensitive suffix avoids "latest")
_TEST_NAME_RE = re.compile(r"^test[_A-Z]|^test$|Test$|Tests$|TestCase$|Spec$")
# path dir / ref component that marks a test location
_TEST_SEGMENT = {"test", "tests", "__tests__", "testing", "spec", "specs"}


def rerank(candidates: list[dict], query: str, k: int = RRF_K, limit: int | None = None) -> list[dict]:
    """Fuse fts_rank/vector_rank/evidence via RRF, sorted best-first."""
    terms = _terms(query)
    evidence = {id(c): _evidence_score(c, terms) for c in candidates}
    evidence_order = [c for c in sorted(candidates, key=lambda c: evidence[id(c)], reverse=True) if evidence[id(c)] > 0]
    evidence_rank = {id(c): pos for pos, c in enumerate(evidence_order)}

    for c in candidates:
        signals: dict[str, float] = {}
        if c.get("fts_rank") is not None:
            signals["lexical"] = 1.0 / (k + c["fts_rank"] + 1)
        if c.get("vector_rank") is not None:
            signals["semantic"] = 1.0 / (k + c["vector_rank"] + 1)
        if id(c) in evidence_rank:
            signals["evidence"] = 1.0 / (k + evidence_rank[id(c)] + 1)
        prior = TYPE_PRIOR.get(c.get("record_type"), 0.0)
        if prior:
            signals["type_prior"] = prior * (1.0 / (k + 1))
        if _is_test_target(c.get("path"), c.get("symbol")):
            signals["test_penalty"] = -TEST_PENALTY * (1.0 / (k + 1))
        c["score"] = sum(signals.values())
        c["rank_signals"] = signals

    ranked = sorted(candidates, key=lambda c: c["score"], reverse=True)
    return ranked[:limit] if limit else ranked


def _terms(query: str) -> list[str]:
    return [term for term in query.lower().replace("_", " ").replace(":", " ").split() if term]


def _is_test_target(path: str | None, symbol_ref: str | None) -> bool:
    # judged on the stored path / full symbol ref (never the body)
    if path:
        segments = re.split(r"[\\/]", path.lower())
        if any(seg in _TEST_SEGMENT for seg in segments[:-1]):
            return True
        name = segments[-1]
        if name.startswith(("test_", "test.")) or any(m in name for m in ("_test.", ".test.", ".spec.")) or name.endswith("_test"):
            return True
    if symbol_ref:
        for comp in symbol_ref.split("."):
            if comp.lower() in _TEST_SEGMENT or _TEST_NAME_RE.search(comp):
                return True
    return False


def _evidence_score(candidate: dict, terms: list[str]) -> int:
    # query terms present in symbol/path/why; pure semantic hits score 0
    text = " ".join(str(candidate.get(key) or "") for key in ("symbol", "path", "why")).lower().replace("_", " ")
    return sum(1 for term in terms if term in text)
