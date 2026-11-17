"""Decide which archive members are worth indexing. Pure functions, no I/O."""

import re
from pathlib import PurePosixPath

# extension -> language
LANGUAGE_BY_EXT: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
}

# Directories that hold third-party, build or tool output rather than the project's own code.
VENDORED_DIRS = frozenset(
    {
        "node_modules", "vendor", "third_party", "thirdparty", "bower_components",
        "dist", "build", "out", ".git", ".hg", ".svn", "__pycache__", ".venv", "venv",
        "site-packages", ".tox", ".next", ".nuxt", "target", "coverage", ".idea", ".vscode",
    }
)  # fmt: skip

_GENERATED_NAME_SUFFIXES = (
    ".min.js", ".bundle.js", ".pb.go", ".pb.gw.go", "_pb2.py", "_pb2_grpc.py",
    "_generated.go", ".generated.ts", ".generated.js", ".d.ts",
)  # fmt: skip

_SECRET_NAME_RE = re.compile(
    r"^(\.env(\..*)?|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?|\.npmrc|\.pypirc|\.netrc|\.htpasswd"
    r"|credentials(\..*)?|secrets?\.(json|ya?ml|toml)|.*\.(pem|key|p12|pfx|jks|keystore|tfstate))$",
    re.IGNORECASE,
)

MAX_PATH_LENGTH = 500


def normalize_member_path(name: str) -> str | None:
    """Strip the archive's top-level directory and reject unsafe paths.

    GitHub tarballs nest everything under ``<owner>-<repo>-<sha>/``. Returns None for any
    path that is absolute, contains ``..``/empty segments, control characters or backslashes.
    """
    if not name or len(name) > MAX_PATH_LENGTH or "\\" in name or name.startswith("/"):
        return None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        return None
    parts = name.split("/")
    if len(parts) < 2:
        return None
    rest = parts[1:]
    if any(p in ("", ".", "..") for p in rest):
        return None
    return "/".join(rest)


def detect_language(path: str) -> str | None:
    return LANGUAGE_BY_EXT.get(PurePosixPath(path).suffix.lower())


def classify_path(path: str) -> tuple[str | None, str | None]:
    """Return (language, skip_reason) using only the path. skip_reason None means keep going."""
    parts = path.split("/")
    base = parts[-1]
    language = detect_language(path)
    if _SECRET_NAME_RE.match(base):
        return language, "secret_file"
    if any(p in VENDORED_DIRS for p in parts[:-1]):
        return language, "vendored"
    if base.lower().endswith(_GENERATED_NAME_SUFFIXES):
        return language, "generated"
    if language is None:
        return None, "unsupported"
    return language, None


def looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:8000]


def has_generated_header(text: str) -> bool:
    head = text[:1024]
    return ("Code generated" in head and "DO NOT EDIT" in head) or "@generated" in head


# Skipped files with a supported language are still recorded (without content) so the
# explorer can show why they are missing. Vendored and unsafe paths are only counted.
RECORDED_SKIP_REASONS = frozenset({"too_large", "generated", "binary", "secret_file"})
