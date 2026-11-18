"""Run the whole question-answering pipeline with the real embedding service and the real
language model on a sample of labelled questions, and report how the answers hold up.

This spends real model requests (a free key allows about 50 a day), so it samples and stops
politely when the service says it is out of allowance.

    uv run python -m eval.qa_live --dataset eval/datasets/requests.json --per-type 4
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.db.models import Snapshot
from app.providers.embeddings.factory import get_embedder
from app.providers.llm.factory import get_llm
from app.services.github.client import GitHubClient
from app.services.qa.graph import QADeps, run
from app.services.retrieval.search import _has_embeddings
from eval.labels import is_relevant, resolve
from eval.pipeline import indexed_snapshot

HERE = Path(__file__).parent
PAUSE_SECONDS = 4.0  # stay well under the free per-minute limit


def sample(questions: list[dict[str, Any]], per_type: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for kind in ("identifier", "concept", "mixed"):
        out += [q for q in questions if q["type"] == kind][:per_type]
    return out


def evaluate(dataset: dict[str, Any], settings: Settings, per_type: int) -> dict[str, Any]:
    owner, name = dataset["repo"].split("/")
    llm = get_llm(settings)
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []  # questions the model service failed on
    stopped: str | None = None
    with indexed_snapshot(
        owner, name, dataset["commit"], settings, GitHubClient(token=None), through="graph"
    ) as (db, sid, _cursor):
        snapshot = db.get(Snapshot, sid)
        assert snapshot is not None
        embedder = get_embedder(settings)
        deps = QADeps(
            db=db,
            settings=settings,
            snapshot=snapshot,
            embedder=embedder,
            llm=llm,
            reserve_quota=lambda: None,
            refund_quota=lambda: None,
        )
        assert _has_embeddings(db, sid)
        for q in sample(dataset["questions"], per_type):
            targets = resolve(db, sid, q["relevant"])
            started = time.monotonic()
            try:
                result = run(deps, q["question"], [])
            except AppError as exc:
                failures.append({"id": q["id"], "code": exc.code})
                if (
                    exc.code == "llm_rate_limited"
                    and sum(1 for f in failures if f["code"] == "llm_rate_limited") >= 3
                ):
                    stopped = "the service kept rate limiting; stopped to avoid wasting requests"
                    break
                time.sleep(PAUSE_SECONDS)
                continue
            rows.append(
                {
                    "id": q["id"],
                    "type": q["type"],
                    "question": q["question"],
                    "seconds": round(time.monotonic() - started, 1),
                    "declined": result.insufficient_evidence,
                    "reason": result.reason,
                    "attempts": result.attempts,
                    "claims": len(result.claims),
                    "dropped": result.dropped_claims,
                    "confidence": result.confidence,
                    "model": result.model,
                    "cites_labelled_code": any(
                        is_relevant(c.path, c.start_line, c.end_line, targets)
                        for c in result.citations
                    ),
                    "answer": [
                        {
                            "text": c.text,
                            "at": [
                                f"{x.path}:{x.start_line}-{x.end_line}"
                                for x in result.citations
                                if x.n in c.cites
                            ],
                        }
                        for c in result.claims
                    ],
                    "missing": result.missing,
                }
            )
            time.sleep(PAUSE_SECONDS)
    answered = [r for r in rows if not r["declined"]]
    return {
        "repo": dataset["repo"],
        "commit": dataset["commit"],
        "questions_attempted": len(rows) + len(failures),
        "questions_answered_or_declined": len(rows),
        "model_service_failures": failures,
        "stopped_early": stopped,
        "declined": len(rows) - len(answered),
        "answered": len(answered),
        "first_attempt_clean": sum(1 for r in answered if r["attempts"] == 1 and r["dropped"] == 0),
        "needed_retry": sum(1 for r in rows if r["attempts"] > 1),
        "claims_dropped": sum(r["dropped"] for r in rows),
        "claims_kept": sum(r["claims"] for r in rows),
        "answers_citing_labelled_code": sum(1 for r in answered if r["cites_labelled_code"]),
        "median_seconds": round(statistics.median(r["seconds"] for r in rows), 1) if rows else None,
        "models": sorted({r["model"] for r in rows if r["model"]}),
        "by_confidence": {
            c: sum(1 for r in rows if r["confidence"] == c)
            for c in ("none", "low", "medium", "high")
        },
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", required=True)
    p.add_argument("--per-type", type=int, default=4)
    p.add_argument("--out", default=str(HERE / "results"))
    args = p.parse_args(argv)
    dataset = json.loads(Path(args.dataset).read_text())
    result = evaluate(dataset, get_settings(), args.per_type)
    out = Path(args.out)
    out.mkdir(exist_ok=True)
    path = out / f"qa_live_{dataset['repo'].replace('/', '_')}.json"
    path.write_text(json.dumps(result, indent=2))
    summary = {k: v for k, v in result.items() if k != "rows"}
    print(json.dumps(summary, indent=2))
    print(f"\nwritten: {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
