import pytest

from app.services.qa.classify import classify
from app.services.qa.context import (
    build_excerpts,
    build_user_message,
    new_nonce,
)
from app.services.qa.types import Excerpt
from app.services.retrieval.search import Hit


def hit(i: int, path: str = "a.py", start: int = 1, end: int = 5, text: str = "x = 1\n") -> Hit:
    return Hit(i, path, start, end, text, None, 0.0)


# --- classify -----------------------------------------------------------------------------------


def test_a_question_naming_code_is_an_identifier_or_mixed_question() -> None:
    assert classify("what does `parse_header_links` do").style == "identifier"
    assert (
        classify("how does the retry logic in `HTTPAdapter.send` interact with timeouts").style
        == "mixed"
    )
    assert classify("how are cookies persisted between requests").style == "concept"


def test_names_are_extracted_in_lowercase() -> None:
    assert "parse_header_links" in classify("what does `parse_header_links` do").names


def test_a_short_follow_up_borrows_the_previous_question_and_a_long_one_does_not() -> None:
    prev = "how does session creation work"
    assert classify("and its lifetime?", prev).search_query.startswith(prev)
    long_q = "how are cookies persisted between two separate requests today"
    assert classify(long_q, prev).search_query == long_q
    assert classify("and its lifetime?", None).search_query == "and its lifetime?"


def test_the_borrowed_context_is_bounded() -> None:
    q = classify("why?", "word " * 500).search_query
    assert len(q) <= 500


# --- assemble -----------------------------------------------------------------------------------


def test_excerpts_are_numbered_in_order_search_first_then_graph() -> None:
    out = build_excerpts([hit(1), hit(2, "b.py")], [hit(3, "c.py")], 8, 10_000)
    assert [(e.alias, e.chunk_id, e.via) for e in out] == [
        ("C1", 1, "search"),
        ("C2", 2, "search"),
        ("C3", 3, "graph"),
    ]


def test_a_chunk_is_not_shown_twice_nor_when_it_lies_inside_one_already_shown() -> None:
    out = build_excerpts(
        [
            hit(1, "a.py", 1, 20),
            hit(1, "a.py", 1, 20),
            hit(2, "a.py", 5, 10),
            hit(3, "a.py", 15, 30),
        ],
        [],
        8,
        10_000,
    )
    assert [e.chunk_id for e in out] == [1, 3]


def test_the_chunk_and_character_budgets_are_respected_but_one_excerpt_is_always_kept() -> None:
    big = "y" * 500
    assert len(build_excerpts([hit(i, f"{i}.py", text=big) for i in range(10)], [], 3, 10_000)) == 3
    kept = build_excerpts([hit(i, f"{i}.py", text=big) for i in range(10)], [], 8, 1_200)
    assert len(kept) == 2
    assert len(build_excerpts([hit(1, text=big)], [], 8, 10)) == 1


# --- the prompt ---------------------------------------------------------------------------------


def excerpt(alias: str, text: str) -> Excerpt:
    return Excerpt(alias, 1, "a.py", 3, 4, text, "f")


def test_the_question_is_outside_the_data_block_and_excerpts_inside() -> None:
    msg = build_user_message("why?", [], [excerpt("C1", "code here\n")], "abc123")
    assert msg.index("Question: why?") < msg.index("<<<EXCERPTS abc123") < msg.index("code here")
    assert msg.index("code here") < msg.index("abc123 EXCERPTS>>>")
    assert "[C1] a.py lines 3-4 (f)" in msg


def test_history_is_labelled_context_and_bounded() -> None:
    turns = [("user", f"q{i}") for i in range(10)] + [("assistant", "a" * 2000)]
    msg = build_user_message("now?", turns, [excerpt("C1", "c")], "n")
    assert "for context only" in msg
    assert msg.count("- user:") + msg.count("- assistant:") == 4
    assert "a" * 501 not in msg


def test_feedback_for_a_retry_is_appended_after_the_data_block() -> None:
    msg = build_user_message("q", [], [excerpt("C1", "c")], "n", ["Claim 1 cited 'C9'"])
    assert msg.index("n EXCERPTS>>>") < msg.index("previous answer had problems")
    assert "Claim 1 cited 'C9'" in msg


def test_each_nonce_is_fresh_and_unguessable_in_length() -> None:
    nonces = {new_nonce() for _ in range(50)}
    assert len(nonces) == 50 and all(len(n) == 12 for n in nonces)


def test_the_shipped_defaults_are_the_measured_ones() -> None:
    from app.core.config import Settings

    s = Settings(_env_file=None)
    # docs/PLAN.md (M7): similarity thresholds from eval/qa_eval.py on psf/requests and
    # pallets/click; neighbours off because they did not help; 6 excerpts matched 8.
    assert s.qa_min_similarity == 0.0
    assert s.qa_high_similarity == 0.80
    assert s.qa_expand_max == 0
    assert s.qa_context_chunks == 6
    assert s.chat_budget_seconds < 30  # the browser's proxy gives up at about 30 seconds


@pytest.mark.parametrize(
    "question",
    [
        "what is the repo about?",
        "What is this project for?",
        "give me an overview of the codebase",
        "what does this app do",
        "summarize this repository",
        "what is the tech stack?",
        "how is the project structured?",
    ],
)
def test_questions_about_the_whole_project_are_overview(question: str) -> None:
    assert classify(question).style == "overview"


@pytest.mark.parametrize(
    "question",
    [
        "What does `make_pass_decorator` do?",
        "where are the prompt templates?",
        "how does the parser handle errors",
    ],
)
def test_questions_about_code_are_not_overview(question: str) -> None:
    assert classify(question).style != "overview"
