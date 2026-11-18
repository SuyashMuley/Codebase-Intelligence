"""A private repository is embedded, searched and asked about with its owner's own providers,
and never with the platform's."""

import logging
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.api.deps import get_embedder, get_llm
from app.services.providers import build, ssrf
from tests.helpers import FakeGitHub, ScriptedEmbedder
from tests.integration.test_chat_api import FakeChat, grounded
from tests.integration.test_parse_phase import rows
from tests.integration.test_private_repos import private_repo
from tests.integration.test_search import FILES, scripted

pytestmark = pytest.mark.db

EMB_KEY = "sk-emb-SECRET-0123456789ab"
CHAT_KEY = "sk-chat-SECRET-0123456789ab"
ACCOUNT = "0123456789abcdef0123456789abcdef"


class UserEmbedder(ScriptedEmbedder):
    model_id = "user-embedder-v1"

    def __init__(self, key: str) -> None:
        inner = scripted()
        super().__init__(inner.topics, inner.query_topics)
        self.key = key
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class UserChat(FakeChat):
    def __init__(self, key: str) -> None:
        super().__init__(lambda p: grounded(p, "def create_session"))
        self.key = key
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class Platform:
    """The platform's providers, wired up but forbidden: any use fails the test."""

    def __init__(self) -> None:
        self.used: list[str] = []
        outer = self

        class Embedder(ScriptedEmbedder):
            model_id = "platform-embedder-v1"

            def embed_query(self, text: str) -> list[float]:
                outer.used.append("embed_query")
                return super().embed_query(text)

            def embed_documents(self, texts: list[str]) -> list[list[float]]:
                outer.used.append("embed_documents")
                return super().embed_documents(texts)

        inner = scripted()
        self.embedder = Embedder(inner.topics, inner.query_topics)

        class Chat(FakeChat):
            def complete(self, system: str, user: str, **kw: Any) -> str:
                outer.used.append("chat")
                return super().complete(system, user, **kw)

        self.chat = Chat(lambda p: grounded(p, "def create_session"))


class World:
    def __init__(self) -> None:
        self.embedders: list[UserEmbedder] = []
        self.chats: list[UserChat] = []
        self.keys: list[tuple[str, str]] = []  # (slot, key the builder received)


@pytest.fixture
def world(db_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> tuple[World, Platform]:
    w, platform = World(), Platform()
    db_app.dependency_overrides[get_embedder] = lambda: platform.embedder
    db_app.dependency_overrides[get_llm] = lambda: platform.chat
    monkeypatch.setattr(
        "app.services.indexing.embed_phase.get_embedder", lambda _s: platform.embedder
    )

    def build_embedder(row: Any, key: str, s: Any) -> UserEmbedder:
        w.keys.append(("embedding", key))
        e = UserEmbedder(key)
        w.embedders.append(e)
        return e

    def build_chat(row: Any, key: str, s: Any) -> UserChat:
        w.keys.append(("chat", key))
        c = UserChat(key)
        w.chats.append(c)
        return c

    monkeypatch.setattr(build, "build_embedder", build_embedder)
    monkeypatch.setattr(build, "build_chat", build_chat)
    return w, platform


def save(c: TestClient, slot: str, mode: str = "stored", **over: Any) -> None:
    if slot == "embedding":
        body: dict[str, Any] = {"kind": "cloudflare", "mode": mode, "account_id": ACCOUNT}
        key = EMB_KEY
    else:
        body = {"kind": "openai", "mode": mode}
        key = CHAT_KEY
    if mode == "stored":
        body["api_key"] = key
    body.update(over)
    r = c.put(f"/api/providers/{slot}", json=body)
    assert r.status_code == 200, r.text


def headers(emb: bool = True, chat: bool = True) -> dict[str, str]:
    h = {}
    if emb:
        h["X-Embedding-Key"] = EMB_KEY
    if chat:
        h["X-Chat-Key"] = CHAT_KEY
    return h


def run_index(c: TestClient, rid: str, **kw: Any) -> None:
    assert c.post(f"/api/repositories/{rid}/index", **kw).status_code == 202
    for _ in range(60):
        r = c.post(f"/api/repositories/{rid}/index/tick", **kw)
        assert r.status_code == 200, r.text
        if r.json()["status"]["state"] == "ready":
            return
    raise AssertionError("never became ready")


def code(r: Any) -> str:
    return str(r.json()["error"]["code"])


# --- the policy gate ---------------------------------------------------------------------


def test_indexing_needs_both_providers_and_never_falls_back_to_the_platform(
    db_client: TestClient, fake_gh: FakeGitHub, world: tuple[World, Platform], db_engine: Engine
) -> None:
    w, platform = world
    fake_gh.files = dict(FILES)
    rid = private_repo(db_client, fake_gh)
    url = f"/api/repositories/{rid}/index"

    r = db_client.post(url)  # nothing configured, though the platform has working providers
    assert r.status_code == 409 and code(r) == "providers_required"
    save(db_client, "embedding")
    r = db_client.post(url)  # chat is still missing
    assert r.status_code == 409 and "chat provider" in r.json()["error"]["message"]
    assert rows(db_engine, "SELECT count(*) FROM snapshots")[0][0] == 0
    assert fake_gh.resolve_calls == 0 and platform.used == []

    save(db_client, "chat")
    assert db_client.post(url).status_code == 202


def test_ticking_needs_the_embedding_provider(
    db_client: TestClient, fake_gh: FakeGitHub, world: tuple[World, Platform]
) -> None:
    w, platform = world
    fake_gh.files = dict(FILES)
    rid = private_repo(db_client, fake_gh)
    save(db_client, "embedding")
    save(db_client, "chat")
    assert db_client.post(f"/api/repositories/{rid}/index").status_code == 202
    db_client.delete("/api/providers/embedding")
    r = db_client.post(f"/api/repositories/{rid}/index/tick")
    assert r.status_code == 409 and code(r) == "providers_required"
    assert platform.used == []


# --- the whole flow uses the user's providers --------------------------------------------


def test_index_search_and_ask_use_only_the_owners_providers(
    db_client: TestClient, fake_gh: FakeGitHub, world: tuple[World, Platform], db_engine: Engine
) -> None:
    w, platform = world
    fake_gh.files = dict(FILES)
    rid = private_repo(db_client, fake_gh)
    save(db_client, "embedding")
    save(db_client, "chat")
    run_index(db_client, rid)

    models = {r[0] for r in rows(db_engine, "SELECT DISTINCT embedding_model FROM chunks")}
    assert models == {"user-embedder-v1"}  # every vector came from the owner's provider
    assert all(key == EMB_KEY for slot, key in w.keys if slot == "embedding")

    found = db_client.post(f"/api/repositories/{rid}/search", json={"query": "user login sessions"})
    assert found.status_code == 200 and found.json()["mode_used"] == "vector"
    assert found.json()["warnings"] == [] and found.json()["hits"]

    answer = db_client.post(
        f"/api/repositories/{rid}/chat", json={"question": "how do users log in with a session"}
    )
    assert answer.status_code == 200 and answer.json()["insufficient_evidence"] is False
    assert w.chats and w.chats[-1].prompts  # the owner's model was asked

    assert platform.used == []  # not once, for anything
    assert platform.chat.prompts == []


def test_clients_built_for_a_request_are_closed_when_it_ends(
    db_client: TestClient, fake_gh: FakeGitHub, world: tuple[World, Platform]
) -> None:
    w, _ = world
    fake_gh.files = dict(FILES)
    rid = private_repo(db_client, fake_gh)
    save(db_client, "embedding")
    save(db_client, "chat")
    run_index(db_client, rid)
    n = len(w.embedders)
    db_client.post(f"/api/repositories/{rid}/search", json={"query": "user login sessions"})
    db_client.post(
        f"/api/repositories/{rid}/chat", json={"question": "how do users log in with a session"}
    )
    assert all(e.closed == 1 for e in w.embedders) and len(w.embedders) > n
    assert all(c.closed == 1 for c in w.chats)


def test_ai_review_needs_the_chat_provider_of_a_private_repository(
    db_client: TestClient, fake_gh: FakeGitHub, world: tuple[World, Platform]
) -> None:
    w, platform = world
    fake_gh.files = dict(FILES)
    rid = private_repo(db_client, fake_gh)
    r = db_client.post(f"/api/repositories/{rid}/impact/review", json={"file": "auth/sessions.py"})
    assert r.status_code == 409 and code(r) == "providers_required"
    assert platform.chat.prompts == []


# --- public repositories are unaffected --------------------------------------------------


def test_public_repositories_keep_using_the_platforms_providers(
    db_client: TestClient, fake_gh: FakeGitHub, world: tuple[World, Platform]
) -> None:
    w, platform = world
    save(db_client, "embedding")  # the user has providers; a public repository must not use them
    save(db_client, "chat")
    fake_gh.files = dict(FILES)
    rid = db_client.post("/api/repositories", json={"url": "https://github.com/acme/open"}).json()[
        "id"
    ]
    run_index(db_client, rid)
    db_client.post(f"/api/repositories/{rid}/search", json={"query": "user login sessions"})
    assert "embed_documents" in platform.used and "embed_query" in platform.used
    assert w.embedders == [] and w.chats == [] and w.keys == []


# --- browser-held keys -------------------------------------------------------------------


def test_a_browser_held_key_must_be_sent_and_is_never_stored_or_logged(
    db_client: TestClient,
    fake_gh: FakeGitHub,
    world: tuple[World, Platform],
    db_engine: Engine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    w, platform = world
    fake_gh.files = dict(FILES)
    rid = private_repo(db_client, fake_gh)
    save(db_client, "embedding", mode="browser")
    save(db_client, "chat", mode="browser")
    url = f"/api/repositories/{rid}/index"

    r = db_client.post(url)  # configured, but no key arrived with the request
    assert r.status_code == 409 and "this browser" in r.json()["error"]["message"]
    r = db_client.post(url, headers=headers(chat=False))  # the chat key is missing
    assert r.status_code == 409 and "chat key" in r.json()["error"]["message"]

    run_index(db_client, rid, headers=headers())
    assert ("embedding", EMB_KEY) in w.keys and ("chat", CHAT_KEY) in w.keys

    # A later request without the headers cannot use the provider: nothing was kept.
    r = db_client.post(f"/api/repositories/{rid}/search", json={"query": "user login sessions"})
    assert r.status_code == 409
    r = db_client.post(
        f"/api/repositories/{rid}/search",
        json={"query": "user login sessions"},
        headers=headers(),
    )
    assert r.status_code == 200

    everything = "".join(str(r) for r in rows(db_engine, "SELECT * FROM user_providers"))
    assert EMB_KEY not in everything and CHAT_KEY not in everything
    assert EMB_KEY not in caplog.text and CHAT_KEY not in caplog.text
    assert platform.used == []


def test_a_malformed_browser_key_is_treated_as_missing(
    db_client: TestClient, fake_gh: FakeGitHub, world: tuple[World, Platform]
) -> None:
    w, _ = world
    fake_gh.files = dict(FILES)
    rid = private_repo(db_client, fake_gh)
    save(db_client, "embedding", mode="browser")
    save(db_client, "chat", mode="browser")
    for bad in ("short", "has spaces in the key 123", "x" * 500):
        r = db_client.post(
            f"/api/repositories/{rid}/index",
            headers={"X-Embedding-Key": bad, "X-Chat-Key": CHAT_KEY},
        )
        assert r.status_code == 409, bad
    assert w.keys == []


# --- stored keys -------------------------------------------------------------------------


def test_a_stored_key_is_decrypted_for_the_request(
    db_client: TestClient, fake_gh: FakeGitHub, world: tuple[World, Platform]
) -> None:
    w, _ = world
    fake_gh.files = dict(FILES)
    rid = private_repo(db_client, fake_gh)
    save(db_client, "embedding")
    save(db_client, "chat")
    run_index(db_client, rid)
    assert set(w.keys) == {("embedding", EMB_KEY), ("chat", CHAT_KEY)}
    # a browser header does not override a stored key
    db_client.post(
        f"/api/repositories/{rid}/search",
        json={"query": "user login sessions"},
        headers={"X-Embedding-Key": "sk-someone-else-0123456789"},
    )
    assert ("embedding", "sk-someone-else-0123456789") not in w.keys


def test_each_user_gets_only_their_own_keys(
    db_client: TestClient,
    other_client: TestClient,
    fake_gh: FakeGitHub,
    world: tuple[World, Platform],
) -> None:
    w, _ = world
    fake_gh.files = dict(FILES)
    save(db_client, "embedding")
    save(db_client, "chat")
    a = private_repo(db_client, fake_gh)
    b = private_repo(other_client, fake_gh)  # bob has access too but configured nothing
    r = other_client.post(f"/api/repositories/{b}/index")
    assert r.status_code == 409 and code(r) == "providers_required"
    run_index(db_client, a)
    assert all(key in (EMB_KEY, CHAT_KEY) for _slot, key in w.keys)


# --- a changed provider ------------------------------------------------------------------


def test_search_does_not_compare_vectors_from_different_models(
    db_client: TestClient,
    fake_gh: FakeGitHub,
    world: tuple[World, Platform],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w, _ = world
    fake_gh.files = dict(FILES)
    rid = private_repo(db_client, fake_gh)
    save(db_client, "embedding")
    save(db_client, "chat")
    run_index(db_client, rid)

    def other_model(row: Any, key: str, s: Any) -> UserEmbedder:
        e = UserEmbedder(key)
        e.model_id = "a-different-model-v2"  # the owner switched providers after indexing
        return e

    monkeypatch.setattr(build, "build_embedder", other_model)
    r = db_client.post(f"/api/repositories/{rid}/search", json={"query": "user login sessions"})
    assert r.status_code == 200
    assert r.json()["mode_used"] == "keyword" and "embeddings_model_changed" in r.json()["warnings"]


# --- the address guard -------------------------------------------------------------------


def test_a_provider_address_that_resolves_internally_is_refused_before_any_call(
    db_client: TestClient,
    fake_gh: FakeGitHub,
    db_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_gh.files = dict(FILES)
    rid = private_repo(db_client, fake_gh)
    save(db_client, "embedding")
    save(db_client, "chat")
    sent: list[str] = []
    monkeypatch.setattr("httpx.Client.send", lambda self, *a, **k: sent.append("sent"))
    for internal in ("127.0.0.1", "169.254.169.254", "10.0.0.7"):
        monkeypatch.setattr(ssrf, "resolver", lambda host, ip=internal: [ip])
        r = db_client.post(f"/api/repositories/{rid}/search", json={"query": "user login sessions"})
        assert r.status_code == 502 and code(r) == "provider_address_refused", internal
        r = db_client.post(
            f"/api/repositories/{rid}/chat", json={"question": "how do users log in"}
        )
        assert r.status_code == 502 and code(r) == "provider_address_refused", internal
        # The chat client alone (AI review uses no embeddings) is guarded on its own.
        r = db_client.post(
            f"/api/repositories/{rid}/impact/review", json={"file": "auth/sessions.py"}
        )
        assert r.status_code == 502 and code(r) == "provider_address_refused", internal
    assert sent == []  # no request left the server, so no key and no code either
