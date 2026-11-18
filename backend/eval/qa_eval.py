"""Evaluate the retrieval half of question answering: when is there enough evidence to answer,
and does the assembled context contain the code that answers the question?

No language model is called. It indexes a pinned commit through the real pipeline (including the
dependency graph), then for every question runs the same `classify` and `retrieve` the chat
endpoint runs and records the closest chunk's cosine similarity, whether the question names a
symbol that exists, and whether the labelled code ends up in the excerpts shown to a model, with
and without graph expansion.

    uv run python -m eval.qa_eval --dataset eval/datasets/requests.json --repo-key psf/requests
    uv run python -m eval.qa_eval --dataset eval/datasets/click.json --repo-key pallets/click \\
        --threshold 0.55
"""

import argparse
import json
import statistics
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.providers.embeddings.base import Embedder
from app.providers.embeddings.factory import get_embedder
from app.services.github.client import GitHubClient
from app.services.qa.classify import classify
from app.services.qa.context import build_excerpts
from app.services.qa.expand import graph_neighbours
from app.services.qa.retrieve import Retrieval, retrieve
from app.services.qa.types import Excerpt
from app.services.retrieval.search import _has_embeddings
from eval.labels import Target, is_relevant, resolve
from eval.pipeline import indexed_snapshot

HERE = Path(__file__).parent
BUDGETS = (3, 4, 5, 6, 8)  # excerpt counts to compare; the shipped default is in settings
THRESHOLDS = [round(0.30 + 0.025 * i, 3) for i in range(0, 25)]  # 0.30 .. 0.90


def _observe(
    db: Session,
    sid: uuid.UUID,
    embedder: Embedder,
    ready: bool,
    question: str,
    settings: Settings,
) -> Retrieval:
    c = classify(question)
    return retrieve(db, sid, embedder, ready, c.search_query, c.names, settings.qa_retrieve_k)


def _contains(excerpts: Sequence[Excerpt], targets: list[Target]) -> bool:
    return any(is_relevant(e.path, e.start_line, e.end_line, targets) for e in excerpts)


def _sims(rows: list[dict[str, Any]]) -> list[float]:
    return sorted(float(r["top_similarity"] or 0.0) for r in rows)


def _spread(rows: list[dict[str, Any]]) -> dict[str, float]:
    values = _sims(rows)
    return {
        "min": round(values[0], 3),
        "median": round(statistics.median(values), 3),
        "max": round(values[-1], 3),
    }


def evaluate(
    dataset: dict[str, Any],
    unanswerable: dict[str, Any],
    settings: Settings,
    threshold: float | None,
) -> dict[str, Any]:
    owner, name = dataset["repo"].split("/")
    kinds: dict[str, str] = {q: "off_topic" for q in unanswerable["off_topic"]}
    for kind in ("absent_feature", "absent_name"):
        kinds.update({q: kind for q in unanswerable[dataset["repo"]][kind]})

    with indexed_snapshot(
        owner, name, dataset["commit"], settings, GitHubClient(token=None), through="graph"
    ) as (db, sid, _cursor):
        embedder = get_embedder(settings)
        ready = _has_embeddings(db, sid)
        answerable: list[dict[str, Any]] = []
        for q in dataset["questions"]:
            targets = resolve(db, sid, q["relevant"])
            r = _observe(db, sid, embedder, ready, q["question"], settings)
            budget = (settings.qa_context_chunks, settings.qa_context_chars)
            plain = build_excerpts(r.hits, [], *budget)
            neighbours = graph_neighbours(db, sid, r.hits, settings.qa_expand_max)
            full = build_excerpts(r.hits, neighbours, *budget)
            by_budget = {
                str(b): {
                    "plain": _contains(build_excerpts(r.hits, [], b, 10**9), targets),
                    "expanded": _contains(build_excerpts(r.hits, neighbours, b, 10**9), targets),
                    # neighbours take the last two slots instead of waiting behind every hit
                    "reserved": _contains(
                        build_excerpts(r.hits[: max(b - 2, 1)], neighbours, b, 10**9), targets
                    ),
                }
                for b in BUDGETS
            }
            answerable.append(
                {
                    "id": q["id"],
                    "type": q["type"],
                    "question": q["question"],
                    "top_similarity": r.top_similarity,
                    "symbol_hit": r.symbol_hit,
                    "in_top_k": any(
                        is_relevant(h.path, h.start_line, h.end_line, targets) for h in r.hits
                    ),
                    "by_budget": by_budget,
                    "in_context_plain": _contains(plain, targets),
                    "in_context_expanded": _contains(full, targets),
                    "excerpts_plain": len(plain),
                    "excerpts_expanded": len(full),
                    "chars_plain": sum(len(e.text) for e in plain),
                    "chars_expanded": sum(len(e.text) for e in full),
                }
            )
        refused: list[dict[str, Any]] = []
        for question, kind in kinds.items():
            r = _observe(db, sid, embedder, ready, question, settings)
            refused.append(
                {
                    "question": question,
                    "kind": kind,
                    "top_similarity": r.top_similarity,
                    "symbol_hit": r.symbol_hit,
                }
            )
    return summarise(dataset, answerable, refused, threshold, settings)


def _rate(rows: list[dict[str, Any]], t: float, with_symbol: bool) -> float:
    """Share of questions the rule would answer at threshold `t`."""
    ok = [(r["top_similarity"] or 0.0) >= t or (with_symbol and r["symbol_hit"]) for r in rows]
    return sum(ok) / len(ok) if ok else 0.0


def summarise(
    dataset: dict[str, Any],
    answerable: list[dict[str, Any]],
    refused: list[dict[str, Any]],
    threshold: float | None,
    settings: Settings,
) -> dict[str, Any]:
    sweep = []
    for t in THRESHOLDS:
        answered = _rate(answerable, t, True)
        declined = 1 - _rate(refused, t, True)
        sweep.append(
            {
                "threshold": t,
                "answers_answerable": round(answered, 3),
                "declines_unanswerable": round(declined, 3),
                "balanced_accuracy": round((answered + declined) / 2, 3),
                "answers_answerable_similarity_only": round(_rate(answerable, t, False), 3),
                "declines_unanswerable_similarity_only": round(1 - _rate(refused, t, False), 3),
            }
        )
    at = threshold if threshold is not None else settings.qa_min_similarity
    n = len(answerable)
    by_kind: dict[str, dict[str, Any]] = {}
    for kind in ("off_topic", "absent_feature", "absent_name"):
        rows = [r for r in refused if r["kind"] == kind]
        by_kind[kind] = {
            "n": len(rows),
            "declined": round(1 - _rate(rows, at, True), 3),
            "max_similarity": round(_sims(rows)[-1], 3),
            "symbol_hits": sum(1 for r in rows if r["symbol_hit"]),
        }

    def share(key: str) -> float:
        return round(sum(1 for r in answerable if r[key]) / n, 3)

    def mean(key: str) -> float:
        return round(float(statistics.mean(r[key] for r in answerable)), 2)

    return {
        "repo": dataset["repo"],
        "commit": dataset["commit"],
        "embedding_model": settings.cloudflare_embedding_model,
        "threshold_evaluated": at,
        "similarity": {"answerable": _spread(answerable), "unanswerable": _spread(refused)},
        "at_threshold": {
            "answers_answerable": round(_rate(answerable, at, True), 3),
            "declines_unanswerable": round(1 - _rate(refused, at, True), 3),
            "by_unanswerable_kind": by_kind,
        },
        "context": {
            "n": n,
            "labelled_code_in_top_k": share("in_top_k"),
            "labelled_code_in_context_without_graph": share("in_context_plain"),
            "labelled_code_in_context_with_graph": share("in_context_expanded"),
            "added_by_graph": sum(
                1 for r in answerable if r["in_context_expanded"] and not r["in_context_plain"]
            ),
            "lost_by_graph": sum(
                1 for r in answerable if r["in_context_plain"] and not r["in_context_expanded"]
            ),
            "mean_excerpts_without_graph": mean("excerpts_plain"),
            "mean_excerpts_with_graph": mean("excerpts_expanded"),
            "mean_chars_without_graph": round(mean("chars_plain")),
            "mean_chars_with_graph": round(mean("chars_expanded")),
        },
        "budgets": {
            str(b): {
                "without_graph": round(
                    sum(1 for r in answerable if r["by_budget"][str(b)]["plain"]) / n, 3
                ),
                "with_graph": round(
                    sum(1 for r in answerable if r["by_budget"][str(b)]["expanded"]) / n, 3
                ),
                "reserved_slots": round(
                    sum(1 for r in answerable if r["by_budget"][str(b)]["reserved"]) / n, 3
                ),
                "gained": sum(
                    1
                    for r in answerable
                    if r["by_budget"][str(b)]["expanded"] and not r["by_budget"][str(b)]["plain"]
                ),
                "lost": sum(
                    1
                    for r in answerable
                    if r["by_budget"][str(b)]["plain"] and not r["by_budget"][str(b)]["expanded"]
                ),
            }
            for b in BUDGETS
        },
        "sweep": sweep,
        "answerable": answerable,
        "unanswerable": refused,
    }


def _pct(x: float) -> str:
    return f"{x:.0%}"


def render(result: dict[str, Any]) -> str:
    sim, at, ctx = result["similarity"], result["at_threshold"], result["context"]
    a, u = sim["answerable"], sim["unanswerable"]
    lines = [
        f"# Question answering retrieval: {result['repo']}",
        "",
        f"- Commit `{result['commit'][:10]}`; embedding model `{result['embedding_model']}`.",
        "- Closest-chunk cosine similarity. Answerable questions: "
        f"min {a['min']}, median {a['median']}, max {a['max']}. Unanswerable: "
        f"min {u['min']}, median {u['median']}, max {u['max']}.",
        "",
        f"## Decision at similarity threshold {result['threshold_evaluated']}",
        "",
        f"- Answers {_pct(at['answers_answerable'])} of the answerable questions and declines "
        f"{_pct(at['declines_unanswerable'])} of the unanswerable ones.",
    ]
    for kind, v in at["by_unanswerable_kind"].items():
        lines.append(
            f"  - {kind}: declined {_pct(v['declined'])} of {v['n']} "
            f"(highest similarity {v['max_similarity']}, named-symbol matches {v['symbol_hits']})"
        )
    lines += [
        "",
        "## Is the labelled code in what the model would see?",
        "",
        f"- In the top retrieved chunks: {_pct(ctx['labelled_code_in_top_k'])}",
        "- In the assembled excerpts without graph expansion: "
        f"{_pct(ctx['labelled_code_in_context_without_graph'])}",
        f"- With graph expansion: {_pct(ctx['labelled_code_in_context_with_graph'])} "
        f"({ctx['added_by_graph']} questions gained it, {ctx['lost_by_graph']} lost it)",
        f"- Mean excerpts: {ctx['mean_excerpts_without_graph']} without, "
        f"{ctx['mean_excerpts_with_graph']} with; mean characters: "
        f"{ctx['mean_chars_without_graph']} without, {ctx['mean_chars_with_graph']} with.",
        "",
        "## Labelled code in context, by number of excerpts shown",
        "",
        "| excerpts | search only | neighbours after hits | gained | lost | "
        "neighbours replace last 2 hits |",
        "| --- | --- | --- | --- | --- | --- |",
        *[
            f"| {b} | {_pct(v['without_graph'])} | {_pct(v['with_graph'])} | "
            f"{v['gained']} | {v['lost']} | {_pct(v['reserved_slots'])} |"
            for b, v in result["budgets"].items()
        ],
        "",
        "## Threshold sweep (the full rule: a named symbol OR similarity)",
        "",
        "| threshold | answers answerable | declines unanswerable | balanced | "
        "similarity only: answers / declines |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in result["sweep"]:
        only = (
            f"{_pct(row['answers_answerable_similarity_only'])} / "
            f"{_pct(row['declines_unanswerable_similarity_only'])}"
        )
        lines.append(
            f"| {row['threshold']} | {_pct(row['answers_answerable'])} | "
            f"{_pct(row['declines_unanswerable'])} | {row['balanced_accuracy']:.3f} | {only} |"
        )
    lines += ["", "Small samples: read differences between neighbouring thresholds as noise."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", required=True)
    p.add_argument("--threshold", type=float, default=None, help="threshold to report at")
    p.add_argument("--out", default=str(HERE / "results"))
    args = p.parse_args(argv)
    dataset = json.loads(Path(args.dataset).read_text())
    unanswerable = json.loads((HERE / "datasets" / "unanswerable.json").read_text())
    settings = get_settings()
    if settings.embedding_provider != "cloudflare":
        print(
            "This evaluation needs real embeddings (EMBEDDING_PROVIDER=cloudflare).",
            file=sys.stderr,
        )
        return 2
    result = evaluate(dataset, unanswerable, settings, args.threshold)
    out = Path(args.out)
    out.mkdir(exist_ok=True)
    stem = "qa_" + dataset["repo"].replace("/", "_")
    (out / f"{stem}.json").write_text(json.dumps(result, indent=2))
    (out / f"{stem}.md").write_text(render(result))
    print(render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
