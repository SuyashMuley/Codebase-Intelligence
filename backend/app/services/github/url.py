"""Strict parsing of GitHub repository URLs.

Only ``https://github.com/<owner>/<name>`` is accepted (optionally ``www.``, a trailing
slash, or a ``.git`` suffix). Anything else is rejected rather than "fixed up", which keeps
the fetcher from ever being pointed at a different host (SSRF) or a surprising path.
"""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.core.errors import InvalidGithubUrl

MAX_URL_LENGTH = 200
_ALLOWED_HOSTS = {"github.com", "www.github.com"}
# GitHub usernames/orgs: alphanumerics and single hyphens, up to 39 chars.
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.name}"


def _reject(reason: str) -> InvalidGithubUrl:
    return InvalidGithubUrl(
        f"Not a valid GitHub repository URL: {reason}. Expected https://github.com/<owner>/<repo>.",
        details={"reason": reason},
    )


def parse_github_url(raw: str) -> RepoRef:
    url = raw.strip()
    if not url:
        raise _reject("empty")
    if len(url) > MAX_URL_LENGTH:
        raise _reject("too long")
    if any(ch.isspace() or ord(ch) < 0x20 for ch in url) or "\\" in url or "%" in url:
        raise _reject("contains whitespace, control, backslash or percent-encoded characters")

    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise _reject("malformed URL") from None

    if parts.scheme != "https":
        raise _reject("must use https")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise _reject("credentials are not allowed")
    if port is not None:
        raise _reject("custom ports are not allowed")
    if (parts.hostname or "") not in _ALLOWED_HOSTS:
        raise _reject("host must be github.com")
    if parts.query or parts.fragment:
        raise _reject("query strings and fragments are not allowed")

    segments = parts.path.strip("/").split("/")
    if len(segments) != 2 or not all(segments):
        raise _reject("path must be exactly <owner>/<repo>")
    owner, name = segments
    name = name.removesuffix(".git")

    if not _OWNER_RE.match(owner):
        raise _reject("invalid owner name")
    if not _NAME_RE.match(name) or name in {".", ".."}:
        raise _reject("invalid repository name")

    return RepoRef(owner=owner, name=name)
