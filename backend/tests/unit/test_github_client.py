import httpx
import pytest

from app.core.errors import (
    GithubRateLimited,
    GithubUnavailable,
    ReauthRequired,
    RepoNotFound,
    RepoTooLarge,
)
from app.services.github.client import GitHubClient

SHA = "b" * 40
CODELOAD = "https://codeload.github.com/o/n/legacy.tar.gz/" + SHA


def make_client(handler: httpx.MockTransport | object, token: str | None = "tkn") -> GitHubClient:
    return GitHubClient(token=token, transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def api_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/repos/o/n":
        return httpx.Response(200, json={"default_branch": "main", "private": False})
    if path.startswith("/repos/o/n/commits/"):
        return httpx.Response(200, text=SHA + "\n")
    return httpx.Response(404, json={"message": "Not Found"})


def test_resolve_commit_default_branch_and_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return api_handler(request)

    info = make_client(handler).resolve_commit("o", "n")
    assert info.sha == SHA
    assert info.default_branch == "main"
    assert seen[0].headers["authorization"] == "Bearer tkn"
    assert seen[1].url.path == "/repos/o/n/commits/main"
    assert seen[1].headers["accept"] == "application/vnd.github.sha"


def test_resolve_commit_explicit_ref_with_slash() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return api_handler(request)

    make_client(handler).resolve_commit("o", "n", ref="feature/x")
    assert paths[-1] == "/repos/o/n/commits/feature/x"


def test_no_token_means_no_authorization_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return api_handler(request)

    make_client(handler, token=None).resolve_commit("o", "n")
    assert all("authorization" not in r.headers for r in seen)


@pytest.mark.parametrize("status", [403, 404, 409, 422, 451])
def test_not_found_like_statuses(status: int) -> None:
    client = make_client(lambda r: httpx.Response(status, json={"message": "x"}))
    with pytest.raises(RepoNotFound):
        client.resolve_commit("o", "n")


def test_private_repository_is_reported_as_not_found() -> None:
    client = make_client(
        lambda r: httpx.Response(200, json={"default_branch": "main", "private": True})
    )
    with pytest.raises(RepoNotFound):
        client.resolve_commit("o", "n")


def test_rate_limit_uses_reset_header() -> None:
    import time

    reset = str(int(time.time()) + 120)
    client = make_client(
        lambda r: httpx.Response(
            403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset}
        )
    )
    with pytest.raises(GithubRateLimited) as exc:
        client.resolve_commit("o", "n")
    assert 100 <= exc.value.details["retry_after_seconds"] <= 120
    assert exc.value.retryable


def test_rate_limit_prefers_retry_after_and_is_capped() -> None:
    client = make_client(lambda r: httpx.Response(429, headers={"retry-after": "999999"}))
    with pytest.raises(GithubRateLimited) as exc:
        client.resolve_commit("o", "n")
    assert exc.value.details["retry_after_seconds"] == 3600


def test_server_error_and_network_error_are_unavailable() -> None:
    with pytest.raises(GithubUnavailable):
        make_client(lambda r: httpx.Response(502)).resolve_commit("o", "n")

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(GithubUnavailable):
        make_client(boom).resolve_commit("o", "n")


def test_unexpected_commit_identifier_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/o/n":
            return httpx.Response(200, json={"default_branch": "main", "private": False})
        return httpx.Response(200, text="<html>not a sha</html>")

    with pytest.raises(GithubUnavailable):
        make_client(handler).resolve_commit("o", "n")


def tarball_handler(body: bytes, location: str = CODELOAD, headers: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "api.github.com":
            return httpx.Response(302, headers={"location": location})
        # A real byte stream (not `content=`) so it is genuinely streamed, not pre-read.
        return httpx.Response(200, stream=httpx.ByteStream(body), headers=headers or {})

    return handler, seen


def test_tarball_streams_and_does_not_leak_token_to_codeload() -> None:
    handler, seen = tarball_handler(b"hello tarball")
    with make_client(handler).open_tarball("o", "n", SHA, max_bytes=1024) as src:
        assert src.read() == b"hello tarball"
    api_req, dl_req = seen
    assert api_req.headers["authorization"] == "Bearer tkn"
    assert "authorization" not in dl_req.headers
    assert dl_req.url.host == "codeload.github.com"


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/o/n.tar.gz",
        "http://codeload.github.com/o/n.tar.gz",  # not https
        "https://codeload.github.com.evil.example/x",
        "https://169.254.169.254/latest/meta-data",
        "",
    ],
)
def test_tarball_redirect_to_unexpected_host_is_refused(location: str) -> None:
    handler, seen = tarball_handler(b"x", location=location)
    with (
        pytest.raises(GithubUnavailable),
        make_client(handler).open_tarball("o", "n", SHA, max_bytes=1024),
    ):
        pass
    assert all(r.url.host == "api.github.com" for r in seen)  # never fetched the bad URL


def test_tarball_declared_size_over_cap_is_rejected_before_reading() -> None:
    handler, _ = tarball_handler(b"x" * 10, headers={"content-length": "5000"})
    with (
        pytest.raises(RepoTooLarge),
        make_client(handler).open_tarball("o", "n", SHA, max_bytes=1000),
    ):
        pass


def test_tarball_streaming_cap_enforced_without_content_length() -> None:
    handler, _ = tarball_handler(b"x" * 5000)
    with make_client(handler).open_tarball("o", "n", SHA, max_bytes=1000) as src:
        with pytest.raises(RepoTooLarge):
            src.read()


def test_open_tarball_requires_full_sha() -> None:
    handler, _ = tarball_handler(b"x")
    with (
        pytest.raises(ValueError),
        make_client(handler).open_tarball("o", "n", "main", max_bytes=10),
    ):
        pass


def test_bound_client_uses_the_users_token_and_leaves_the_original_alone() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return api_handler(request)

    platform = make_client(handler, token="platform-token")
    platform.bind("user-token").resolve_commit("o", "n")
    platform.resolve_commit("o", "n")
    assert seen[0].headers["authorization"] == "Bearer user-token"
    assert seen[-1].headers["authorization"] == "Bearer platform-token"


def test_rejected_user_token_asks_for_reauth_but_platform_token_does_not() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    client = make_client(handler, token="platform-token")
    with pytest.raises(ReauthRequired):
        client.bind("user-token").resolve_commit("o", "n")
    with pytest.raises(GithubUnavailable):  # operator problem, not the user's
        client.resolve_commit("o", "n")


def test_user_token_never_goes_to_codeload() -> None:
    handler, seen = tarball_handler(b"data")
    with (
        make_client(handler, token=None)
        .bind("user-token")
        .open_tarball("o", "n", SHA, max_bytes=1024) as src
    ):
        assert src.read() == b"data"
    assert seen[0].headers["authorization"] == "Bearer user-token"
    assert "authorization" not in seen[1].headers
