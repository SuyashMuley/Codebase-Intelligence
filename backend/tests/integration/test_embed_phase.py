import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.core.errors import (
    EmbeddingAuthFailed,
    EmbeddingBadResponse,
    EmbeddingNotConfigured,
    EmbeddingQuotaExceeded,
    EmbeddingUnavailable,
)
from app.providers.embeddings.hash import HashEmbedder
from app.services.retrieval.documents import document_text
from tests.helpers import FakeGitHub, ScriptedEmbedder
from tests.integration.test_parse_phase import (
    REPO_FILES,
    expired,
    index_repo,
    rows,
    run_until,
    settings_override,
    tick,
)

pytestmark = pytest.mark.db

TOPICS = {"auth": ["session"], "util": ["slugify", "strip"]}


def scripted(monkeypatch: pytest.MonkeyPatch, **kw: Any) -> ScriptedEmbedder:
    embedder = ScriptedEmbedder(TOPICS, {}, **kw)
    monkeypatch.setattr(
        "app.services.indexing.embed_phase.get_embedder", lambda _settings: embedder
    )
    return embedder


def indexed_until_embed(
    c: TestClient, fake: FakeGitHub, files: dict[str, Any] | None = None
) -> str:
    rid = index_repo(c, fake, REPO_FILES if files is None else files)
    return rid


def chunk_count(engine: Engine) -> int:
    return int(rows(engine, "SELECT count(*) FROM chunks")[0][0])


# --- storing vectors --------------------------------------------------------------------------


def test_every_chunk_gets_a_vector_of_the_database_size_and_the_model_is_recorded(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = indexed_until_embed(db_client, fake_gh)
    status = run_until(db_client, rid, "graph")
    total = chunk_count(db_engine)
    assert total > 0
    assert status["embed"] == {
        "embedded": total,
        "total": total,
        "model": "hash-384-v1",
        "done": True,
        "neurons": 0.0,
    }
    assert rows(db_engine, "SELECT count(*) FROM chunks WHERE embedding IS NULL")[0][0] == 0
    assert [
        r[0] for r in rows(db_engine, "SELECT DISTINCT vector_dims(embedding) FROM chunks")
    ] == [384]
    assert [r[0] for r in rows(db_engine, "SELECT DISTINCT embedding_model FROM chunks")] == [
        "hash-384-v1"
    ]


def test_stored_vectors_are_what_the_embedder_returns_for_path_plus_text(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = indexed_until_embed(db_client, fake_gh)
    run_until(db_client, rid, "graph")
    stored = rows(
        db_engine,
        "SELECT f.path, c.text, c.embedding::text FROM chunks c JOIN files f ON f.id = c.file_id",
    )
    assert stored
    expected = HashEmbedder().embed_documents([document_text(p, t) for p, t, _ in stored])
    for (_path, _text, raw), want in zip(stored, expected, strict=True):
        got = json.loads(raw)
        assert len(got) == 384
        assert got == pytest.approx(want, abs=1e-6)  # float4 storage rounds slightly


def test_the_text_sent_for_embedding_includes_the_path_but_the_stored_text_does_not(
    db_client: TestClient,
    fake_gh: FakeGitHub,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedder = scripted(monkeypatch)
    rid = indexed_until_embed(db_client, fake_gh)
    run_until(db_client, rid, "graph")
    stored = {t for (t,) in rows(db_engine, "SELECT text FROM chunks")}
    assert len(embedder.seen) == len(stored)
    for sent in embedder.seen:
        header, _, body = sent.partition("\n")
        assert header.startswith("File: ") and body in stored
    assert not any(t.startswith("File: ") for t in stored)


def test_only_the_snapshots_own_chunks_are_embedded(
    db_client: TestClient, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    embedder = scripted(monkeypatch)
    first = index_repo(db_client, FakeGitHub(), {"a.py": "def a():\n    return 1\n"})
    run_until(db_client, first, "graph")
    done = embedder.calls
    other = FakeGitHub({"b.py": "def b():\n    return 2\n"})
    # A second repository, indexed only as far as parsing, must keep its chunks un-embedded.
    second_id = db_client.post(
        "/api/repositories", json={"url": "https://github.com/o/second"}
    ).json()["id"]
    from app.api.deps import get_github

    db_client.app.dependency_overrides[get_github] = lambda: other  # type: ignore[attr-defined]
    db_client.post(f"/api/repositories/{second_id}/index")
    run_until(db_client, second_id, "embed")
    assert embedder.calls == done
    assert rows(db_engine, "SELECT count(*) FROM chunks WHERE embedding IS NULL")[0][0] == 1


# --- resumability -----------------------------------------------------------------------------


def test_a_tiny_time_budget_embeds_one_batch_per_tick_and_resumes(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    settings_override(db_client, embedding_batch_size=3, embedding_budget_seconds=0.0)
    rid = indexed_until_embed(db_client, fake_gh)
    run_until(db_client, rid, "embed")  # fetch and parse
    counts = []
    for _ in range(40):
        status = tick(db_client, rid)["status"]
        counts.append(status["embed"]["embedded"])
        if status["phase"] == "graph":
            break
    total = chunk_count(db_engine)
    assert counts[0] == 3 and all(
        b - a in (3, total % 3 or 3) for a, b in zip(counts, counts[1:], strict=False)
    )
    assert counts[-1] == total and status["embed"]["done"] is True


def test_a_failed_batch_loses_only_that_batch_and_the_phase_continues_from_there(
    db_client: TestClient,
    fake_gh: FakeGitHub,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_override(db_client, embedding_budget_seconds=0.0)
    embedder = scripted(monkeypatch, fail_on_call=3, error=EmbeddingUnavailable("service busy"))
    embedder.batch_size = 2
    rid = indexed_until_embed(db_client, fake_gh)
    run_until(db_client, rid, "embed")
    tick(db_client, rid)
    tick(db_client, rid)
    failed = tick(db_client, rid)["status"]
    assert failed["error"]["code"] == "embedding_unavailable" and failed["attempts"] == 1
    assert failed["retry_at"] is not None and failed["state"] == "running"
    assert rows(db_engine, "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL")[0][0] == 4

    expired(db_engine)
    status = run_until(db_client, rid, "graph")
    assert status["attempts"] == 0 and status["embed"]["done"] is True
    assert rows(db_engine, "SELECT count(*) FROM chunks WHERE embedding IS NULL")[0][0] == 0


def test_a_changed_embedding_model_makes_the_old_vectors_be_redone(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = indexed_until_embed(db_client, fake_gh)
    run_until(db_client, rid, "graph")
    from sqlalchemy import text

    with db_engine.begin() as conn:  # pretend the vectors came from another model
        conn.execute(text("UPDATE chunks SET embedding_model = 'some-older-model'"))
        conn.execute(text("UPDATE index_jobs SET phase = 'embed', cursor = cursor - 'embed'"))
    status = run_until(db_client, rid, "graph")
    assert [r[0] for r in rows(db_engine, "SELECT DISTINCT embedding_model FROM chunks")] == [
        "hash-384-v1"
    ]
    assert status["embed"]["model"] == "hash-384-v1" and status["embed"]["done"] is True


def test_provider_usage_is_reported_so_cost_is_visible(
    db_client: TestClient,
    fake_gh: FakeGitHub,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripted(monkeypatch)
    rid = indexed_until_embed(db_client, fake_gh)
    status = run_until(db_client, rid, "graph")
    assert status["embed"]["neurons"] == pytest.approx(0.5 * chunk_count(db_engine))
    stats = db_client.get(f"/api/repositories/{rid}/stats").json()
    assert stats["chunks"]["embedded"] == stats["chunks"]["total"] == chunk_count(db_engine)
    assert stats["chunks"]["embedding_model"] == "scripted-v1"


# --- failures ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (
            EmbeddingQuotaExceeded("The daily free embedding allowance is used up."),
            "embedding_quota_exceeded",
        ),
        (EmbeddingAuthFailed("Check CLOUDFLARE_API_TOKEN."), "embedding_auth_failed"),
        (EmbeddingBadResponse("Wrong number of vectors."), "embedding_bad_response"),
        (EmbeddingNotConfigured("Embeddings are not configured."), "embedding_not_configured"),
    ],
)
def test_problems_that_retrying_cannot_fix_fail_the_job_at_once_and_keep_the_parsed_data(
    db_client: TestClient,
    fake_gh: FakeGitHub,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    code: str,
) -> None:
    scripted(monkeypatch, fail_on_call=1, error=error)
    rid = indexed_until_embed(db_client, fake_gh)
    status = run_until(db_client, rid, "graph")
    assert status["state"] == "failed" and status["can_resume"] is True
    assert status["error"]["code"] == code and status["attempts"] == 1
    assert status["retry_at"] is None
    assert chunk_count(db_engine) > 0  # parsing is not undone
    assert rows(db_engine, "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL")[0][0] == 0


def test_missing_credentials_are_reported_clearly_and_can_be_fixed_then_resumed(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    settings_override(
        db_client,
        embedding_provider="cloudflare",
        cloudflare_account_id=None,
        cloudflare_api_token=None,
    )
    rid = indexed_until_embed(db_client, fake_gh)
    status = run_until(db_client, rid, "graph")
    assert status["state"] == "failed"
    assert status["error"]["code"] == "embedding_not_configured"
    assert "CLOUDFLARE_API_TOKEN" in status["error"]["message"]

    # Fix the configuration and restart: the same job continues from the embed phase.
    settings_override(db_client, embedding_provider="hash")
    restarted = db_client.post(f"/api/repositories/{rid}/index")
    assert restarted.status_code == 200 and restarted.json()["status"]["phase"] == "embed"
    status = run_until(db_client, rid, "graph")
    assert status["state"] == "running" and status["embed"]["done"] is True


def test_a_repository_with_nothing_to_embed_needs_no_credentials(
    db_client: TestClient, fake_gh: FakeGitHub
) -> None:
    settings_override(
        db_client,
        embedding_provider="cloudflare",
        cloudflare_account_id=None,
        cloudflare_api_token=None,
    )
    rid = indexed_until_embed(db_client, fake_gh, {"NOTES.md": "# docs only\n"})
    status = run_until(db_client, rid, "graph")
    assert status["state"] == "running" and status["embed"]["total"] == 0
    assert status["embed"]["done"] is True
