"""Compare keyword, vector and hybrid retrieval on a labelled question set.

    uv run python -m eval.run_eval --dataset eval/datasets/requests.json
    uv run python -m eval.run_eval --dataset eval/datasets/requests.json --embedder hash
    uv run python -m eval.run_eval --dataset eval/datasets/requests.json --check-labels

It indexes the pinned commit through the real pipeline (using the database in DATABASE_URL, and
deleting what it creates), asks every question in every mode, and reports Recall@k and MRR with
bootstrap confidence intervals. Nothing here is estimated: every number comes from this run.
"""

import argparse
import json
import re
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.providers.embeddings.factory import get_embedder
from app.services.github.client import GitHubClient, GitHubGateway
from app.services.retrieval.search import Mode, search
from eval.labels import LabelError, is_relevant, resolve
from eval.metrics import Rank, bootstrap_ci, compare, mrr, recall_at_k
from eval.pipeline import indexed_snapshot

K = 10
SWEEP_WEIGHTS = (1.5, 2.0, 3.0)  # vector weight relative to keyword = 1
TYPES = ("identifier", "concept", "mixed")


@dataclass(frozen=True)
class Config:
    label: str
    mode: Mode
    embedder: Any
    symbol_boost: bool = True
    weights: dict[str, float] | None = None  # fusion weights; None means equal


def load_dataset(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    ids = [q["id"] for q in data["questions"]]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate question ids")
    return dict(data)


def _summarize(
    ranks: dict[str, Rank], types: dict[str, str], latencies: list[float]
) -> dict[str, Any]:
    ordered = list(ranks.values())
    by_type: dict[str, Any] = {}
    for t in TYPES:
        subset = [r for q, r in ranks.items() if types[q] == t]
        if subset:
            by_type[t] = {
                "n": len(subset),
                "recall@5": recall_at_k(subset, 5),
                "mrr": mrr(subset, K),
            }
    ms = sorted(x * 1000 for x in latencies)
    return {
        "recall@1": recall_at_k(ordered, 1),
        "recall@5": recall_at_k(ordered, 5),
        "recall@10": recall_at_k(ordered, 10),
        "mrr": mrr(ordered, K),
        "mrr_ci95": bootstrap_ci(ordered, lambda r: mrr(r, K)),
        "recall@5_ci95": bootstrap_ci(ordered, lambda r: recall_at_k(r, 5)),
        "by_type": by_type,
        "latency_ms": {
            "median": round(statistics.median(ms), 1),
            "p95": round(ms[min(len(ms) - 1, int(0.95 * len(ms)))], 1),
        },
    }


def run_evaluation(
    dataset: dict[str, Any],
    gh: GitHubGateway,
    settings: Settings,
    variants: bool = True,
    sweep: bool = False,
) -> dict[str, Any]:
    owner, name = dataset["repo"].split("/")
    questions = dataset["questions"]
    types = {q["id"]: q["type"] for q in questions}

    started = time.monotonic()
    with indexed_snapshot(owner, name, dataset["commit"], settings, gh, through="embed") as (
        db,
        sid,
        cursor,
    ):
        index_seconds = time.monotonic() - started
        targets = {q["id"]: resolve(db, sid, q["relevant"]) for q in questions}  # validates labels
        embedder = get_embedder(settings)
        embed_cursor: dict[str, Any] = cursor.get("embed", {})
        chunk_count = int(embed_cursor.get("total", 0))

        configs: list[Config] = [
            Config("keyword", "keyword", None),
            Config("keyword (text and path only)", "keyword", None, symbol_boost=False),
            Config("vector", "vector", embedder),
            Config("hybrid", "hybrid", embedder),
            Config("hybrid (text and path only)", "hybrid", embedder, symbol_boost=False),
        ]
        if sweep:
            configs += [
                Config(f"hybrid (vector weight {w:g})", "hybrid", embedder, weights={"vector": w})
                for w in SWEEP_WEIGHTS
            ]
        if (
            variants
            and settings.embedding_provider == "cloudflare"
            and settings.embedding_query_prefix
        ):
            bare = get_embedder(settings.model_copy(update={"embedding_query_prefix": ""}))
            configs += [
                Config("vector (no query prefix)", "vector", bare),
                Config("hybrid (no query prefix)", "hybrid", bare),
            ]

        results: dict[str, Any] = {}
        details: dict[str, dict[str, Any]] = {q["id"]: {} for q in questions}
        all_ranks: dict[str, dict[str, Rank]] = {}
        for cfg in configs:
            label = cfg.label
            ranks: dict[str, Rank] = {}
            latencies: list[float] = []
            for q in questions:
                t0 = time.monotonic()
                found = search(
                    db,
                    sid,
                    q["question"],
                    cfg.mode,
                    K,
                    cfg.embedder,
                    strict=True,
                    symbol_boost=cfg.symbol_boost,
                    weights=cfg.weights,
                )
                latencies.append(time.monotonic() - t0)
                rank: Rank = None
                for position, hit in enumerate(found.hits, start=1):
                    if is_relevant(hit.path, hit.start_line, hit.end_line, targets[q["id"]]):
                        rank = position
                        break
                ranks[q["id"]] = rank
                details[q["id"]][label] = {
                    "rank": rank,
                    "top": [f"{h.path}:{h.start_line}-{h.end_line}" for h in found.hits[:3]],
                }
            all_ranks[label] = ranks
            results[label] = _summarize(ranks, types, latencies)

        def pair(a: str, b: str) -> dict[str, int]:
            order = [q["id"] for q in questions]
            return compare([all_ranks[a][i] for i in order], [all_ranks[b][i] for i in order])

        paired = {
            "keyword vs keyword (text and path only)": pair(
                "keyword", "keyword (text and path only)"
            ),
            "hybrid vs keyword": pair("hybrid", "keyword"),
            "hybrid vs vector": pair("hybrid", "vector"),
            "vector vs keyword": pair("vector", "keyword"),
        }
        usage = dict(embedder.usage)

    return {
        "meta": {
            "dataset": dataset["name"],
            "repo": dataset["repo"],
            "commit": dataset["commit"],
            "date_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "embedding_model": embedder.model_id,
            "questions": len(questions),
            "question_types": {t: sum(1 for v in types.values() if v == t) for t in TYPES},
            "chunks": chunk_count,
            "k": K,
            "indexing_seconds": round(index_seconds, 1),
            "indexing_neurons": embed_cursor.get("neurons", 0.0),
            "query_embedding_usage": usage,
        },
        "results": results,
        "paired": paired,
        "questions": [
            {"id": q["id"], "type": q["type"], "question": q["question"], "modes": details[q["id"]]}
            for q in questions
        ],
    }


def render_markdown(report: dict[str, Any]) -> str:
    m = report["meta"]
    lines = [
        f"# Retrieval benchmark: {m['repo']}",
        "",
        f"- Commit `{m['commit'][:10]}`; {m['questions']} questions "
        f"({', '.join(f'{n} {t}' for t, n in m['question_types'].items())}); {m['chunks']} chunks.",
        f"- Embedding model: `{m['embedding_model']}`. Run at {m['date_utc']}.",
        f"- Indexing took {m['indexing_seconds']} s"
        + (f" and used {m['indexing_neurons']} neurons." if m["indexing_neurons"] else "."),
        "- A result counts as relevant when it overlaps a labelled symbol. "
        f"Top {m['k']} per question.",
        "",
        "| Method | Recall@1 | Recall@5 | Recall@10 | MRR | MRR 95% CI | median ms |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, r in report["results"].items():
        lo, hi = r["mrr_ci95"]
        lines.append(
            f"| {name} | {r['recall@1']:.2f} | {r['recall@5']:.2f} | {r['recall@10']:.2f} | "
            f"{r['mrr']:.3f} | {lo:.2f} to {hi:.2f} | {r['latency_ms']['median']} |"
        )
    lines += [
        "",
        "By question type (Recall@5 / MRR):",
        "",
        "| Method | " + " | ".join(TYPES) + " |",
        "| --- | " + " | ".join("---" for _ in TYPES) + " |",
    ]
    for name, r in report["results"].items():
        cells = [
            f"{r['by_type'][t]['recall@5']:.2f} / {r['by_type'][t]['mrr']:.2f}"
            if t in r["by_type"]
            else "-"
            for t in TYPES
        ]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "Question by question (the first method ranks the answer higher / lower / equal):",
        "",
    ]
    for name, p in report["paired"].items():
        lines.append(f"- {name}: {p['wins']} better, {p['losses']} worse, {p['ties']} tied")
    lines += [
        "",
        "With this many questions the confidence intervals are wide. Treat differences smaller "
        "than the interval as unproven, and the dataset as one repository's questions, not a "
        "general result.",
        "",
    ]
    return "\n".join(lines)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("eval/results"))
    parser.add_argument("--embedder", choices=["configured", "hash"], default="configured")
    parser.add_argument(
        "--pooling", choices=["cls", "mean"], help="override the Cloudflare pooling"
    )
    parser.add_argument("--no-variants", action="store_true", help="skip the query-prefix ablation")
    parser.add_argument("--sweep", action="store_true", help="also try other hybrid fusion weights")
    parser.add_argument("--check-labels", action="store_true", help="only verify the labels")
    args = parser.parse_args(argv)

    settings = get_settings()
    updates: dict[str, Any] = {}
    if args.embedder == "hash":
        updates["embedding_provider"] = "hash"
    if args.pooling:
        updates["cloudflare_pooling"] = args.pooling
    settings = settings.model_copy(update=updates)
    dataset = load_dataset(args.dataset)
    gh = GitHubClient(token=None)  # public repositories need no credentials

    if args.check_labels:
        owner, name = dataset["repo"].split("/")
        with indexed_snapshot(owner, name, dataset["commit"], settings, gh, through="parse") as (
            db,
            sid,
            _,
        ):
            try:
                for q in dataset["questions"]:
                    resolve(db, sid, q["relevant"])
            except LabelError as exc:
                print(f"BAD LABEL in {q['id']}: {exc}", file=sys.stderr)
                return 1
        count, sha = len(dataset["questions"]), dataset["commit"][:10]
        print(f"all {count} questions have valid labels at {sha}")
        return 0

    report = run_evaluation(dataset, gh, settings, variants=not args.no_variants, sweep=args.sweep)
    args.out.mkdir(parents=True, exist_ok=True)
    model = _slug(report["meta"]["embedding_model"])
    stem = f"{datetime.now(UTC):%Y-%m-%d}-{_slug(dataset['repo'])}-{model}"
    (args.out / f"{stem}.json").write_text(json.dumps(report, indent=2) + "\n")
    markdown = render_markdown(report)
    (args.out / f"{stem}.md").write_text(markdown)
    print(markdown)
    print(f"written: {args.out / stem}.json and .md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
