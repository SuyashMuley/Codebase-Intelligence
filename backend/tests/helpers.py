"""Test doubles and fixtures shared by unit and integration tests."""

import io
import tarfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from app.core.errors import AppError, GithubUnavailable
from app.services.github.client import ByteSource, CommitInfo

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

    def resolve_commit(self, owner: str, name: str, ref: str | None = None) -> CommitInfo:
        self.resolve_calls += 1
        self.last_ref = ref
        if self.resolve_error:
            raise self.resolve_error
        return CommitInfo(sha=self.sha, default_branch=self.default_branch)

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
