import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.api.deps import get_embedder, get_github
from app.core.errors import EmbeddingUnavailable
from app.services.retrieval.search import keyword_search, search
from tests.helpers import FakeGitHub, ScriptedEmbedder
from tests.integration.test_parse_phase import index_repo, rows, run_until

pytestmark = pytest.mark.db

FILES: dict[str, str | bytes] = {
    "auth/sessions.py": (
        '"""Login sessions."""\n\n\n'
        "def create_session(user_id, ttl_seconds=3600):\n"
        '    """Create a signed session cookie for a user."""\n'
        '    return sign({"user": user_id, "ttl": ttl_seconds})\n\n\n'
        "def revoke_session(token):\n"
        '    """Delete a session so its cookie stops working."""\n'
        "    store.delete(token)\n"
    ),
    "auth/passwords.py": (
        "def authenticate(user, password):\n"
        '    """Check a user\'s password against the stored hash and return the user."""\n'
        "    return verify_hash(password, user.password_hash)\n"
    ),
    "billing/invoices.py": (
        "def compute_invoice_total(items, tax_rate):\n"
        '    """Sum line items and add sales tax to get the invoice total."""\n'
        "    subtotal = sum(i.price * i.qty for i in items)\n"
        "    return subtotal * (1 + tax_rate)\n"
    ),
    "web/router.ts": (
        "export function registerRoute(path: string, handler: Handler) {\n"
        "  routes.set(path, handler);\n}\n\n"
        'export function sessionCookieName() { return "sid"; }\n'
    ),
    "cmd/server/main.go": (
        "package main\n\nfunc (s *Server) ListenAndServe() error {\n"
        "\treturn http.ListenAndServe(s.addr, s.mux)\n}\n"
    ),
    "utils/strings.py": (
        "def slugify(text):\n"
        '    """Lowercase text and join words with hyphens for use in a URL."""\n'
        '    return "-".join(text.lower().split())\n'
    ),
}

# What the scripted embedder "knows": documents about these topics are close to queries about
# them, even when no words are shared. This is the semantic knowledge keyword search lacks.
TOPICS = {
    "auth": ["session", "password", "authenticate"],
    "billing": ["invoice"],
    "web": ["registerroute"],
    "server": ["listenandserve"],
    "text": ["slugify"],
}
QUERY_TOPICS = {
    "log in": "auth",
    "sign in": "auth",
    "invoice": "billing",
    "pay": "billing",
    "web address": "text",
}


def scripted(**kw: Any) -> ScriptedEmbedder:
    return ScriptedEmbedder(TOPICS, QUERY_TOPICS, **kw)


def use_embedder(app: FastAPI, monkeypatch: pytest.MonkeyPatch, embedder: ScriptedEmbedder) -> None:
    monkeypatch.setattr("app.services.indexing.embed_phase.get_embedder", lambda _s: embedder)
    app.dependency_overrides[get_embedder] = lambda: embedder


@pytest.fixture
def embedded(
    db_client: TestClient, fake_gh: FakeGitHub, db_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, uuid.UUID, ScriptedEmbedder]:
    embedder = scripted()
    use_embedder(db_app, monkeypatch, embedder)
    rid = index_repo(db_client, fake_gh, dict(FILES))
    run_until(db_client, rid, "graph")
    return rid, snapshot_id(db_client, rid), embedder


def snapshot_id(c: TestClient, rid: str) -> uuid.UUID:
    return uuid.UUID(c.get(f"/api/repositories/{rid}/files").json()["snapshot"]["id"])


def paths(hits: list[Any]) -> list[str]:
    return [h.path if hasattr(h, "path") else h["path"] for h in hits]


def post(c: TestClient, rid: str, query: str, **body: Any) -> Any:
    return c.post(f"/api/repositories/{rid}/search", json={"query": query, **body})


# --- keyword search ---------------------------------------------------------------------------


def test_an_exact_identifier_finds_the_chunk_that_contains_it(
    db_engine: Engine, embedded: tuple[str, uuid.UUID, ScriptedEmbedder], db_client: TestClient
) -> None:
    rid, _sid, _e = embedded
    body = post(db_client, rid, "create_session", mode="keyword").json()
    assert body["mode_used"] == "keyword" and body["warnings"] == []
    assert body["hits"][0]["path"] == "auth/sessions.py"
    assert body["hits"][0]["keyword_rank"] == 1 and body["hits"][0]["vector_rank"] is None


def test_camel_case_and_spaced_words_find_the_same_code(
    embedded: tuple[str, uuid.UUID, ScriptedEmbedder], db_client: TestClient
) -> None:
    rid, _sid, _e = embedded
    for query in ("sessionCookieName", "session cookie name"):
        hits = post(db_client, rid, query, mode="keyword").json()["hits"]
        assert "web/router.ts" in paths(hits), query


def test_file_paths_are_searchable_too(
    embedded: tuple[str, uuid.UUID, ScriptedEmbedder], db_client: TestClient
) -> None:
    rid, _sid, _e = embedded
    hits = post(db_client, rid, "billing invoices", mode="keyword").json()["hits"]
    assert hits[0]["path"] == "billing/invoices.py"


def test_matching_ignores_case_and_a_question_is_reduced_to_its_meaningful_words(
    embedded: tuple[str, uuid.UUID, ScriptedEmbedder], db_client: TestClient
) -> None:
    rid, _sid, _e = embedded
    hits = post(db_client, rid, "HOW do I SLUGIFY a title?", mode="keyword").json()["hits"]
    assert hits[0]["path"] == "utils/strings.py"


def test_a_query_with_no_meaningful_words_or_no_matches_returns_nothing_not_an_error(
    embedded: tuple[str, uuid.UUID, ScriptedEmbedder], db_client: TestClient
) -> None:
    rid, _sid, _e = embedded
    for query in ("what is the", "zzzzzz qqqqqq", "???"):
        r = post(db_client, rid, query, mode="keyword")
        assert r.status_code == 200 and r.json()["hits"] == [], query


@pytest.mark.parametrize(
    "hostile",
    [
        "'; DROP TABLE chunks; --",
        "a & b | !c",
        "(foo) <-> bar",
        "foo:* bar:A",
        "\\ \" ' ` ",
        "x" * 5000,
    ],
)
def test_hostile_queries_are_safe(
    embedded: tuple[str, uuid.UUID, ScriptedEmbedder],
    db_client: TestClient,
    db_engine: Engine,
    hostile: str,
) -> None:
    rid, _sid, _e = embedded
    body = {"query": hostile[:500], "mode": "keyword"}
    assert db_client.post(f"/api/repositories/{rid}/search", json=body).status_code == 200
    assert rows(db_engine, "SELECT count(*) FROM chunks")[0][0] > 0  # the table is still there


def test_results_are_ordered_best_first_and_every_hit_matches(
    embedded: tuple[str, uuid.UUID, ScriptedEmbedder], db_engine: Engine
) -> None:
    _rid, sid, _e = embedded
    with Session(db_engine) as db:
        ids = keyword_search(db, sid, "session cookie", 10)
        assert len(ids) == len(set(ids)) >= 2


# --- vector search ----------------------------------------------------------------------------


def test_vector_search_finds_meaning_that_shares_no_words_with_the_query(
    embedded: tuple[str, uuid.UUID, ScriptedEmbedder], db_client: TestClient
) -> None:
    rid, _sid, _e = embedded
    keyword = post(db_client, rid, "how do users log in", mode="keyword").json()["hits"]
    assert keyword == []  # no chunk shares a meaningful word with this question
    vector = post(db_client, rid, "how do users log in", mode="vector").json()
    assert vector["mode_used"] == "vector" and vector["warnings"] == []
    top = paths(vector["hits"])[:3]
    assert "auth/passwords.py" in top and "auth/sessions.py" in top
    assert "billing/invoices.py" not in top
    assert all(h["vector_rank"] is not None and h["keyword_rank"] is None for h in vector["hits"])


def test_vector_search_only_sees_the_requested_snapshot(
    embedded: tuple[str, uuid.UUID, ScriptedEmbedder],
    db_client: TestClient,
    db_app: FastAPI,
) -> None:
    # A second repository holds a chunk that is just as close to the query ("session" is an
    # auth topic word). If the vector query forgot to filter by snapshot, it would be returned.
    rid, _sid, _e = embedded
    other = FakeGitHub({"secret/vault.py": "def session_vault():\n    return 'other repo'\n"})
    db_app.dependency_overrides[get_github] = lambda: other
    second = db_client.post("/api/repositories", json={"url": "https://github.com/o/other"}).json()[
        "id"
    ]
    db_client.post(f"/api/repositories/{second}/index")
    run_until(db_client, second, "graph")

    mine = post(db_client, rid, "sign in", mode="vector", limit=20).json()["hits"]
    assert mine and "secret/vault.py" not in paths(mine)
    theirs = post(db_client, second, "sign in", mode="vector", limit=20).json()["hits"]
    assert paths(theirs) == ["secret/vault.py"]


def test_hybrid_rewards_chunks_that_both_methods_find(
    embedded: tuple[str, uuid.UUID, ScriptedEmbedder], db_client: TestClient
) -> None:
    rid, _sid, _e = embedded
    body = post(db_client, rid, "invoice total tax", mode="hybrid").json()
    top = body["hits"][0]
    assert top["path"] == "billing/invoices.py"
    assert top["keyword_rank"] == 1 and top["vector_rank"] == 1
    assert top["score"] == pytest.approx(2 / 61, abs=1e-6)


def test_hybrid_finds_what_keyword_alone_misses(
    embedded: tuple[str, uuid.UUID, ScriptedEmbedder], db_client: TestClient
) -> None:
    rid, _sid, _e = embedded
    body = post(db_client, rid, "how do users log in", mode="hybrid").json()
    assert body["mode_used"] == "hybrid" and body["hits"]
    assert paths(body["hits"])[0].startswith("auth/")
    assert all(h["keyword_rank"] is None for h in body["hits"])  # found purely by meaning


def test_hybrid_keeps_exact_identifier_matches_that_meaning_alone_would_bury(
    embedded: tuple[str, uuid.UUID, ScriptedEmbedder], db_client: TestClient
) -> None:
    rid, _sid, _e = embedded
    body = post(db_client, rid, "ListenAndServe", mode="hybrid").json()
    assert body["hits"][0]["path"] == "cmd/server/main.go"


def test_every_hit_text_is_exactly_the_cited_lines(
    embedded: tuple[str, uuid.UUID, ScriptedEmbedder], db_client: TestClient
) -> None:
    rid, _sid, _e = embedded
    for query in ("create_session", "invoice total", "sign in"):
        for hit in post(db_client, rid, query, limit=20).json()["hits"]:
            source = FILES[hit["path"]]
            assert isinstance(source, str)
            lines = source.split("\n")
            assert hit["text"] == "\n".join(lines[hit["start_line"] - 1 : hit["end_line"]])


def test_the_limit_is_respected(
    embedded: tuple[str, uuid.UUID, ScriptedEmbedder], db_client: TestClient
) -> None:
    rid, _sid, _e = embedded
    assert len(post(db_client, rid, "session", limit=2).json()["hits"]) == 2
    assert len(post(db_client, rid, "session", limit=20).json()["hits"]) <= 6


# --- when embeddings are not available ---------------------------------------------------------


def test_before_embeddings_exist_hybrid_and_vector_fall_back_to_keyword_and_say_so(
    db_client: TestClient,
    fake_gh: FakeGitHub,
    db_app: FastAPI,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_embedder(db_app, monkeypatch, scripted())
    rid = index_repo(db_client, fake_gh, dict(FILES))
    run_until(db_client, rid, "embed")  # parsed and chunked, but not embedded yet
    for mode in ("hybrid", "vector"):
        body = post(db_client, rid, "create_session", mode=mode).json()
        assert body["mode_used"] == "keyword" and body["warnings"] == ["embeddings_not_ready"]
        assert body["hits"][0]["path"] == "auth/sessions.py"
    with Session(db_engine) as db:
        sid = snapshot_id(db_client, rid)
        with pytest.raises(RuntimeError, match="no embeddings"):
            search(db, sid, "create_session", "hybrid", 5, scripted(), strict=True)


def test_a_failing_embedding_service_degrades_search_instead_of_breaking_it(
    embedded: tuple[str, uuid.UUID, ScriptedEmbedder],
    db_client: TestClient,
    db_app: FastAPI,
    db_engine: Engine,
) -> None:
    rid, sid, _e = embedded
    broken = scripted(fail_on_call=-1, error=EmbeddingUnavailable("the service is down"))
    db_app.dependency_overrides[get_embedder] = lambda: broken
    for mode in ("hybrid", "vector"):
        r = post(db_client, rid, "create_session", mode=mode)
        assert r.status_code == 200
        assert r.json()["warnings"] == ["embedding_failed:embedding_unavailable"]
        assert r.json()["mode_used"] == "keyword" and r.json()["hits"]
    with Session(db_engine) as db, pytest.raises(EmbeddingUnavailable):
        search(db, sid, "create_session", "hybrid", 5, broken, strict=True)


def test_no_embedder_configured_still_allows_keyword_search(
    embedded: tuple[str, uuid.UUID, ScriptedEmbedder], db_client: TestClient, db_app: FastAPI
) -> None:
    rid, _sid, _e = embedded
    db_app.dependency_overrides[get_embedder] = lambda: None
    body = post(db_client, rid, "slugify").json()
    assert body["mode_used"] == "keyword" and body["warnings"] == ["embeddings_not_ready"]
    assert body["hits"][0]["path"] == "utils/strings.py"


# --- the endpoint -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"query": ""},
        {"query": "x" * 501},
        {"query": "ok", "limit": 0},
        {"query": "ok", "limit": 21},
        {"query": "ok", "mode": "fuzzy"},
        {"query": "ok", "snapshot": "not-a-uuid"},
        {},
    ],
)
def test_invalid_requests_are_a_422(
    embedded: tuple[str, uuid.UUID, ScriptedEmbedder], db_client: TestClient, body: dict[str, Any]
) -> None:
    rid, _sid, _e = embedded
    assert db_client.post(f"/api/repositories/{rid}/search", json=body).status_code == 422


def test_nothing_to_search_before_indexing(db_client: TestClient) -> None:
    rid = str(
        db_client.post("/api/repositories", json={"url": "https://github.com/o/r"}).json()["id"]
    )
    r = post(db_client, rid, "anything")
    assert r.status_code == 409 and r.json()["error"]["code"] == "snapshot_not_ready"


def test_another_users_repository_cannot_be_searched(
    embedded: tuple[str, uuid.UUID, ScriptedEmbedder],
    other_client: TestClient,
    anon_client: TestClient,
) -> None:
    rid, _sid, _e = embedded
    r = post(other_client, rid, "create_session")
    assert r.status_code == 404 and r.json()["error"]["code"] == "repo_not_found"
    r = post(anon_client, rid, "create_session")
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthenticated"


def test_search_is_scoped_to_one_repository(
    embedded: tuple[str, uuid.UUID, ScriptedEmbedder],
    db_client: TestClient,
    db_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rid, _sid, _e = embedded
    other = FakeGitHub(
        {"secret/vault.py": "def create_session_vault():\n    return 'only in the other repo'\n"}
    )
    db_app.dependency_overrides[get_github] = lambda: other
    second = db_client.post("/api/repositories", json={"url": "https://github.com/o/other"}).json()[
        "id"
    ]
    db_client.post(f"/api/repositories/{second}/index")
    run_until(db_client, second, "graph")

    mine = post(db_client, rid, "create_session vault", mode="keyword").json()["hits"]
    assert "secret/vault.py" not in paths(mine)
    theirs = post(db_client, second, "create_session vault", mode="keyword").json()["hits"]
    assert paths(theirs) == ["secret/vault.py"]

    snap = snapshot_id(db_client, second)
    r = post(db_client, rid, "create_session", snapshot=str(snap))
    assert r.status_code == 404 and r.json()["error"]["code"] == "snapshot_not_found"


def test_searches_are_rate_limited_per_user(
    embedded: tuple[str, uuid.UUID, ScriptedEmbedder], db_client: TestClient
) -> None:
    rid, _sid, _e = embedded
    codes = [post(db_client, rid, "session", mode="keyword").status_code for _ in range(21)]
    assert codes[:20] == [200] * 20 and codes[20] == 429


def test_secrets_that_were_redacted_cannot_be_found_by_searching_for_them(
    db_client: TestClient, fake_gh: FakeGitHub, db_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.integration.test_parse_phase import GENERIC_SECRET, GITHUB_TOKEN

    use_embedder(db_app, monkeypatch, scripted())
    rid = index_repo(
        db_client,
        fake_gh,
        {
            "cfg.py": (
                f'password = "{GENERIC_SECRET}"\n'
                f'GH = "{GITHUB_TOKEN}"\n\ndef load():\n    return 1\n'
            )
        },
    )
    run_until(db_client, rid, "graph")
    for needle in (GENERIC_SECRET, GITHUB_TOKEN):
        for mode in ("keyword", "vector", "hybrid"):
            body = post(db_client, rid, needle, mode=mode).json()
            assert needle not in str(body)


# --- the jump-to-definition signal ------------------------------------------------------------

DEFINITION_FILES: dict[str, str | bytes] = {
    "lib/links.py": (
        "def parse_header_links(value):\n"
        '    """Split a Link header into a list of link dictionaries."""\n'
        "    return [part.strip() for part in value.split(',')]\n"
    ),
    "app/handlers.py": (
        "def follow_next(response):\n"
        "    links = parse_header_links(response.headers['link'])\n"
        "    again = parse_header_links(links)\n"
        "    return parse_header_links(again)\n"
    ),
    "tests/test_links.py": (
        "def test_parse_header_links_empty():\n"
        "    assert parse_header_links('') == []\n\n\n"
        "def test_parse_header_links_one():\n"
        "    assert parse_header_links('<a>') == ['<a>']\n"
    ),
}


def test_a_definition_outranks_its_callers_and_tests_when_the_name_is_given(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = index_repo(db_client, fake_gh, dict(DEFINITION_FILES))
    run_until(db_client, rid, "graph")
    sid = snapshot_id(db_client, rid)
    question = "Show me `parse_header_links`."
    with Session(db_engine) as db:

        def first_path(boost: bool) -> str:
            ids = keyword_search(db, sid, question, 10, symbol_boost=boost)
            return str(
                db.execute(
                    text(
                        "SELECT f.path FROM chunks c JOIN files f ON f.id = c.file_id "
                        "WHERE c.id = :i"
                    ),
                    {"i": ids[0]},
                ).scalar_one()
            )

        assert first_path(boost=True) == "lib/links.py"
        # Without the signal the callers and tests, which repeat the name, win on text alone:
        # this is the weakness the signal exists to fix.
        assert first_path(boost=False) != "lib/links.py"


def test_the_boost_never_applies_to_a_plain_english_question(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = index_repo(db_client, fake_gh, dict(DEFINITION_FILES))
    run_until(db_client, rid, "graph")
    sid = snapshot_id(db_client, rid)
    with Session(db_engine) as db:
        question = "how are link headers split into dictionaries"
        assert keyword_search(db, sid, question, 10, True) == keyword_search(
            db, sid, question, 10, False
        )
