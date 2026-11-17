from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.core.errors import GithubAuthFailed, GithubUnavailable, ReauthRequired
from app.services.auth.oauth import GitHubOAuth


def make(handler: object) -> GitHubOAuth:
    return GitHubOAuth(
        client_id="cid",
        client_secret="super-secret",
        redirect_uri="http://localhost:3000/api/auth/github/callback",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


def test_authorize_url_has_pkce_and_never_the_secret() -> None:
    url = make(lambda r: httpx.Response(500)).authorize_url("the-state", "the-challenge")
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    assert (
        f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://github.com/login/oauth/authorize"
    )
    assert query["state"] == ["the-state"]
    assert query["code_challenge"] == ["the-challenge"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["client_id"] == ["cid"]
    assert query["redirect_uri"] == ["http://localhost:3000/api/auth/github/callback"]
    assert "super-secret" not in url


def test_exchange_code_posts_secret_in_body_and_parses_expiries() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "access_token": "gho_abc",
                "expires_in": 28800,
                "refresh_token": "ghr_def",
                "refresh_token_expires_in": 15811200,
            },
        )

    tokens = make(handler).exchange_code("the-code", "the-verifier")
    req = seen[0]
    assert req.method == "POST"
    assert "super-secret" not in str(req.url) and "the-code" not in str(req.url)
    body = parse_qs(req.content.decode())
    assert body["client_secret"] == ["super-secret"]
    assert body["code"] == ["the-code"]
    assert body["code_verifier"] == ["the-verifier"]
    assert tokens.access_token == "gho_abc" and tokens.refresh_token == "ghr_def"
    assert tokens.access_expires_at is not None and tokens.refresh_expires_at is not None
    assert tokens.refresh_expires_at > tokens.access_expires_at


def test_exchange_code_without_expiry_fields() -> None:
    tokens = make(lambda r: httpx.Response(200, json={"access_token": "gho_x"})).exchange_code(
        "c", "v"
    )
    assert tokens.refresh_token is None and tokens.access_expires_at is None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"error": "bad_verification_code"}),  # GitHub's "failure"
        httpx.Response(200, json={"scope": "", "token_type": "bearer"}),  # no access token
        httpx.Response(200, json={"access_token": ""}),
    ],
)
def test_exchange_code_failures_are_reported(response: httpx.Response) -> None:
    with pytest.raises(GithubAuthFailed):
        make(lambda r: response).exchange_code("c", "v")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(502),
        httpx.Response(200, text="<html>oops</html>"),
        httpx.Response(200, json=[]),
    ],
)
def test_exchange_code_upstream_problems_are_unavailable(response: httpx.Response) -> None:
    with pytest.raises(GithubUnavailable):
        make(lambda r: response).exchange_code("c", "v")


def test_network_error_is_unavailable() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(GithubUnavailable):
        make(boom).exchange_code("c", "v")


def test_refresh_uses_refresh_grant_and_maps_rejection_to_reauth() -> None:
    seen: list[httpx.Request] = []

    def ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"access_token": "gho_new", "refresh_token": "ghr_new"})

    assert make(ok).refresh("ghr_old").access_token == "gho_new"
    body = parse_qs(seen[0].content.decode())
    assert body["grant_type"] == ["refresh_token"] and body["refresh_token"] == ["ghr_old"]

    with pytest.raises(ReauthRequired):
        make(lambda r: httpx.Response(200, json={"error": "bad_refresh_token"})).refresh("x")


def profile_handler(payload: object, status: int = 200):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer tok"
        return httpx.Response(status, json=payload)

    return handler


def test_profile_success_keeps_only_safe_fields() -> None:
    profile = make(
        profile_handler(
            {
                "id": 42,
                "login": "octo-cat",
                "avatar_url": "https://avatars.githubusercontent.com/u/42",
                "email": "private@example.com",
            }
        )
    ).fetch_profile("tok")
    assert (profile.id, profile.login) == (42, "octo-cat")
    assert profile.avatar_url == "https://avatars.githubusercontent.com/u/42"
    assert not hasattr(profile, "email")


@pytest.mark.parametrize(
    "payload",
    [
        {"login": "x"},  # no id
        {"id": "42", "login": "x"},  # id not an int
        {"id": 1, "login": "bad login!"},
        {"id": 1, "login": "<script>"},
        {"id": 1, "login": "-leading"},
        {"id": 1, "login": "a" * 40},
        {"id": 1},
    ],
)
def test_profile_rejects_unexpected_shapes(payload: dict[str, object]) -> None:
    with pytest.raises(GithubAuthFailed):
        make(profile_handler(payload)).fetch_profile("tok")


def test_profile_drops_non_https_avatar() -> None:
    p = make(
        profile_handler({"id": 1, "login": "a", "avatar_url": "javascript:alert(1)"})
    ).fetch_profile("tok")
    assert p.avatar_url is None


def test_profile_error_statuses() -> None:
    with pytest.raises(GithubAuthFailed):
        make(profile_handler({}, status=401)).fetch_profile("tok")
    with pytest.raises(GithubUnavailable):
        make(profile_handler({}, status=500)).fetch_profile("tok")
