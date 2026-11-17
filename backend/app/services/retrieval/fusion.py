"""Reciprocal Rank Fusion: merge several ranked lists without comparing their raw scores."""

from collections.abc import Mapping, Sequence

RRF_K = 60  # the constant from the original paper; larger values flatten the differences


def reciprocal_rank_fusion(
    rankings: Mapping[str, Sequence[int]],
    k: int = RRF_K,
    weights: Mapping[str, float] | None = None,
) -> list[tuple[int, float, dict[str, int]]]:
    """Each id scores sum(weight / (k + rank)) over the lists that contain it (rank starts at 1).

    Returns (id, score, {list name: rank}) best first. Ties break by the id so results are
    deterministic. Keyword and vector scores live on different scales, which is exactly why
    only ranks are used.
    """
    scores: dict[int, float] = {}
    ranks: dict[int, dict[str, int]] = {}
    for name, ids in rankings.items():
        weight = (weights or {}).get(name, 1.0)
        for position, item in enumerate(ids, start=1):
            if item in ranks and name in ranks[item]:
                continue  # a duplicate within one list counts once, at its best rank
            scores[item] = scores.get(item, 0.0) + weight / (k + position)
            ranks.setdefault(item, {})[name] = position
    ordered = sorted(scores, key=lambda i: (-scores[i], i))
    return [(i, scores[i], ranks[i]) for i in ordered]
