"""Private repositories: who can connect them, who can reach them, and what is refused."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import GithubUnavailable
from app.db.models import Repository, User
from app.services.github.client import GithubRepoInfo
from app.services.indexing import service
from tests.conftest import Persona
from tests.helpers import FakeGitHub

pytestmark = pytest.mark.db

SECRET = "https://github.com/acme/secret"
PUBLIC = "https://github.com/acme/open"


def connect(c: TestClient, url: str = SECRET, **extra: Any) -> Any:
    return c.post("/api/repositories", json={"url": url, **extra})


def private_repo(c: TestClient, fake: FakeGitHub, url: str = SECRET) -> str:
    fake.private_names.add(url.removeprefix("https://github.com/").lower())
    r = connect(c, url)
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def rows(engine: Engine, sql: str, **params: Any) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(text(sql), params))


def age_the_check(engine: Engine, seconds: int = 3600) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE repositories SET access_checked_at = now() - make_interval(secs => :s)"),
            {"s": seconds},
        )


# --- connecting -------------------------------------------------------------------------


def test_visibility_comes_from_github_and_the_owner_gets_a_private_row(
    db_client: TestClient, fake_gh: FakeGitHub, alice: Persona, db_engine: Engine
) -> None:
    fake_gh.private_names.add("acme/secret")
    r = connect(db_client)
    assert r.status_code == 201 and r.json()["is_private"] is True
    row = rows(db_engine, "SELECT is_private, owner_user_id, access_checked_at FROM repositories")[
        0
    ]
    assert row[0] is True and row[1] == alice.id and row[2] is not None
    assert fake_gh.get_repo_calls == ["acme/secret"]

    public = connect(db_client, PUBLIC)
    assert public.json()["is_private"] is False
    assert (
        rows(db_engine, "SELECT owner_user_id FROM repositories WHERE NOT is_private")[0][0] is None
    )


def test_the_client_cannot_claim_a_repository_is_public_or_private(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    fake_gh.private_names.add("acme/secret")
    r = connect(db_client, SECRET, is_private=False, owner_user_id=str(uuid.uuid4()))
    assert r.status_code == 201 and r.json()["is_private"] is True
    r = connect(db_client, PUBLIC, is_private=True)
    assert r.json()["is_private"] is False


def test_a_repository_github_does_not_show_you_cannot_be_connected(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    fake_gh.hidden_names.add("acme/secret")  # private and the app is not installed, or no access
    r = connect(db_client)
    assert r.status_code == 404 and r.json()["error"]["code"] == "repo_not_found"
    assert (
        "GitHub App" in r.json()["error"]["message"] or "accessible" in r.json()["error"]["message"]
    )
    assert rows(db_engine, "SELECT count(*) FROM repositories")[0][0] == 0


def test_connecting_a_private_repository_again_is_idempotent(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = private_repo(db_client, fake_gh)
    again = connect(db_client)
    assert again.status_code == 200 and again.json()["id"] == rid
    assert rows(db_engine, "SELECT count(*) FROM repositories")[0][0] == 1


def test_two_users_with_access_get_separate_private_rows(
    db_client: TestClient,
    other_client: TestClient,
    fake_gh: FakeGitHub,
    alice: Persona,
    bob: Persona,
    db_engine: Engine,
) -> None:
    a = private_repo(db_client, fake_gh)
    b = private_repo(other_client, fake_gh)
    assert a != b  # never shared or deduplicated
    owners = {r[0] for r in rows(db_engine, "SELECT owner_user_id FROM repositories")}
    assert owners == {alice.id, bob.id}
    assert db_client.get(f"/api/repositories/{b}").status_code == 404
    assert other_client.get(f"/api/repositories/{a}").status_code == 404
    assert db_client.get("/api/repositories").json()["total"] == 1


def test_a_public_repository_is_still_shared_between_users(
    db_client: TestClient, other_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    a = connect(db_client, PUBLIC).json()["id"]
    b = connect(other_client, PUBLIC).json()["id"]
    assert a == b and rows(db_engine, "SELECT count(*) FROM repositories")[0][0] == 1


def test_someone_without_access_cannot_reach_a_private_repository_by_connecting_its_name(
    db_client: TestClient, other_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    a = private_repo(db_client, fake_gh)
    # GitHub, asked with bob's token, says it is not visible to him.
    fake_gh.hidden_names.add("acme/secret")
    assert connect(other_client).status_code == 404
    assert other_client.get(f"/api/repositories/{a}/status").status_code == 404


# --- every route, wrong user ------------------------------------------------------------


def test_every_route_treats_another_users_private_repository_as_missing(
    db_client: TestClient, other_client: TestClient, fake_gh: FakeGitHub
) -> None:
    rid = private_repo(db_client, fake_gh)
    base = f"/api/repositories/{rid}"
    routes = [
        ("GET", base, None),
        ("GET", f"{base}/status", None),
        ("GET", f"{base}/files", None),
        ("GET", f"{base}/files/a.py", None),
        ("GET", f"{base}/stats", None),
        ("GET", f"{base}/graph", None),
        ("GET", f"{base}/impact?file=a.py", None),
        ("POST", f"{base}/impact/review", {"file": "a.py"}),
        ("POST", f"{base}/index", None),
        ("POST", f"{base}/index/tick", None),
        ("POST", f"{base}/search", {"query": "secret"}),
        ("POST", f"{base}/chat", {"question": "what is this"}),
        ("DELETE", base, None),
    ]
    for method, url, body in routes:
        r = other_client.request(method, url, json=body)
        assert r.status_code == 404, (method, url, r.status_code, r.text)
        assert r.json()["error"]["code"] == "repo_not_found", (method, url)
    assert db_client.get(base).status_code == 200  # and the owner's repository was not touched


# --- providers ---------------------------------------------------------------------------


def test_a_private_repository_cannot_use_the_platforms_providers(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = private_repo(db_client, fake_gh)
    base = f"/api/repositories/{rid}"
    blocked = [
        ("POST", f"{base}/index", None),
        ("POST", f"{base}/index/tick", None),
        ("POST", f"{base}/search", {"query": "x"}),
        ("POST", f"{base}/search", {"query": "x", "mode": "hybrid"}),
        ("POST", f"{base}/chat", {"question": "what is this"}),
        ("POST", f"{base}/impact/review", {"file": "a.py"}),
    ]
    for method, url, body in blocked:
        r = db_client.request(method, url, json=body)
        assert r.status_code == 409, (url, r.status_code, r.text)
        assert r.json()["error"]["code"] == "providers_required", url
    assert fake_gh.resolve_calls == 0 and fake_gh.tarball_calls == 0  # nothing was fetched
    assert rows(db_engine, "SELECT count(*) FROM snapshots")[0][0] == 0
    # Keyword search sends nothing anywhere, so it is not refused for that reason.
    r = db_client.post(f"{base}/search", json={"query": "x", "mode": "keyword"})
    assert r.json()["error"]["code"] != "providers_required"


def test_indexing_a_private_repository_asks_github_for_private_access(
    db_client: TestClient, fake_gh: FakeGitHub, alice: Persona, db_engine: Engine
) -> None:
    rid = private_repo(db_client, fake_gh)
    with Session(db_engine) as db:
        repo = db.get(Repository, uuid.UUID(rid))
        assert repo is not None
        service.start_indexing(db, repo, None, alice.id, fake_gh, get_settings())
    assert fake_gh.resolve_allow_private == [True]
    public = connect(db_client, PUBLIC).json()["id"]
    with Session(db_engine) as db:
        repo = db.get(Repository, uuid.UUID(public))
        assert repo is not None
        service.start_indexing(db, repo, None, alice.id, fake_gh, get_settings())
    assert fake_gh.resolve_allow_private == [True, False]


# --- access rechecked --------------------------------------------------------------------


def test_the_recheck_is_cached_and_then_asked_again(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = private_repo(db_client, fake_gh)
    assert fake_gh.get_repo_calls == ["acme/secret"]  # the connect itself
    for _ in range(3):
        assert db_client.get(f"/api/repositories/{rid}/status").status_code == 200
    assert fake_gh.get_repo_calls == ["acme/secret"]  # within the window: not asked again

    age_the_check(db_engine, get_settings().private_access_recheck_seconds + 5)
    assert db_client.get(f"/api/repositories/{rid}/status").status_code == 200
    assert fake_gh.get_repo_calls == ["acme/secret", "acme/secret"]
    checked = rows(db_engine, "SELECT access_checked_at FROM repositories")[0][0]
    assert datetime.now(UTC) - checked < timedelta(seconds=30)  # confirmed again


def test_revoked_access_stops_every_route_but_still_allows_disconnecting(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = private_repo(db_client, fake_gh)
    base = f"/api/repositories/{rid}"
    fake_gh.hidden_names.add("acme/secret")  # the app was uninstalled or access removed
    age_the_check(db_engine)

    for method, url, body in [
        ("GET", f"{base}/status", None),
        ("GET", f"{base}/files", None),
        ("GET", f"{base}/graph", None),
        ("POST", f"{base}/index/tick", None),
        ("POST", f"{base}/chat", {"question": "what is this"}),
    ]:
        r = db_client.request(method, url, json=body)
        assert r.status_code == 403 and r.json()["error"]["code"] == "repo_access_lost", url
    assert rows(db_engine, "SELECT access_checked_at FROM repositories")[0][0] is None

    # The owner can still see the record (to understand what happened) and delete the data.
    assert db_client.get(base).status_code == 200
    assert db_client.delete(base).status_code == 204
    assert rows(db_engine, "SELECT count(*) FROM repositories")[0][0] == 0


def test_access_that_cannot_be_confirmed_fails_closed(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = private_repo(db_client, fake_gh)
    age_the_check(db_engine)

    def down(owner: str, name: str) -> Any:
        raise GithubUnavailable("Could not reach GitHub.")

    monkeypatch.setattr(fake_gh, "get_repo", down)
    r = db_client.get(f"/api/repositories/{rid}/status")
    assert r.status_code == 502 and r.json()["error"]["code"] == "github_unavailable"
    assert rows(db_engine, "SELECT access_checked_at FROM repositories")[0][0] is not None


def test_public_repositories_are_never_rechecked(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = connect(db_client, PUBLIC).json()["id"]
    calls = len(fake_gh.get_repo_calls)
    for _ in range(3):
        assert db_client.get(f"/api/repositories/{rid}/status").status_code == 200
    assert len(fake_gh.get_repo_calls) == calls


# --- listing -----------------------------------------------------------------------------


def listing_entry(name: str) -> GithubRepoInfo:
    return GithubRepoInfo("acme", name, "d", "Python", 0, "2026-11-01T10:00:00Z", False, False)


def test_private_repository_listing_and_the_install_link(
    db_client: TestClient, anon_client: TestClient, fake_gh: FakeGitHub, db_app: Any
) -> None:
    fake_gh.private_repos = [listing_entry("secret")]
    body = db_client.get("/api/github/private-repos").json()
    assert [i["name"] for i in body["items"]] == ["secret"] and body["install_url"] is None

    base = get_settings()
    db_app.dependency_overrides[get_settings] = lambda: base.model_copy(
        update={"github_app_slug": "my-app"}
    )
    body = db_client.get("/api/github/private-repos").json()
    assert body["install_url"] == "https://github.com/apps/my-app/installations/new"
    assert body["retention_days"] == base.private_retention_days
    db_app.dependency_overrides[get_settings] = lambda: base.model_copy(
        update={"private_retention_days": 0}
    )
    assert db_client.get("/api/github/private-repos").json()["retention_days"] is None
    assert anon_client.get("/api/github/private-repos").status_code == 401


# --- the database itself -----------------------------------------------------------------


def insert_repo(
    engine: Engine, owner_user: uuid.UUID | None, private: bool, name: str = "secret"
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO repositories (id, owner, name, url, is_private, owner_user_id) "
                "VALUES (:id, 'acme', :n, 'https://github.com/acme/x', :p, :o)"
            ),
            {"id": uuid.uuid4(), "n": name, "p": private, "o": owner_user},
        )


def test_database_rules_for_private_rows(
    db_clean: Engine, alice: Persona, bob: Persona, db_engine: Engine
) -> None:
    insert_repo(db_engine, alice.id, True)
    insert_repo(db_engine, bob.id, True)  # same name, different owner: allowed
    insert_repo(db_engine, None, False)  # and a public one with the same name
    with pytest.raises(IntegrityError):
        insert_repo(db_engine, alice.id, True)  # the same owner twice
    with pytest.raises(IntegrityError):
        insert_repo(db_engine, None, False)  # two public rows with one name
    with pytest.raises(IntegrityError):
        insert_repo(db_engine, None, True, "other")  # private without an owner
    with pytest.raises(IntegrityError):
        insert_repo(db_engine, alice.id, False, "other2")  # public with an owner


def test_deleting_a_user_deletes_their_private_repositories(
    db_client: TestClient, fake_gh: FakeGitHub, alice: Persona, db_engine: Engine
) -> None:
    private_repo(db_client, fake_gh)
    connect(db_client, PUBLIC)
    with Session(db_engine) as db:
        user = db.get(User, alice.id)
        assert user is not None
        db.delete(user)
        db.commit()
    left = rows(db_engine, "SELECT name, is_private FROM repositories")
    assert left == [("open", False)]  # the shared public record stays; the private one is gone
