"""Retrieval metrics. A "rank" is the 1-based position of the first relevant result, or None
when no relevant result was returned within the cutoff."""

import random
from collections.abc import Callable, Sequence
from statistics import mean

Rank = int | None


def recall_at_k(ranks: Sequence[Rank], k: int) -> float:
    """Share of questions whose first relevant result is within the top k."""
    if not ranks:
        return 0.0
    return sum(1 for r in ranks if r is not None and r <= k) / len(ranks)


def mrr(ranks: Sequence[Rank], cutoff: int = 10) -> float:
    """Mean reciprocal rank: 1 for a first-place hit, 1/2 for second, 0 when missed."""
    if not ranks:
        return 0.0
    return mean(1 / r if r is not None and r <= cutoff else 0.0 for r in ranks)


def bootstrap_ci(
    ranks: Sequence[Rank],
    statistic: Callable[[Sequence[Rank]], float],
    resamples: int = 2000,
    seed: int = 7,
    confidence: float = 0.95,
) -> tuple[float, float]:
    """Percentile bootstrap interval for a metric. With few questions the interval is wide, and
    reporting it is what keeps a small benchmark honest."""
    if not ranks:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(ranks)
    stats = sorted(statistic([ranks[rng.randrange(n)] for _ in range(n)]) for _ in range(resamples))
    low = stats[int(((1 - confidence) / 2) * resamples)]
    high = stats[min(resamples - 1, int((1 - (1 - confidence) / 2) * resamples))]
    return (low, high)


def compare(a: Sequence[Rank], b: Sequence[Rank]) -> dict[str, int]:
    """Question by question, how often A ranks the answer above B (a miss counts as worst)."""
    if len(a) != len(b):
        raise ValueError("rankings must cover the same questions")
    worst = 10**9
    wins = losses = ties = 0
    for x, y in zip(a, b, strict=True):
        xv, yv = x if x is not None else worst, y if y is not None else worst
        if xv < yv:
            wins += 1
        elif xv > yv:
            losses += 1
        else:
            ties += 1
    return {"wins": wins, "losses": losses, "ties": ties}
