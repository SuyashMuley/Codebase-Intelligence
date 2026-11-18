"""Minimal GitHub REST client: resolve a commit and download its tarball.

Security notes:
- The token is sent only to the API host. The tarball redirect is followed manually, must
  point at codeload.github.com over https, and is fetched without credentials.
- Downloads are streamed and capped; nothing is written to disk and nothing is executed.
"""

import copy
import re
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote, urlsplit

import httpx

from app.core.errors import (
    GithubRateLimited,
    GithubUnavailable,
    ReauthRequired,
    RepoNotFound,
    RepoTooLarge,
)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_CODELOAD_HOST = "codeload.github.com"
_MAX_RATE_LIMIT_WAIT = 3600
_USER_AGENT = "codebase-intelligence"


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    default_branch: str


@dataclass(frozen=True)
class RepoMeta:
    """What GitHub says about one repository, as seen with the caller's own token."""

    private: bool
    default_branch: str


@dataclass(frozen=True)
class GithubRepoInfo:
    """A public repository as shown in the picker."""

    owner: str
    name: str
    description: str | None
    language: str | None
    stars: int
    pushed_at: str | None
    fork: bool
    archived: bool


class ByteSource(Protocol):
    """Anything tarfile can stream from."""

    def read(self, size: int = -1) -> bytes: ...


class GitHubGateway(Protocol):
    """What the indexing code needs from GitHub. Tests substitute a fake."""

    def get_repo(self, owner: str, name: str) -> RepoMeta: ...

    def resolve_commit(
        self, owner: str, name: str, ref: str | None = None, allow_private: bool = False
    ) -> CommitInfo: ...

    def list_private_repos(self, limit: int = 100) -> list[GithubRepoInfo]: ...

    def list_public_repos(self, login: str, limit: int = 100) -> list[GithubRepoInfo]: ...

    def open_tarball(
        self, owner: str, name: str, sha: str, max_bytes: int
    ) -> AbstractContextManager[ByteSource]: ...


class _CappedReader:
    """File-like view over a streamed (compressed) body that enforces a byte cap."""

    def __init__(self, chunks: Iterator[bytes], max_bytes: int) -> None:
        self._chunks = chunks
        self._max = max_bytes
        self._total = 0
        self._buf = b""

    def read(self, size: int = -1) -> bytes:
        try:
            while size < 0 or len(self._buf) < size:
                chunk = next(self._chunks)
                self._total += len(chunk)
                if self._total > self._max:
                    raise RepoTooLarge(f"Repository archive exceeds the {_human(self._max)} limit.")
                self._buf += chunk
        except StopIteration:
            pass
        except httpx.HTTPError:
            raise GithubUnavailable("Connection to GitHub was interrupted.") from None
        if size < 0:
            out, self._buf = self._buf, b""
        else:
            out, self._buf = self._buf[:size], self._buf[size:]
        return out


class GitHubClient:
    def __init__(
        self,
        token: str | None,
        api_base: str = "https://api.github.com",
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        # The token is added per request (see _auth_headers), so one shared client can serve
        # many users, each with their own token.
        self._token = token
        self._token_is_user = False
        self._api = httpx.Client(
            base_url=api_base,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": _USER_AGENT,
            },
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )
        # Separate client: never carries the token.
        self._download = httpx.Client(
            headers={"User-Agent": _USER_AGENT},
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )

    def bind(self, user_token: str) -> "GitHubClient":
        """A view of this client that authenticates as one user. Shares the connection pool."""
        bound = copy.copy(self)
        bound._token = user_token
        bound._token_is_user = True
        return bound

    def _auth_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = dict(extra or {})
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def close(self) -> None:
        self._api.close()
        self._download.close()

    # --- errors -------------------------------------------------------------------------

    def _raise_for_response(self, resp: httpx.Response) -> None:
        status = resp.status_code
        if status < 400:
            return
        if status == 401:
            if self._token_is_user:
                raise ReauthRequired("GitHub rejected your authorization. Please sign in again.")
            raise GithubUnavailable("GitHub rejected the server's access token.")
        remaining = resp.headers.get("x-ratelimit-remaining")
        if status in (403, 429) and (remaining == "0" or "retry-after" in resp.headers):
            raise GithubRateLimited(
                "GitHub API rate limit reached.",
                details={"retry_after_seconds": _retry_after(resp)},
            )
        if status in (403, 404, 409, 410, 422, 451):
            raise RepoNotFound(
                "Repository or ref was not found, is empty, or is not accessible to you. "
                "For a private repository, the GitHub App must be installed on it."
            )
        raise GithubUnavailable(f"GitHub returned an unexpected error (HTTP {status}).")

    def _get(self, path: str, headers: dict[str, str] | None = None) -> httpx.Response:
        try:
            resp = self._api.get(path, headers=self._auth_headers(headers))
        except httpx.HTTPError:
            raise GithubUnavailable("Could not reach GitHub.") from None
        if resp.status_code != 302:
            self._raise_for_response(resp)
        return resp

    # --- API ----------------------------------------------------------------------------

    def get_repo(self, owner: str, name: str) -> RepoMeta:
        """Visibility and default branch, read from GitHub with the caller's token. This is the
        only source of "is it private": the browser's claim is never used. For a private
        repository GitHub answers 404 unless the caller has access and the GitHub App is
        installed on it."""
        _check_names(owner, name)
        repo = self._get(f"/repos/{owner}/{name}").json()
        if not isinstance(repo, dict) or not isinstance(repo.get("default_branch"), str):
            raise GithubUnavailable("GitHub returned an unexpected repository description.")
        return RepoMeta(private=repo.get("private") is True, default_branch=repo["default_branch"])

    def resolve_commit(
        self, owner: str, name: str, ref: str | None = None, allow_private: bool = False
    ) -> CommitInfo:
        _check_names(owner, name)
        repo = self._get(f"/repos/{owner}/{name}").json()
        if repo.get("private") and not allow_private:
            raise RepoNotFound("Repository was not found or is not public.")
        default_branch = str(repo["default_branch"])
        target = quote(ref or default_branch, safe="/")
        resp = self._get(
            f"/repos/{owner}/{name}/commits/{target}",
            headers={"Accept": "application/vnd.github.sha"},
        )
        sha = resp.text.strip()
        if not _SHA_RE.match(sha):
            raise GithubUnavailable("GitHub returned an unexpected commit identifier.")
        return CommitInfo(sha=sha, default_branch=default_branch)

    def list_public_repos(self, login: str, limit: int = 100) -> list[GithubRepoInfo]:
        """A user's public repositories they own, most recently pushed first.

        Uses the public listing endpoint, so it needs no repository permissions and can never
        return a private repository. Entries that do not look like normal owner/name pairs are
        dropped rather than passed on to the browser.
        """
        if not _LOGIN_RE.match(login):
            raise ValueError("not a valid GitHub login")
        per_page = max(1, min(limit, 100))
        data = self._get(
            f"/users/{login}/repos?type=owner&sort=pushed&direction=desc&per_page={per_page}"
        ).json()
        if not isinstance(data, list):
            raise GithubUnavailable("GitHub returned an unexpected repository list.")

        repos: list[GithubRepoInfo] = []
        for item in data:
            if isinstance(item, dict) and item.get("private") is True:
                continue
            info = _repo_info(item)
            if info is not None:
                repos.append(info)
        return repos

    def list_private_repos(self, limit: int = 100) -> list[GithubRepoInfo]:
        """Private repositories the signed-in user can reach through the GitHub App.

        A GitHub App user token sees a repository only if the user has access to it and the app
        is installed on it, so this is exactly the set that can be connected. Needs the user's
        token (a platform token would list nothing useful and is refused)."""
        if not self._token_is_user:
            raise ReauthRequired("Please sign in with GitHub.")
        data = self._get("/user/installations?per_page=20").json()
        installations = data.get("installations") if isinstance(data, dict) else None
        if not isinstance(installations, list):
            raise GithubUnavailable("GitHub returned an unexpected installation list.")
        repos: list[GithubRepoInfo] = []
        for inst in installations[:10]:
            inst_id = inst.get("id") if isinstance(inst, dict) else None
            if not isinstance(inst_id, int):
                continue
            page = self._get(f"/user/installations/{inst_id}/repositories?per_page=100").json()
            items = page.get("repositories") if isinstance(page, dict) else None
            if not isinstance(items, list):
                continue
            for item in items:
                info = _repo_info(item)
                if info is not None and isinstance(item, dict) and item.get("private") is True:
                    repos.append(info)
                if len(repos) >= limit:
                    return repos
        return repos

    @contextmanager
    def open_tarball(self, owner: str, name: str, sha: str, max_bytes: int) -> Iterator[ByteSource]:
        if not _SHA_RE.match(sha):
            raise ValueError("sha must be a full 40-character commit hash")
        resp = self._get(f"/repos/{owner}/{name}/tarball/{sha}")
        if resp.status_code != 302:
            raise GithubUnavailable("GitHub did not redirect to the archive.")
        location = urlsplit(resp.headers.get("location", ""))
        if location.scheme != "https" or location.hostname != _CODELOAD_HOST:
            raise GithubUnavailable("GitHub redirected to an unexpected host.")
        try:
            with self._download.stream("GET", location.geturl()) as body:
                self._raise_for_response(body)
                if body.status_code != 200:
                    raise GithubUnavailable("GitHub returned an unexpected archive response.")
                declared = body.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise RepoTooLarge(f"Repository archive exceeds the {_human(max_bytes)} limit.")
                yield _CappedReader(body.iter_raw(), max_bytes)
        except httpx.HTTPError:
            raise GithubUnavailable("Connection to GitHub was interrupted.") from None


def _check_names(owner: str, name: str) -> None:
    if not _LOGIN_RE.match(owner) or not _REPO_NAME_RE.match(name) or name in {".", ".."}:
        raise ValueError("not a valid GitHub repository name")


def _repo_info(item: object) -> GithubRepoInfo | None:
    """One entry of a GitHub repository list, or None if it does not look like a normal
    owner/name pair (such entries are dropped rather than passed on to the browser)."""
    if not isinstance(item, dict):
        return None
    owner = (item.get("owner") or {}).get("login")
    name = item.get("name")
    if not (
        isinstance(owner, str)
        and isinstance(name, str)
        and _LOGIN_RE.match(owner)
        and _REPO_NAME_RE.match(name)
    ):
        return None
    description = item.get("description")
    language = item.get("language")
    stars = item.get("stargazers_count")
    pushed = item.get("pushed_at")
    return GithubRepoInfo(
        owner=owner,
        name=name,
        description=description[:200] if isinstance(description, str) else None,
        language=language[:40] if isinstance(language, str) else None,
        stars=stars if isinstance(stars, int) else 0,
        pushed_at=pushed if isinstance(pushed, str) else None,
        fork=item.get("fork") is True,
        archived=item.get("archived") is True,
    )


def _human(n: int) -> str:
    return f"{n // (1024 * 1024)} MB" if n >= 1024 * 1024 else f"{max(1, n // 1024)} KB"


def _retry_after(resp: httpx.Response) -> int:
    header = resp.headers.get("retry-after")
    if header and header.isdigit():
        return min(int(header), _MAX_RATE_LIMIT_WAIT)
    reset = resp.headers.get("x-ratelimit-reset")
    if reset and reset.isdigit():
        return max(1, min(int(reset) - int(time.time()), _MAX_RATE_LIMIT_WAIT))
    return 60
