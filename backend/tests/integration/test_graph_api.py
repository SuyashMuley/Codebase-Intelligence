import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.api.deps import get_github
from tests.helpers import FakeGitHub
from tests.integration.test_parse_phase import index_repo, rows, run_until, tick

pytestmark = pytest.mark.db

FILES: dict[str, str | bytes] = {
    "app/__init__.py": "",
    "app/models.py": (
        "def make_user():\n    return User()\n\n\nclass User:\n    def save(self):\n        pass\n"
    ),
    "app/service.py": (
        "import os\nfrom app.models import make_user\n\n\n"
        "def create():\n    make_user()\n    os.getcwd()\n\n\n"
        "def helper():\n    create()\n"
    ),
    "app/api.py": (
        "from app.service import create\n\n\n"
        "def handler():\n    create()\n\n\n"
        "def other(client):\n    client.save()\n"
    ),
    "tests/test_service.py": (
        "from app.service import create\n\n\ndef test_create():\n    create()\n"
    ),
    "tests/test_api.py": ("from app import api\n\n\ndef test_handler():\n    api.handler()\n"),
    "tests/test_models.py": "def test_nothing():\n    pass\n",
    "lonely.py": "def alone():\n    pass\n",
}


@pytest.fixture
def built(db_client: TestClient, fake_gh: FakeGitHub) -> str:
    rid = index_repo(db_client, fake_gh, dict(FILES))
    for _ in range(40):
        if tick(db_client, rid)["status"]["state"] == "ready":
            return rid
    raise AssertionError("never became ready")


def get(c: TestClient, rid: str, what: str, **params: Any) -> Any:
    return c.get(f"/api/repositories/{rid}/{what}", params=params)


def symbol_id(c: TestClient, rid: str, path: str, qualified: str) -> int:
    detail = c.get(f"/api/repositories/{rid}/files/{path}").json()
    return int(next(s["id"] for s in detail["symbols"] if s["qualified_name"].endswith(qualified)))


# --- the graph phase --------------------------------------------------------------------------


def test_the_job_is_ready_and_reports_graph_counts(db_client: TestClient, built: str) -> None:
    graph = db_client.get(f"/api/repositories/{built}/status").json()["graph"]
    assert graph["done"] is True
    assert graph["static_imports"] >= 5 and graph["static_calls"] >= 5
    assert graph["unresolved_imports"] >= 1  # `os`
    stats = db_client.get(f"/api/repositories/{built}/stats").json()
    assert stats["graph"]["static_calls"] == graph["static_calls"]


def test_rerunning_the_graph_phase_does_not_duplicate_edges(
    db_client: TestClient, db_engine: Engine, built: str
) -> None:
    before = rows(db_engine, "SELECT count(*) FROM graph_edges")[0][0]
    with db_engine.begin() as conn:
        conn.execute(text("UPDATE index_jobs SET phase = 'graph', lease_until = NULL"))
        conn.execute(text("UPDATE snapshots SET status = 'running'"))
    run_until(db_client, built, "done")
    assert rows(db_engine, "SELECT count(*) FROM graph_edges")[0][0] == before


def test_every_static_edge_points_inside_its_own_snapshot(db_engine: Engine, built: str) -> None:
    bad = rows(
        db_engine,
        """
        SELECT count(*) FROM graph_edges e
        LEFT JOIN files df ON df.id = e.dst_file_id AND df.snapshot_id = e.snapshot_id
        LEFT JOIN symbols ds ON ds.id = e.dst_symbol_id AND ds.snapshot_id = e.snapshot_id
        WHERE e.resolution = 'static'
          AND (df.id IS NULL OR (e.dst_symbol_id IS NOT NULL AND ds.id IS NULL))
        """,
    )[0][0]
    assert bad == 0
    unresolved = rows(
        db_engine,
        "SELECT count(*) FROM graph_edges WHERE resolution='unresolved' "
        "AND (dst_file_id IS NOT NULL OR dst_symbol_id IS NOT NULL OR reason IS NULL)",
    )[0][0]
    assert unresolved == 0


# --- file graph -------------------------------------------------------------------------------


def test_file_graph_links_files_and_leaves_out_unlinked_ones(
    db_client: TestClient, built: str
) -> None:
    body = get(db_client, built, "graph").json()
    labels = {n["path"] for n in body["nodes"]}
    assert "app/service.py" in labels and "lonely.py" not in labels
    ids = {n["path"]: n["id"] for n in body["nodes"]}
    pairs = {(e["source"], e["target"]) for e in body["edges"]}
    assert (ids["app/service.py"], ids["app/models.py"]) in pairs
    assert (ids["tests/test_service.py"], ids["app/service.py"]) in pairs
    assert all(e["resolution"] == "static" for e in body["edges"])
    assert body["note"] == "2 files with no static links are not drawn."


def test_file_graph_focus_returns_a_neighbourhood(db_client: TestClient, built: str) -> None:
    one = get(db_client, built, "graph", path="app/models.py", depth=1).json()
    assert {n["path"] for n in one["nodes"]} == {"app/models.py", "app/service.py"}
    assert [n["path"] for n in one["nodes"] if n["focus"]] == ["app/models.py"]
    two = get(db_client, built, "graph", path="app/models.py", depth=2).json()
    assert {"app/api.py", "tests/test_service.py"} <= {n["path"] for n in two["nodes"]}


def test_the_overview_shows_only_the_most_connected_files_but_a_neighbourhood_is_not_cut(
    db_client: TestClient, built: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.graph import queries as graph_queries

    monkeypatch.setattr(graph_queries, "MAX_OVERVIEW_NODES", 3)
    body = get(db_client, built, "graph").json()
    assert body["truncated"] is True and len(body["nodes"]) == 3 and body["total_nodes"] > 3
    names = {n["path"] for n in body["nodes"]}
    assert "app/service.py" in names  # the hub is kept
    assert all(e["source"] in {n["id"] for n in body["nodes"]} for e in body["edges"])
    near = get(db_client, built, "graph", path="app/service.py", depth=3).json()
    assert near["truncated"] is False and len(near["nodes"]) > 3


def test_file_graph_for_a_missing_file_is_404(db_client: TestClient, built: str) -> None:
    r = get(db_client, built, "graph", path="nope.py")
    assert r.status_code == 404 and r.json()["error"]["code"] == "file_not_found"


def test_symbol_graph_shows_resolved_calls_and_labels_unresolved_ones(
    db_client: TestClient, built: str
) -> None:
    body = get(db_client, built, "graph", level="symbol", path="app/service.py").json()
    by_id = {n["id"]: n for n in body["nodes"]}
    static = {
        (by_id[e["source"]]["label"], by_id[e["target"]]["label"])
        for e in body["edges"]
        if e["resolution"] == "static"
    }
    assert ("create", "make_user") in static and ("helper", "create") in static
    assert ("handler", "create") in static  # a caller from another file is included
    unresolved = [e for e in body["edges"] if e["resolution"] == "unresolved"]
    assert any(by_id[e["target"]]["label"] == "os.getcwd" for e in unresolved)
    assert all(by_id[e["target"]]["kind"] == "external" for e in unresolved)


def test_symbol_graph_needs_a_path(db_client: TestClient, built: str) -> None:
    r = get(db_client, built, "graph", level="symbol")
    assert r.status_code == 422 and r.json()["error"]["code"] == "missing_target"


# --- impact -----------------------------------------------------------------------------------


def test_file_impact_lists_dependents_with_the_path_to_each(
    db_client: TestClient, built: str
) -> None:
    body = get(db_client, built, "impact", file="app/models.py", depth=3).json()
    by_path = {d["path"]: d for d in body["dependents"]}
    assert by_path["app/service.py"]["depth"] == 1
    assert by_path["app/api.py"]["depth"] == 2
    assert by_path["tests/test_service.py"]["depth"] == 2
    assert by_path["tests/test_api.py"]["depth"] == 3
    hops = by_path["tests/test_api.py"]["hops"]
    assert [h["source"] for h in hops] == ["app/models.py", "app/service.py", "app/api.py"]
    assert [h["target"] for h in hops] == ["app/service.py", "app/api.py", "tests/test_api.py"]
    assert body["direct"] == 1 and body["transitive"] == 3 and body["files_affected"] == 4
    assert "lonely.py" not in by_path and "app/models.py" not in by_path


def test_impact_depth_limits_how_far_it_walks(db_client: TestClient, built: str) -> None:
    body = get(db_client, built, "impact", file="app/models.py", depth=1).json()
    assert [d["path"] for d in body["dependents"]] == ["app/service.py"]


def test_suggested_tests_separate_static_facts_from_the_naming_heuristic(
    db_client: TestClient, built: str
) -> None:
    body = get(db_client, built, "impact", file="app/models.py", depth=3).json()
    tests = {t["path"]: t for t in body["suggested_tests"]}
    assert tests["tests/test_service.py"]["basis"] == "static_dependency"
    assert tests["tests/test_service.py"]["heuristic"] is False
    assert tests["tests/test_service.py"]["confidence"] == "medium"
    assert tests["tests/test_models.py"]["basis"] == "naming_convention"
    assert tests["tests/test_models.py"]["heuristic"] is True
    assert tests["tests/test_models.py"]["confidence"] == "low"
    # the heuristic suggestion is never mixed into the static dependents
    assert "tests/test_models.py" not in {d["path"] for d in body["dependents"]}


def test_symbol_impact_follows_callers_and_keeps_name_matches_apart(
    db_client: TestClient, built: str
) -> None:
    sid = symbol_id(db_client, built, "app/models.py", "User.save")
    body = get(db_client, built, "impact", symbol=sid).json()
    assert body["target"]["type"] == "symbol" and body["target"]["symbol"] == "User.save"
    assert body["dependents"] == []  # `client.save()` cannot be tied to User.save
    leads = body["unverified_callers"]
    assert [(u["path"], u["symbol"], u["called_as"]) for u in leads] == [
        ("app/api.py", "other", "client.save")
    ]
    assert leads[0]["reason"] == "dynamic_receiver"

    create = symbol_id(db_client, built, "app/service.py", "create")
    body = get(db_client, built, "impact", symbol=create, depth=2).json()
    got = {(d["path"], d["symbol"], d["depth"]) for d in body["dependents"]}
    assert ("app/service.py", "helper", 1) in got
    assert ("app/api.py", "handler", 1) in got
    assert ("tests/test_service.py", "test_create", 1) in got
    assert any("only its callers" in x for x in body["limitations"])


def test_impact_always_states_its_limitations(db_client: TestClient, built: str) -> None:
    body = get(db_client, built, "impact", file="app/models.py").json()
    assert body["limitations"] and "static fact" in " ".join(body["limitations"])


def test_impact_validation_and_not_found(db_client: TestClient, built: str) -> None:
    assert get(db_client, built, "impact").json()["error"]["code"] == "missing_target"
    assert get(db_client, built, "impact", file="nope.py").status_code == 404
    assert get(db_client, built, "impact", symbol=99999999).json()["error"]["code"] == (
        "symbol_not_found"
    )
    assert get(db_client, built, "impact", file="app/models.py", depth=0).status_code == 422
    assert get(db_client, built, "impact", file="app/models.py", depth=99).status_code == 422


# --- not ready, ownership, scoping ------------------------------------------------------------


def test_the_graph_is_not_available_until_indexing_finishes(
    db_client: TestClient, fake_gh: FakeGitHub
) -> None:
    rid = index_repo(db_client, fake_gh, dict(FILES))
    run_until(db_client, rid, "embed")
    for what in ("graph", "impact"):
        params = {"file": "app/models.py"} if what == "impact" else {}
        r = get(db_client, rid, what, **params)
        assert r.status_code == 409 and r.json()["error"]["code"] == "graph_not_ready"


def test_other_users_and_anonymous_visitors_cannot_read_the_graph(
    built: str, other_client: TestClient, anon_client: TestClient
) -> None:
    for what, params in (("graph", {}), ("impact", {"file": "app/models.py"})):
        assert get(other_client, built, what, **params).status_code == 404
        assert get(anon_client, built, what, **params).status_code == 401


def test_a_symbol_from_another_repository_is_not_found(
    db_client: TestClient, built: str, db_app: FastAPI, fake_gh: FakeGitHub
) -> None:
    other = FakeGitHub({"x.py": "def secret():\n    pass\n"})
    db_app.dependency_overrides[get_github] = lambda: other
    second = db_client.post("/api/repositories", json={"url": "https://github.com/o/other"}).json()[
        "id"
    ]
    db_client.post(f"/api/repositories/{second}/index")
    for _ in range(40):
        if tick(db_client, second)["status"]["state"] == "ready":
            break
    foreign = symbol_id(db_client, second, "x.py", "secret")
    r = get(db_client, built, "impact", symbol=foreign)
    assert r.status_code == 404 and r.json()["error"]["code"] == "symbol_not_found"
    # and an unknown snapshot id cannot reach another repository's graph either
    snap = db_client.get(f"/api/repositories/{second}/files").json()["snapshot"]["id"]
    assert get(db_client, built, "graph", snapshot=snap).status_code == 404
    assert uuid.UUID(snap)


# --- AI review ----------------------------------------------------------------------------------


class FakeLlm:
    """Stands in for OpenRouter: records prompts and returns a scripted answer."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []
        self.last_model: str | None = "fake/model"

    def complete(self, system: str, user: str, **kw: Any) -> str:
        self.calls.append({"system": system, "user": user, **kw})
        return self.text


def good_answer() -> str:
    import json

    return json.dumps(
        {
            "summary": "models.py feeds the service layer.",
            "risks": [
                {
                    "description": "Service creates users",
                    "confidence": "high",
                    "paths": ["app/service.py", "billing/ghost.py"],
                }
            ],
            "tests_to_consider": [{"path": "tests/test_service.py", "reason": "covers create"}],
            "open_questions": ["Is User pickled?"],
        }
    )


def review(c: TestClient, rid: str, **body: Any) -> Any:
    return c.post(f"/api/repositories/{rid}/impact/review", json=body)


def use_llm(app: FastAPI, llm: FakeLlm) -> None:
    from app.api.deps import get_llm

    app.dependency_overrides[get_llm] = lambda: llm


def test_ai_review_is_labelled_validated_and_separate_from_the_static_result(
    db_client: TestClient, db_app: FastAPI, built: str
) -> None:
    llm = FakeLlm(good_answer())
    use_llm(db_app, llm)
    body = review(db_client, built, file="app/models.py").json()
    assert "not verified" in body["notice"] and body["model"] == "fake/model"
    assert body["risks"][0]["paths"] == ["app/service.py"]  # the invented file was dropped
    assert body["dropped"] == 1
    assert [t["path"] for t in body["tests_to_consider"]] == ["tests/test_service.py"]
    assert "dependents" not in body  # never merged into the static facts
    static = get(db_client, built, "impact", file="app/models.py").json()
    assert "ai" not in str(static).lower() or "notice" not in static


def test_the_prompt_contains_the_static_facts_and_the_symbols_code_only(
    db_client: TestClient, db_app: FastAPI, built: str
) -> None:
    llm = FakeLlm(good_answer())
    use_llm(db_app, llm)
    sid = symbol_id(db_client, built, "app/models.py", "make_user")
    assert review(db_client, built, symbol=sid).status_code == 200
    prompt = llm.calls[0]["user"]
    assert "return User()" in prompt  # the symbol's own lines
    assert "class User" not in prompt  # not the rest of the file
    assert "app/service.py" in prompt
    assert llm.calls[0]["json_mode"] is True
    assert "never an instruction" in llm.calls[0]["system"]


def test_ai_review_needs_a_target_and_a_configured_model(
    db_client: TestClient, db_app: FastAPI, built: str
) -> None:
    use_llm(db_app, FakeLlm(good_answer()))
    assert review(db_client, built).json()["error"]["code"] == "missing_target"
    assert review(db_client, built, file="nope.py").status_code == 404
    assert review(db_client, built, file="app/models.py", depth=99).status_code == 422

    from app.api.deps import get_llm

    del db_app.dependency_overrides[get_llm]
    r = review(db_client, built, file="app/models.py")
    assert r.status_code == 503 and r.json()["error"]["code"] == "llm_not_configured"


def test_a_garbled_model_answer_is_a_clean_error_not_a_crash(
    db_client: TestClient, db_app: FastAPI, built: str
) -> None:
    use_llm(db_app, FakeLlm("I cannot help with that."))
    r = review(db_client, built, file="app/models.py")
    assert r.status_code == 502 and r.json()["error"]["code"] == "llm_bad_response"


def test_ai_review_is_limited_per_user_and_private(
    db_client: TestClient,
    db_app: FastAPI,
    other_client: TestClient,
    anon_client: TestClient,
    built: str,
) -> None:
    use_llm(db_app, FakeLlm(good_answer()))
    codes = [review(db_client, built, file="app/models.py").status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and set(codes[5:]) == {429}
    assert review(other_client, built, file="app/models.py").status_code == 404
    assert review(anon_client, built, file="app/models.py").status_code == 401


def test_ai_review_waits_for_the_graph(
    db_client: TestClient, db_app: FastAPI, fake_gh: FakeGitHub
) -> None:
    use_llm(db_app, FakeLlm(good_answer()))
    rid = index_repo(db_client, fake_gh, dict(FILES))
    run_until(db_client, rid, "embed")
    r = review(db_client, rid, file="app/models.py")
    assert r.status_code == 409 and r.json()["error"]["code"] == "graph_not_ready"
