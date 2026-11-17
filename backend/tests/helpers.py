"""Test doubles and fixtures shared by unit and integration tests."""

import io
import tarfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from app.core.errors import AppError, GithubAuthFailed, GithubUnavailable
from app.services.auth.oauth import GitHubProfile, TokenSet
from app.services.github.client import ByteSource, CommitInfo, GithubRepoInfo

PREFIX = "owner-repo-abc123/"

DEFAULT_FILES: dict[str, bytes | str] = {
    "README.md": "# project\n",
    "src/app.py": "import os\n\n\ndef main():\n    return os.getcwd()\n",
    "src/util.js": "export const x = 1;\n",
    "web/index.ts": "export function f(): number { return 1; }\n",
    "cmd/main.go": "package main\n\nfunc main() {}\n",
    "node_modules/lib/index.js": "module.exports = 1;\n",
    "assets/logo.png": b"\x89PNG\r\n\x1a\n\x00\x00binary",
    ".env": "SECRET=hunter2\n",
    "src/big.py": "x = 1\n" * 100_000,  # > 256 KB
}
# Kept: app.py, util.js, index.ts, main.go. big.py is recorded as too_large.
DEFAULT_KEPT = {"src/app.py", "src/util.js", "web/index.ts", "cmd/main.go"}


def make_tarball(
    files: dict[str, bytes | str],
    prefix: str = PREFIX,
    extras: Callable[[tarfile.TarFile], None] | None = None,
) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tf:
        top = tarfile.TarInfo(prefix.rstrip("/"))
        top.type = tarfile.DIRTYPE
        tf.addfile(top)
        for name, content in files.items():
            data = content.encode() if isinstance(content, str) else content
            info = tarfile.TarInfo(prefix + name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        if extras:
            extras(tf)
    return buf.getvalue()


class _FailingReader:
    """Serves bytes, then raises once `fail_after` bytes have been read."""

    def __init__(self, data: bytes, fail_after: int) -> None:
        self._data = data
        self._pos = 0
        self._fail_after = fail_after

    def read(self, size: int = -1) -> bytes:
        if self._pos >= self._fail_after:
            raise GithubUnavailable("Connection to GitHub was interrupted.")
        end = self._fail_after if size < 0 else min(self._pos + size, self._fail_after)
        chunk = self._data[self._pos : end]
        self._pos = end
        return chunk


class FakeGitHub:
    """In-memory GitHubGateway. Set `fail_next` / `resolve_error` to inject failures."""

    def __init__(self, files: dict[str, bytes | str] | None = None) -> None:
        self.files = dict(DEFAULT_FILES if files is None else files)
        self.sha = "a" * 40
        self.default_branch = "main"
        self.resolve_calls = 0
        self.tarball_calls = 0
        self.last_ref: str | None = None
        self.resolve_error: AppError | None = None
        self.fail_next: list[AppError] = []
        self.fail_after_fraction: float | None = None
        self.public_repos: dict[str, list[GithubRepoInfo]] = {}
        self.repo_list_error: AppError | None = None
        self.repo_list_logins: list[str] = []

    def resolve_commit(self, owner: str, name: str, ref: str | None = None) -> CommitInfo:
        self.resolve_calls += 1
        self.last_ref = ref
        if self.resolve_error:
            raise self.resolve_error
        return CommitInfo(sha=self.sha, default_branch=self.default_branch)

    def list_public_repos(self, login: str, limit: int = 100) -> list[GithubRepoInfo]:
        self.repo_list_logins.append(login)
        if self.repo_list_error:
            raise self.repo_list_error
        return list(self.public_repos.get(login, []))

    @contextmanager
    def open_tarball(self, owner: str, name: str, sha: str, max_bytes: int) -> Iterator[ByteSource]:
        self.tarball_calls += 1
        if self.fail_next:
            raise self.fail_next.pop(0)
        data = make_tarball(self.files)
        if self.fail_after_fraction is not None:
            yield _FailingReader(data, int(len(data) * self.fail_after_fraction))
        else:
            yield io.BytesIO(data)


class FakeOAuth:
    """In-memory GitHubOAuthGateway. Register codes in `codes`; each code works once."""

    def __init__(self) -> None:
        self.codes: dict[str, GitHubProfile] = {}
        self.used: set[str] = set()
        self.exchange_error: AppError | None = None
        self.refresh_error: AppError | None = None
        self.last_verifier: str | None = None
        self.last_challenge: str | None = None
        self.refresh_calls = 0

    def authorize_url(self, state: str, code_challenge: str) -> str:
        self.last_challenge = code_challenge
        return f"https://github.test/authorize?state={state}&cc={code_challenge}"

    def exchange_code(self, code: str, code_verifier: str) -> TokenSet:
        if self.exchange_error:
            raise self.exchange_error
        if code not in self.codes or code in self.used:
            raise GithubAuthFailed("GitHub rejected the sign-in code.")
        self.used.add(code)
        self.last_verifier = code_verifier
        now = datetime.now(UTC)
        return TokenSet(
            access_token=f"gho_access_{code}",
            refresh_token=f"ghr_refresh_{code}",
            access_expires_at=now + timedelta(hours=8),
            refresh_expires_at=now + timedelta(days=180),
        )

    def refresh(self, refresh_token: str) -> TokenSet:
        self.refresh_calls += 1
        if self.refresh_error:
            raise self.refresh_error
        now = datetime.now(UTC)
        return TokenSet(
            access_token=f"gho_refreshed_{self.refresh_calls}",
            refresh_token=f"ghr_refreshed_{self.refresh_calls}",
            access_expires_at=now + timedelta(hours=8),
            refresh_expires_at=now + timedelta(days=180),
        )

    def fetch_profile(self, access_token: str) -> GitHubProfile:
        code = access_token.removeprefix("gho_access_")
        return self.codes[code]
