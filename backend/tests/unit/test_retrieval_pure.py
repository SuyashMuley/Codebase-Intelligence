import random
import re

import pytest

from app.services.retrieval.documents import document_text
from app.services.retrieval.fusion import RRF_K, reciprocal_rank_fusion
from app.services.retrieval.query import (
    MAX_NAMES,
    MAX_TERMS,
    STOPWORDS,
    query_terms,
    symbol_names,
    to_tsquery,
)


def test_stopwords_and_question_words_are_dropped() -> None:
    assert query_terms("How does the login process work?") == ["login", "process", "work"]
    assert query_terms("where is the session cookie set") == ["session", "cookie", "set"]


def test_a_question_made_only_of_glue_has_no_terms() -> None:
    assert query_terms("what is the") == []
    assert query_terms("") == [] and query_terms("   ?!  ") == []


def test_identifiers_are_split_at_punctuation_and_underscores() -> None:
    assert query_terms("create_session()") == ["create", "session"]
    assert query_terms("auth.sessions.py") == ["auth", "sessions", "py"]


def test_camel_case_gives_the_whole_word_and_its_parts() -> None:
    assert query_terms("sessionCookie") == ["sessioncookie", "session", "cookie"]
    assert query_terms("parseHTTPResponse")[0] == "parsehttpresponse"


def test_terms_are_lowercase_unique_and_in_order() -> None:
    assert query_terms("Cookie COOKIE cookie session Cookie") == ["cookie", "session"]


def test_the_number_of_terms_and_the_input_length_are_bounded() -> None:
    assert len(query_terms(" ".join(f"term{i}" for i in range(200)))) == MAX_TERMS
    assert query_terms("x" * 10_000)[0] == "x" * 500  # input is cut before it is processed


@pytest.mark.parametrize(
    "hostile",
    [
        "'; DROP TABLE chunks; --",
        "a & b | !c",
        "(foo) <-> bar",
        "foo:* bar:A",
        "\\ \" ' ` $ % ^ * ( ) { } [ ] < > ~",
        "a\x00b\nc\td",
        "日本語 ünïcode ☃",
        "||| &&& !!!",
    ],
)
def test_no_input_can_inject_tsquery_syntax(hostile: str) -> None:
    terms = query_terms(hostile)
    assert all(re.fullmatch(r"[a-z0-9]+", t) for t in terms)
    assert re.fullmatch(r"[a-z0-9]+( \| [a-z0-9]+)*|", to_tsquery(terms))


def test_the_tsquery_matches_any_term() -> None:
    assert to_tsquery(["a", "b", "c"]) == "a | b | c"
    assert to_tsquery([]) == ""


def test_stopwords_are_all_lowercase_words() -> None:
    assert all(w == w.lower() and w.isalpha() for w in STOPWORDS)


# --- fusion -----------------------------------------------------------------------------------


def ids(fused: list[tuple[int, float, dict[str, int]]]) -> list[int]:
    return [i for i, _s, _r in fused]


def test_fusion_of_one_list_keeps_its_order() -> None:
    assert ids(reciprocal_rank_fusion({"keyword": [5, 3, 9]})) == [5, 3, 9]


def test_an_item_both_methods_like_beats_items_only_one_likes() -> None:
    fused = reciprocal_rank_fusion({"keyword": [1, 2, 3], "vector": [9, 2, 8]})
    assert ids(fused)[0] == 2  # rank 2 in both beats rank 1 in just one
    assert fused[0][2] == {"keyword": 2, "vector": 2}


def test_scores_follow_the_rrf_formula() -> None:
    fused = dict((i, s) for i, s, _r in reciprocal_rank_fusion({"a": [7], "b": [1, 7]}))
    assert fused[7] == pytest.approx(1 / (RRF_K + 1) + 1 / (RRF_K + 2))
    assert fused[1] == pytest.approx(1 / (RRF_K + 1))


def test_weights_shift_the_balance() -> None:
    lists = {"keyword": [1, 2], "vector": [2, 1]}
    assert ids(reciprocal_rank_fusion(lists, weights={"keyword": 3.0}))[0] == 1
    assert ids(reciprocal_rank_fusion(lists, weights={"vector": 3.0}))[0] == 2


def test_ties_break_by_id_so_results_are_deterministic() -> None:
    fused = reciprocal_rank_fusion({"a": [10], "b": [4]})
    assert ids(fused) == [4, 10]
    assert ids(reciprocal_rank_fusion({"b": [4], "a": [10]})) == [4, 10]


def test_a_duplicate_inside_one_list_counts_once_at_its_best_rank() -> None:
    fused = reciprocal_rank_fusion({"a": [1, 2, 1]})
    assert fused[0][1] == pytest.approx(1 / (RRF_K + 1))
    assert len(fused) == 2


def test_empty_inputs() -> None:
    assert reciprocal_rank_fusion({}) == []
    assert reciprocal_rank_fusion({"a": [], "b": []}) == []


@pytest.mark.parametrize("seed", range(100))
def test_fusion_properties_hold_for_random_rankings(seed: int) -> None:
    rng = random.Random(seed)
    lists = {name: rng.sample(range(40), rng.randint(0, 25)) for name in ("keyword", "vector")}
    fused = reciprocal_rank_fusion(lists)
    assert set(ids(fused)) == set(lists["keyword"]) | set(
        lists["vector"]
    )  # nothing lost or invented
    scores = [s for _i, s, _r in fused]
    assert scores == sorted(scores, reverse=True)
    for item, _score, ranks in fused:
        for name, rank in ranks.items():
            assert lists[name][rank - 1] == item  # ranks point back at the real position


def test_documents_are_embedded_with_their_path_but_stored_text_is_untouched() -> None:
    assert document_text("pkg/a.py", "def f(): ...") == "File: pkg/a.py\ndef f(): ..."


# --- symbol names in a question ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Show me `guess_json_utf`.", ["guess_json_utf"]),
        (
            "Where is `HTTPDigestAuth.build_digest_header` implemented?",
            ["httpdigestauth", "build_digest_header"],
        ),
        ("What does `Response.raise_for_status` do?", ["response", "raise_for_status"]),
        ("What does `slugify` do?", ["slugify"]),  # a backticked plain word is taken
        ("iter_lines splitting a streamed response", ["iter_lines"]),
        ("ConnectTimeout and ReadTimeout exceptions", ["connecttimeout", "readtimeout"]),
        ("how is sessionCookie built", ["sessioncookie"]),
        ("REQUESTS_CA_BUNDLE environment variable", ["requests_ca_bundle"]),
    ],
)
def test_symbol_names_are_picked_out_of_a_question(question: str, expected: list[str]) -> None:
    assert symbol_names(question) == expected


@pytest.mark.parametrize(
    "question",
    [
        "How does the library decide whether to keep the Authorization header?",
        "timeout handling when the adapter sends a request",
        "What is the capital of France?",
        "slugify a title",  # a plain lowercase word is not assumed to be a symbol
        "",
        "```",
    ],
)
def test_ordinary_english_is_never_mistaken_for_a_symbol(question: str) -> None:
    assert symbol_names(question) == []


def test_symbol_names_are_deduplicated_capped_and_safe() -> None:
    assert symbol_names("`do_thing` and do_thing and `do_thing`") == ["do_thing"]
    many = " ".join(f"name_{i}" for i in range(50))
    assert len(symbol_names(many)) == MAX_NAMES
    for hostile in ("`'; DROP TABLE x; --`", "a_b'c d_e\"f", "x_y.z_w.v_u"):
        assert all(re.fullmatch(r"[a-z0-9_]+", n) for n in symbol_names(hostile))
