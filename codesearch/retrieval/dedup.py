from __future__ import annotations

import math


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def collapse_families(order_ids: list[int], vectors: dict[int, list[float]], floor: float, min_size: int, protected: set[int]) -> dict[int, int]:
    # connected components over cosine >= floor; collapse components larger than min_size.
    # returns member_id -> representative_id for members rendered as signatures only.
    ids = [i for i in order_ids if i in vectors]
    parent = {i: i for i in ids}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            if _cosine(vectors[ids[i]], vectors[ids[j]]) >= floor:
                union(ids[i], ids[j])

    components: dict[int, list[int]] = {}
    for i in ids:
        components.setdefault(find(i), []).append(i)

    collapsed: dict[int, int] = {}
    for members in components.values():
        if len(members) <= min_size:
            continue
        members.sort(key=order_ids.index)  # earliest (highest-ranked) is the representative
        rep = members[0]
        for member in members[1:]:
            if member not in protected:
                collapsed[member] = rep
    return collapsed
