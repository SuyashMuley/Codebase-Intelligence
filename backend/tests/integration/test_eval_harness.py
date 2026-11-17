from typing import Any

import pytest
from sqlalchemy import Engine, text

from app.core.config import get_settings
from eval.labels import LabelError
from eval.run_eval import render_markdown, run_evaluation
from tests.helpers import FakeGitHub
from tests.integration.test_search import FILES

pytestmark = pytest.mark.db


def dataset(questions: list[dict[str, Any]]) -> dict[str, Any]:
    return {"name": "synthetic", "repo": "o/r", "commit": "a" * 40, "questions": questions}


def q(qid: str, kind: str, question: str, *labels: dict[str, str]) -> dict[str, Any]:
    return {"id": qid, "type": kind, "question": question, "relevant": list(labels)}


QUESTIONS = [
    q("q1", "identifier", "Where is `create_session` defined?",
      {"path": "auth/sessions.py", "symbol": "create_session"}),
    q("q2", "identifier", "What does `slugify` do?",
      {"path": "utils/strings.py", "symbol": "slugify"}),
    q("q3", "concept", "How is the invoice total computed with tax?",
      {"path": "billing/invoices.py", "symbol": "compute_invoice_total"}),
    q("q4", "mixed", "ListenAndServe on the server", {"path": "cmd/server/main.go"}),
]  # fmt: skip


def leftovers(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM repositories WHERE owner LIKE 'eval-%'")
            ).scalar_one()
        )


def gateway() -> FakeGitHub:
    return FakeGitHub(dict(FILES))


def test_the_harness_runs_end_to_end_and_reports_every_method(db_clean: Engine) -> None:
    report = run_evaluation(dataset(QUESTIONS), gateway(), get_settings())
    # The offline embedder has no query prefix, so the only extra rows are the symbol-signal
    # ablations.
    assert set(report["results"]) == {
        "keyword",
        "keyword (text and path only)",
        "vector",
        "hybrid",
        "hybrid (text and path only)",
    }  # no query-prefix variants for hash
    meta = report["meta"]
    assert meta["questions"] == 4 and meta["question_types"] == {
        "identifier": 2,
        "concept": 1,
        "mixed": 1,
    }
    assert meta["embedding_model"] == "hash-384-v1" and meta["chunks"] == 6
    for name, r in report["results"].items():
        assert 0.0 <= r["recall@1"] <= r["recall@5"] <= r["recall@10"] <= 1.0, name
        assert r["mrr_ci95"][0] <= r["mrr"] <= r["mrr_ci95"][1], name
        assert set(r["by_type"]) == {"identifier", "concept", "mixed"}
        assert r["latency_ms"]["median"] >= 0
    # Exact identifiers are keyword search's home ground.
    assert report["results"]["keyword"]["by_type"]["identifier"]["recall@5"] == 1.0
    assert report["results"]["keyword"]["mrr"] > 0.5
    assert set(report["paired"]) == {
        "keyword vs keyword (text and path only)",
        "hybrid vs keyword",
        "hybrid vs vector",
        "vector vs keyword",
    }
    for pair in report["paired"].values():
        assert sum(pair.values()) == 4
    assert [x["id"] for x in report["questions"]] == ["q1", "q2", "q3", "q4"]
    assert report["questions"][0]["modes"]["keyword"]["rank"] == 1


def test_the_temporary_repository_is_removed_afterwards(db_clean: Engine) -> None:
    run_evaluation(dataset(QUESTIONS), gateway(), get_settings())
    assert leftovers(db_clean) == 0
    with db_clean.connect() as conn:
        for table in ("snapshots", "files", "symbols", "chunks", "code_refs"):
            assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0, table


def test_a_label_that_matches_nothing_fails_loudly_and_still_cleans_up(db_clean: Engine) -> None:
    stale = [
        q("q1", "identifier", "Where is `gone`?", {"path": "auth/sessions.py", "symbol": "gone"})
    ]
    with pytest.raises(LabelError, match="symbol not found"):
        run_evaluation(dataset(stale), gateway(), get_settings())
    assert leftovers(db_clean) == 0
    missing_file = [q("q1", "mixed", "x", {"path": "nope/file.py"})]
    with pytest.raises(LabelError, match="file not found"):
        run_evaluation(dataset(missing_file), gateway(), get_settings())
    assert leftovers(db_clean) == 0


def test_an_unanswerable_question_scores_as_a_miss_not_an_error(db_clean: Engine) -> None:
    hard = [
        q("q1", "concept", "zzzz qqqq", {"path": "auth/sessions.py", "symbol": "create_session"})
    ]
    report = run_evaluation(dataset(hard), gateway(), get_settings())
    assert (
        report["results"]["keyword"]["recall@10"] == 0.0
        and report["results"]["keyword"]["mrr"] == 0.0
    )
    assert report["questions"][0]["modes"]["keyword"]["rank"] is None


def test_the_markdown_summary_has_the_tables_and_the_honesty_note(db_clean: Engine) -> None:
    report = run_evaluation(dataset(QUESTIONS), gateway(), get_settings())
    md = render_markdown(report)
    for expected in (
        "# Retrieval benchmark: o/r",
        "| keyword |",
        "| hybrid |",
        "MRR 95% CI",
        "By question type",
        "hybrid vs keyword:",
        "confidence intervals are wide",
    ):
        assert expected in md
    assert "hash-384-v1" in md


def test_a_failed_indexing_run_is_reported_and_cleaned_up(db_clean: Engine) -> None:
    from app.core.errors import GithubUnavailable
    from eval.pipeline import IndexingFailed

    broken = gateway()
    broken.fail_next = [GithubUnavailable("down")] * 5
    with pytest.raises(IndexingFailed):
        run_evaluation(dataset(QUESTIONS), broken, get_settings())
    assert leftovers(db_clean) == 0
