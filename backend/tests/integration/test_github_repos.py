import pytest
from fastapi.testclient import TestClient

from app.core.errors import GithubRateLimited, ReauthRequired
from app.services.github.client import GithubRepoInfo
from tests.helpers import FakeGitHub

pytestmark = pytest.mark.db


def info(owner: str, name: str, **over: object) -> GithubRepoInfo:
    base: dict[str, object] = {
        "owner": owner,
        "name": name,
        "description": "d",
        "language": "Go",
        "stars": 3,
        "pushed_at": "2026-01-01T00:00:00Z",
        "fork": False,
        "archived": False,
    }
    base.update(over)
    return GithubRepoInfo(**base)  # type: ignore[arg-type]


def test_requires_sign_in(anon_client: TestClient) -> None:
    r = anon_client.get("/api/github/repos")
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthenticated"


def test_lists_the_callers_public_repos(db_client: TestClient, fake_gh: FakeGitHub) -> None:
    fake_gh.public_repos["alice"] = [info("alice", "one"), info("alice", "two", fork=True)]
    r = db_client.get("/api/github/repos")
    assert r.status_code == 200
    assert [(i["owner"], i["name"], i["fork"]) for i in r.json()["items"]] == [
        ("alice", "one", False),
        ("alice", "two", True),
    ]
    assert set(r.json()["items"][0]) == {
        "owner",
        "name",
        "description",
        "language",
        "stars",
        "pushed_at",
        "fork",
        "archived",
    }


def test_each_user_only_gets_their_own_login_queried(
    db_client: TestClient, other_client: TestClient, fake_gh: FakeGitHub
) -> None:
    fake_gh.public_repos["alice"] = [info("alice", "a-repo")]
    fake_gh.public_repos["bob"] = [info("bob", "b-repo")]
    assert [i["name"] for i in db_client.get("/api/github/repos").json()["items"]] == ["a-repo"]
    assert [i["name"] for i in other_client.get("/api/github/repos").json()["items"]] == ["b-repo"]
    # The login always comes from the session, never from a request parameter.
    assert fake_gh.repo_list_logins == ["alice", "bob"]
    db_client.get("/api/github/repos", params={"login": "bob", "user": "bob"})
    assert fake_gh.repo_list_logins[-1] == "alice"


def test_empty_list_is_a_normal_answer(db_client: TestClient) -> None:
    r = db_client.get("/api/github/repos")
    assert r.status_code == 200 and r.json() == {"items": []}


def test_github_problems_are_reported_not_hidden(
    db_client: TestClient, fake_gh: FakeGitHub
) -> None:
    fake_gh.repo_list_error = GithubRateLimited("slow", details={"retry_after_seconds": 30})
    r = db_client.get("/api/github/repos")
    assert r.status_code == 503 and r.json()["error"]["code"] == "github_rate_limited"
    fake_gh.repo_list_error = ReauthRequired("Please sign in again.")
    r = db_client.get("/api/github/repos")
    assert r.status_code == 401 and r.json()["error"]["code"] == "reauth_required"
