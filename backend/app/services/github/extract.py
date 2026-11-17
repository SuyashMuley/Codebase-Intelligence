"""Stream a GitHub tarball and yield the files worth indexing.

Nothing is extracted to disk and nothing is executed. Members are read one at a time from a
streaming gzip reader, with caps on archive size, entry count, per-file size, and kept
source volume, plus a wall-clock deadline.
"""

import hashlib
import tarfile
import time
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import IO, cast

from app.core.errors import FetchTimeout, GithubUnavailable, RepoTooLarge
from app.services.github.client import ByteSource
from app.services.github.filters import (
    RECORDED_SKIP_REASONS,
    classify_path,
    has_generated_header,
    looks_binary,
    normalize_member_path,
)
from app.services.security.secrets import redact


@dataclass(frozen=True)
class FetchLimits:
    max_file_bytes: int
    max_files: int
    max_total_bytes: int
    max_uncompressed_bytes: int
    max_members: int


@dataclass
class FetchStats:
    files_kept: int = 0
    bytes_kept: int = 0
    members: int = 0
    secrets_redacted: int = 0  # secret-looking values replaced before storage
    files_redacted: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def as_dict(self) -> dict[str, object]:
        return {
            "files_kept": self.files_kept,
            "bytes_kept": self.bytes_kept,
            "secrets_redacted": self.secrets_redacted,
            "files_redacted": self.files_redacted,
            "skipped": dict(sorted(self.skipped.items())),
        }


@dataclass(frozen=True)
class ExtractedFile:
    path: str
    language: str | None
    size_bytes: int
    sha256: str
    content: str | None  # None when skipped
    skipped_reason: str | None
    redactions: int = 0  # secret-looking values replaced in `content`

    @property
    def stored(self) -> bool:
        """Whether this file gets a row in `files` (kept, or an informative skip)."""
        return self.skipped_reason is None or (
            self.language is not None and self.skipped_reason in RECORDED_SKIP_REASONS
        )


def extract_files(
    source: ByteSource, limits: FetchLimits, deadline: float, stats: FetchStats
) -> Iterator[ExtractedFile]:
    uncompressed_total = 0
    try:
        with tarfile.open(fileobj=cast(IO[bytes], source), mode="r|gz") as archive:
            for member in archive:
                if time.monotonic() > deadline:
                    raise FetchTimeout("Fetching the repository took too long.")
                stats.members += 1
                if stats.members > limits.max_members:
                    raise RepoTooLarge("Repository archive has too many entries.")
                if not member.isreg():  # directories, symlinks, hardlinks, devices
                    continue
                uncompressed_total += member.size
                if uncompressed_total > limits.max_uncompressed_bytes:
                    raise RepoTooLarge("Repository is too large once uncompressed.")

                path = normalize_member_path(member.name)
                if path is None:
                    stats.skip("unsafe_path")
                    continue
                language, reason = classify_path(path)
                if reason is None and member.size > limits.max_file_bytes:
                    reason = "too_large"

                if reason is not None:
                    # Content is never read for path-level skips; tarfile skips the data.
                    stats.skip(reason)
                    skipped = ExtractedFile(path, language, member.size, "", None, reason)
                    if skipped.stored:
                        yield skipped
                    continue

                handle = archive.extractfile(member)
                data = handle.read() if handle else b""
                digest = hashlib.sha256(data).hexdigest()
                text, reason = _decode(data)
                redactions = 0
                if text is not None:
                    # Secrets are replaced before anything is stored or yielded, so a raw
                    # credential never reaches the database, the index, or a log.
                    redacted = redact(text)
                    text, redactions = redacted.text, redacted.count
                if reason is not None:
                    stats.skip(reason)
                    skipped = ExtractedFile(path, language, len(data), digest, None, reason)
                    if skipped.stored:
                        yield skipped
                    continue

                stats.files_kept += 1
                stats.bytes_kept += len(data)
                if redactions:
                    stats.secrets_redacted += redactions
                    stats.files_redacted += 1
                if stats.files_kept > limits.max_files:
                    raise RepoTooLarge(f"Repository has more than {limits.max_files} source files.")
                if stats.bytes_kept > limits.max_total_bytes:
                    raise RepoTooLarge(
                        f"Repository source exceeds {limits.max_total_bytes // (1024 * 1024)} MB."
                    )
                yield ExtractedFile(path, language, len(data), digest, text, None, redactions)
    except (tarfile.TarError, EOFError, zlib.error, OSError):
        raise GithubUnavailable("The repository archive could not be read.") from None


def _decode(data: bytes) -> tuple[str | None, str | None]:
    if looks_binary(data):
        return None, "binary"
    try:
        text = data.decode("utf-8-sig")  # also drops a leading byte order mark
    except UnicodeDecodeError:
        return None, "binary"
    # One line ending everywhere: line numbers are unchanged, but chunks and the viewer agree.
    text = text.replace("\r\n", "\n")
    if has_generated_header(text):
        return None, "generated"
    return text, None
