import json
import re
from pathlib import Path
from typing import Any

import pytest

from eval.labels import Target, is_relevant
from eval.metrics import bootstrap_ci, compare, mrr, recall_at_k

DATASET = Path(__file__).resolve().parents[2] / "eval" / "datasets" / "requests.json"


# --- metrics ----------------------------------------------------------------------------------


def test_recall_counts_questions_answered_within_k() -> None:
    ranks = [1, 3, None, 6, 2]
    assert recall_at_k(ranks, 1) == pytest.approx(1 / 5)
    assert recall_at_k(ranks, 3) == pytest.approx(3 / 5)
    assert recall_at_k(ranks, 10) == pytest.approx(4 / 5)
    assert recall_at_k([], 5) == 0.0


def test_mrr_rewards_earlier_answers_and_zeroes_misses() -> None:
    assert mrr([1, 2, 4]) == pytest.approx((1 + 0.5 + 0.25) / 3)
    assert mrr([None, None]) == 0.0
    assert mrr([1, None]) == pytest.approx(0.5)
    assert mrr([11], cutoff=10) == 0.0  # beyond the cutoff counts as a miss
    assert mrr([]) == 0.0


def test_perfect_and_hopeless_systems_are_at_the_extremes() -> None:
    assert mrr([1] * 10) == 1.0 and recall_at_k([1] * 10, 1) == 1.0
    assert mrr([None] * 10) == 0.0 and recall_at_k([None] * 10, 10) == 0.0


def test_the_bootstrap_interval_contains_the_estimate_and_is_reproducible() -> None:
    ranks = [1, 1, 2, None, 3, 1, None, 5, 1, 2, 4, None, 1, 2, 1, 1, 3, None, 2, 1]
    low, high = bootstrap_ci(ranks, mrr)
    assert low <= mrr(ranks) <= high
    assert bootstrap_ci(ranks, mrr) == (low, high)  # seeded
    assert 0.0 <= low and high <= 1.0


def test_more_data_gives_a_narrower_interval() -> None:
    small = [1, None] * 5
    large = [1, None] * 100
    small_width = bootstrap_ci(small, mrr)[1] - bootstrap_ci(small, mrr)[0]
    large_width = bootstrap_ci(large, mrr)[1] - bootstrap_ci(large, mrr)[0]
    assert large_width < small_width


def test_an_all_same_result_has_a_zero_width_interval() -> None:
    assert bootstrap_ci([1] * 30, mrr) == (1.0, 1.0)
    assert bootstrap_ci([], mrr) == (0.0, 0.0)


def test_paired_comparison_counts_wins_losses_and_ties() -> None:
    # 1 beats 2 (win); 2 vs 2 (tie); a miss vs rank 3 (loss); rank 5 beats a miss (win)
    assert compare([1, 2, None, 5], [2, 2, 3, None]) == {"wins": 2, "losses": 1, "ties": 1}
    assert compare([3, None], [1, None]) == {"wins": 0, "losses": 1, "ties": 1}
    with pytest.raises(ValueError):
        compare([1], [1, 2])


# --- relevance --------------------------------------------------------------------------------


def test_a_chunk_is_relevant_when_it_overlaps_a_labelled_symbol() -> None:
    targets = [Target("a.py", ((10, 20),)), Target("b.py", ((5, 5), (40, 50)))]
    assert is_relevant("a.py", 15, 30, targets)  # overlaps the end
    assert is_relevant("a.py", 1, 10, targets)  # touches the first line
    assert is_relevant("a.py", 12, 14, targets)  # inside
    assert is_relevant("a.py", 1, 100, targets)  # contains it
    assert not is_relevant("a.py", 21, 30, targets)  # just after
    assert not is_relevant("a.py", 1, 9, targets)  # just before
    assert is_relevant("b.py", 45, 60, targets)  # any one of several ranges (overloads)
    assert not is_relevant("c.py", 10, 20, targets)  # right lines, wrong file


def test_a_whole_file_label_accepts_any_chunk_of_that_file() -> None:
    assert is_relevant("x.py", 1, 2, [Target("x.py", None)])
    assert not is_relevant("y.py", 1, 2, [Target("x.py", None)])


# --- the datasets themselves --------------------------------------------------------------------

# name -> (minimum questions, minimum per question type, required path prefix)
DATASETS = {
    "requests.json": (40, 10, "src/requests/"),
    "click.json": (20, 6, "src/click/"),
}
KINDS = ("identifier", "concept", "mixed")


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((DATASET.parent / name).read_text())
    return data


@pytest.mark.parametrize("name", list(DATASETS))
def test_each_dataset_is_well_formed_and_balanced(name: str) -> None:
    minimum, per_type, prefix = DATASETS[name]
    data = load(name)
    assert re.fullmatch(r"[0-9a-f]{40}", data["commit"])
    questions = data["questions"]
    assert len(questions) >= minimum
    ids = [q["id"] for q in questions]
    assert len(ids) == len(set(ids))
    assert {q["type"] for q in questions} == set(KINDS)
    for kind in KINDS:
        assert sum(1 for q in questions if q["type"] == kind) >= per_type
    for q in questions:
        assert q["question"].strip() and q["relevant"]
        for label in q["relevant"]:
            assert set(label) <= {"path", "symbol"} and label["path"].startswith(prefix)


@pytest.mark.parametrize("name", list(DATASETS))
def test_concept_questions_do_not_name_the_code_they_are_about(name: str) -> None:
    """The concept questions exist to test meaning, so they must not hand keyword search the
    answer. A backticked identifier or a snake_case/CamelCase name would."""
    for q in load(name)["questions"]:
        if q["type"] != "concept":
            continue
        assert "`" not in q["question"], q["id"]
        assert not re.search(r"\b[a-z]+_[a-z_]+\b", q["question"]), q["id"]
        assert not re.search(r"\b[A-Z][a-z]+[A-Z]\w*\b", q["question"]), q["id"]
        for label in q["relevant"]:
            part = label.get("symbol", "").split(".")[-1].strip("_")
            if len(part) > 12:  # a long identifier would be a giveaway
                assert part.lower() not in q["question"].lower(), q["id"]


@pytest.mark.parametrize("name", list(DATASETS))
def test_identifier_questions_do_name_what_they_ask_about(name: str) -> None:
    for q in load(name)["questions"]:
        if q["type"] == "identifier":
            assert "`" in q["question"], q["id"]


def test_search_defaults_to_vector_because_hybrid_did_not_beat_it_on_either_benchmark() -> None:
    from app.schemas.search import SearchRequest

    assert SearchRequest(query="x").mode == "vector"
