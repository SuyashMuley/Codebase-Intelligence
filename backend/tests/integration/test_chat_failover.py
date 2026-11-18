"""Asking a question when the platform's first chat provider is limited or down."""

from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.api.deps import get_llm
from app.core.errors import LlmAuthFailed, LlmRateLimited, LlmUnavailable
from app.providers.llm.fallback import Chat, FallbackChat
from tests.integration.test_chat_api import (  # noqa: F401  (chat_repo is a fixture)
    FakeChat,
    ask,
    chat_repo,
    grounded,
    usage,
)
from tests.integration.test_private_providers import save, world  # noqa: F401
from tests.integration.test_private_repos import private_repo
from tests.integration.test_search import FILES

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("chat_repo")]

QUESTION = "how do users log in with a session"


class Named(FakeChat):
    def __init__(self, model: str, *script: Any) -> None:
        super().__init__(*script)
        self.last_model = model


def serve(app: FastAPI, primary: FakeChat, secondary: FakeChat) -> FallbackChat:
    chain = FallbackChat([("openrouter", cast(Chat, primary)), ("groq", cast(Chat, secondary))])
    app.dependency_overrides[get_llm] = lambda: chain
    return chain


def answer(prompt: str) -> str:
    return grounded(prompt, "def create_session")


@pytest.fixture
def repo(request: pytest.FixtureRequest) -> str:
    rid, _embedder = request.getfixturevalue("chat_repo")
    return str(rid)


def test_a_limited_first_provider_is_invisible_to_the_user_and_the_quota_is_charged_once(
    db_client: TestClient, db_app: FastAPI, db_engine: Engine, repo: str
) -> None:
    first = Named("or/free-model", LlmRateLimited("slow", details={"retry_after_seconds": 600}))
    second = Named("groq/gpt-oss", answer)
    chain = serve(db_app, first, second)

    body = ask(db_client, repo, QUESTION).json()
    assert body["insufficient_evidence"] is False and body["claims"]
    assert body["model"] == "groq/gpt-oss"  # the answer says which model really wrote it
    assert usage(db_engine) == 1  # one question, one count, whichever provider answered
    assert len(first.prompts) == 1 and len(second.prompts) == 1
    assert chain.status()["openrouter"] > 590


def test_while_the_first_provider_is_limited_every_request_goes_to_the_second(
    db_client: TestClient, db_app: FastAPI, repo: str
) -> None:
    first = Named("or/free-model", LlmRateLimited("slow", details={"retry_after_seconds": 600}))
    second = Named("groq/gpt-oss", answer)
    serve(db_app, first, second)
    for _ in range(4):
        assert ask(db_client, repo, QUESTION).json()["model"] == "groq/gpt-oss"
    assert len(first.prompts) == 1  # tried once, then left alone
    assert len(second.prompts) == 4


def test_an_unavailable_or_rejected_first_provider_is_covered_too(
    db_client: TestClient, db_app: FastAPI, repo: str
) -> None:
    for error in (LlmUnavailable("down"), LlmAuthFailed("bad key")):
        second = Named("groq/gpt-oss", answer)
        serve(db_app, Named("or/free-model", error), second)
        assert ask(db_client, repo, QUESTION).status_code == 200


def test_when_both_providers_fail_the_user_gets_a_clear_retryable_error_and_is_not_charged(
    db_client: TestClient, db_app: FastAPI, db_engine: Engine, repo: str
) -> None:
    serve(
        db_app,
        Named("or/free-model", LlmRateLimited("slow", details={"retry_after_seconds": 900})),
        Named("groq/gpt-oss", LlmRateLimited("slow too", details={"retry_after_seconds": 120})),
    )
    r = ask(db_client, repo, QUESTION)
    assert r.status_code == 503 and r.json()["error"]["code"] == "llm_rate_limited"
    assert r.json()["error"]["details"]["retry_after_seconds"] == 120  # the soonest to recover
    assert usage(db_engine) == 0  # a question that could not be answered is refunded


def test_the_ai_review_uses_the_chain_as_well(
    db_client: TestClient, db_app: FastAPI, repo: str
) -> None:
    first = Named("or/free-model", LlmUnavailable("down"))
    second = Named("groq/gpt-oss", '{"summary": "x", "risks": [], "tests": [], "questions": []}')
    serve(db_app, first, second)
    db_client.post(f"/api/repositories/{repo}/impact/review", json={"file": "auth/sessions.py"})
    assert len(first.prompts) == 1 and len(second.prompts) == 1


def test_a_private_repository_never_reaches_the_platforms_providers_even_when_they_fail_over(
    db_client: TestClient,
    db_app: FastAPI,
    fake_gh: Any,
    world: Any,  # noqa: F811
) -> None:
    class Forbidden(FakeChat):
        def complete(self, system: str, user: str, **kw: Any) -> str:  # pragma: no cover
            raise AssertionError("the platform's chat was used for a private repository")

    fake_gh.files = dict(FILES)
    serve(db_app, Forbidden("x"), Forbidden("y"))
    rid = private_repo(db_client, fake_gh)
    # no provider of the owner's own: refused outright, and the platform's chain is not asked
    r = ask(db_client, rid, QUESTION)
    assert r.status_code == 409 and r.json()["error"]["code"] == "providers_required"
    save(db_client, "chat")
    save(db_client, "embedding")
    r = ask(db_client, rid, QUESTION)
    assert r.status_code != 500  # whatever it answers, it was not through the platform's chain


def test_a_question_too_big_for_every_provider_gets_a_clear_413_and_is_not_charged(
    db_client: TestClient, db_app: FastAPI, db_engine: Engine, repo: str
) -> None:
    first, second = Named("or/free-model", answer), Named("groq/gpt-oss", answer)
    chain = FallbackChat(
        [("openrouter", cast(Chat, first)), ("groq", cast(Chat, second))],
        max_request_tokens={"openrouter": 50, "groq": 50},  # absurdly small: nothing fits
    )
    db_app.dependency_overrides[get_llm] = lambda: chain
    r = ask(db_client, repo, QUESTION)
    assert r.status_code == 413 and r.json()["error"]["code"] == "llm_request_too_large"
    assert first.prompts == [] and second.prompts == []
    assert usage(db_engine) == 0
