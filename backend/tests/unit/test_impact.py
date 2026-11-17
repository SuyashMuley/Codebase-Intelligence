import pytest

from app.services.graph import builder as builder_module
from app.services.graph.impact import is_test_path, path_to, reverse_bfs, suggest_tests
from tests.graph_helpers import build_graph

# --- reverse_bfs ------------------------------------------------------------------------------


def chain() -> dict[str, list[tuple[str, str]]]:
    # a <- b <- c <- d  (b depends on a, c on b, d on c)
    return {
        "a": [("b", "imports")],
        "b": [("c", "calls")],
        "c": [("d", "imports")],
    }


def test_bfs_reports_depth_and_the_path_back() -> None:
    reached, truncated = reverse_bfs(["a"], chain(), max_depth=5)
    assert {k: v.depth for k, v in reached.items()} == {"b": 1, "c": 2, "d": 3}
    assert not truncated
    assert path_to("d", reached, ["a"]) == [
        ("a", "b", "imports"),
        ("b", "c", "calls"),
        ("c", "d", "imports"),
    ]


def test_bfs_stops_at_max_depth() -> None:
    reached, _ = reverse_bfs(["a"], chain(), max_depth=2)
    assert set(reached) == {"b", "c"}


def test_bfs_survives_cycles_and_reaches_each_node_once_at_its_shortest_depth() -> None:
    graph = {
        "a": [("b", "imports"), ("c", "imports")],
        "b": [("a", "imports"), ("c", "imports")],
        "c": [("a", "imports"), ("b", "imports")],
    }
    reached, _ = reverse_bfs(["a"], graph, max_depth=10)
    assert {k: v.depth for k, v in reached.items()} == {"b": 1, "c": 1}  # never "a" itself


def test_bfs_caps_the_number_of_nodes() -> None:
    graph = {"root": [(f"n{i}", "imports") for i in range(50)]}
    reached, truncated = reverse_bfs(["root"], graph, max_depth=3, max_nodes=10)
    assert len(reached) == 10 and truncated


def test_bfs_is_deterministic() -> None:
    graph = {"a": [("z", "imports"), ("m", "calls"), ("b", "imports")]}
    first, _ = reverse_bfs(["a"], graph, 2)
    second, _ = reverse_bfs(["a"], dict(graph), 2)
    assert list(first) == list(second) == ["b", "m", "z"]


# --- tests ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_x.py",
        "pkg/test_x.py",
        "pkg/x_test.py",
        "src/x.test.ts",
        "src/x.spec.tsx",
        "src/__tests__/x.ts",
        "pkg/x_test.go",
        "test/app.js",
    ],
)
def test_test_files_are_recognised(path: str) -> None:
    assert is_test_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "src/x.py",
        "src/contest.py",
        "src/latest.ts",
        "pkg/x.go",
        "tests/conftest.py",
        "a/testing.py",
    ],
)
def test_ordinary_files_are_not_tests(path: str) -> None:
    # "testing.py" is not under a test directory and does not follow a test naming pattern
    assert not is_test_path(path)


def test_static_dependent_tests_come_first_and_are_not_heuristic() -> None:
    out = suggest_tests(
        ["app/models.py"],
        {"app/service.py": 1, "tests/test_service.py": 2, "tests/test_direct.py": 1},
        ["app/models.py", "app/service.py", "tests/test_service.py", "tests/test_direct.py"],
    )
    assert [(s.path, s.confidence, s.heuristic) for s in out] == [
        ("tests/test_direct.py", "high", False),
        ("tests/test_service.py", "medium", False),
    ]


def test_naming_convention_adds_low_confidence_heuristic_suggestions_only() -> None:
    out = suggest_tests(
        ["app/models.py", "web/router.ts", "pkg/util.go"],
        {},
        [
            "tests/test_models.py",
            "web/router.test.ts",
            "pkg/util_test.go",
            "tests/test_other.py",
            "app/models.py",
        ],
    )
    assert {s.path for s in out} == {
        "tests/test_models.py",
        "web/router.test.ts",
        "pkg/util_test.go",
    }
    assert all(
        s.heuristic and s.confidence == "low" and s.basis == "naming_convention" for s in out
    )


def test_a_static_suggestion_is_not_duplicated_by_the_naming_rule() -> None:
    out = suggest_tests(
        ["app/models.py"], {"tests/test_models.py": 1}, ["tests/test_models.py", "app/models.py"]
    )
    assert [(s.path, s.basis) for s in out] == [("tests/test_models.py", "static_dependency")]


def test_init_files_do_not_match_every_test() -> None:
    out = suggest_tests(["app/__init__.py"], {}, ["tests/test___init__.py", "tests/test_x.py"])
    assert out == []


# --- the edge cap -----------------------------------------------------------------------------


def test_the_edge_cap_drops_unresolved_edges_before_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    sources = {
        "lib.py": "def f(): pass\n",
        "use.py": "from lib import f\n\ndef go():\n    f()\n    a()\n    b()\n    c()\n    d()\n",
    }
    full = build_graph(sources)
    statics = [e for e in full.result.edges if e.resolution == "static"]
    assert len(statics) == 2 and not full.result.truncated
    monkeypatch.setattr(builder_module, "MAX_EDGES", 3)
    capped = build_graph(sources)
    assert capped.result.truncated
    assert len(capped.result.edges) == 3
    assert [e for e in capped.result.edges if e.resolution == "static"] == statics
