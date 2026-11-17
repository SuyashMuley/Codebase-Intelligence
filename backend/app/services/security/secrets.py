"""Find and redact secret-looking values in source text before it is stored.

Design rules:
- The line structure is preserved exactly (a secret never changes line numbers), so citations
  into the original file stay valid.
- Findings record only the kind and the line, never the value.
- Rules are deliberately high-precision. A redaction that damages ordinary code is worse than
  a missed low-confidence match, so generic rules require a quoted, high-entropy value and
  skip obvious placeholders.
"""

import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import unquote


@dataclass(frozen=True)
class Finding:
    kind: str
    line: int  # 1-based


@dataclass(frozen=True)
class Redaction:
    text: str
    findings: tuple[Finding, ...]

    @property
    def count(self) -> int:
        return len(self.findings)


def _placeholder(kind: str) -> str:
    return f"[REDACTED:{kind}]"


def _entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = Counter(value)
    total = len(value)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


_PLACEHOLDER_WORDS = (
    "example", "sample", "placeholder", "changeme", "change_me", "your", "xxxx", "dummy",
    "fake", "redacted", "todo", "insert", "replace", "<", "${", "{{", "%(", "process.env",
    "os.environ", "getenv", "null", "none", "undefined",
)  # fmt: skip


# lowercase words (and numbers) joined by separators: "invalid-token", "valid:token", "a_b_c"
_SLUG = re.compile(r"^[a-z][a-z0-9]*(?:[-_:./][a-z0-9]+)+$")


def _looks_like_real_secret(value: str) -> bool:
    """Real credentials are random-looking. Words, slugs, paths, URLs and placeholders are not,
    and redacting them would damage ordinary code (test fixtures are full of `token: 'x-token'`)."""
    lowered = value.lower()
    if lowered.startswith("[redacted"):
        return False
    if any(word in lowered for word in _PLACEHOLDER_WORDS):
        return False
    if len(value) < 12 or " " in value or "\t" in value:
        return False
    if value.startswith(("/", "./", "../", "~")) or "://" in value:
        return False  # a path or URL, not a credential
    if len(set(value)) < 4 or _SLUG.match(value):
        return False
    alnum = [c for c in value if c.isalnum()]
    classes = sum(
        (
            any(c.islower() for c in alnum),
            any(c.isupper() for c in alnum),
            any(c.isdigit() for c in alnum),
        )
    )
    if classes < 2:  # all-lowercase or all-digit text reads as a word, not a key
        return False
    return _entropy(value) >= 3.0


_URL_PASSWORD_PLACEHOLDERS = frozenset(
    {"pass", "password", "passwd", "pwd", "secret", "pw", "admin", "user", "test", "foo", "bar",
     "baz", "root", "guest", "none", "null", "hunter2", "mypassword", "yourpassword"}
)  # fmt: skip
_PASSWORDISH = re.compile(r"[a-z0-9 _.\-]*(?:pass|secret|pwd)[a-z0-9 _.\-]*")


def _url_password_is_real(value: str) -> bool:
    """Skip the passwords that documentation and URL-parsing tests use (`user:pass@host`)."""
    if value.startswith(("[", "{", "<", "$", "%")):  # [secure], {PASSWORD}, ${PASSWORD}, %s
        return False
    lowered = unquote(value).lower()
    if any(
        w in lowered
        for w in (
            "example",
            "xxxx",
            "your",
            "placeholder",
            "changeme",
            "dummy",
            "fake",
            "sample",
            "redacted",
        )
    ):
        return False
    if lowered in _URL_PASSWORD_PLACEHOLDERS:
        return False
    # A password-looking word with no case mix or symbols ("password123", "a secret").
    return not (value == value.lower() and _PASSWORDISH.fullmatch(lowered))


@dataclass(frozen=True)
class _Rule:
    kind: str
    pattern: re.Pattern[str]
    group: int = 0  # which group to replace (0 = the whole match)
    accept: Callable[[str], bool] | None = None


def _rx(p: str, flags: int = 0) -> re.Pattern[str]:
    return re.compile(p, flags)


_RULES: tuple[_Rule, ...] = (
    _Rule("aws_access_key", _rx(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA)[0-9A-Z]{16}\b")),
    _Rule("github_token", _rx(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b")),
    _Rule("github_token", _rx(r"\bgithub_pat_[A-Za-z0-9_]{50,255}\b")),
    _Rule("slack_token", _rx(r"\bxox[abposr]-[A-Za-z0-9-]{10,200}\b")),
    _Rule("stripe_key", _rx(r"\b[sr]k_live_[0-9A-Za-z]{20,}\b")),
    _Rule("google_api_key", _rx(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    _Rule("anthropic_key", _rx(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    _Rule("openrouter_key", _rx(r"\bsk-or-v1-[A-Za-z0-9]{32,}")),
    _Rule("openai_key", _rx(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_]{32,}\b")),
    _Rule("huggingface_token", _rx(r"\bhf_[A-Za-z0-9]{34,}\b")),
    _Rule(
        "jwt",
        _rx(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    ),
    # scheme://user:PASSWORD@host: redact only the password
    _Rule(
        "url_password",
        _rx(r"\b[a-z][a-z0-9+.\-]{1,20}://[^\s:/@'\"<>`]{1,64}:([^\s@'\"<>/`]{3,128})@"),
        group=1,
        accept=lambda v: _url_password_is_real(v),
    ),
    # secret_key = "high entropy value" (quoted, so ordinary expressions are never touched)
    _Rule(
        "generic_secret",
        _rx(
            r"""(?ix)
            \b[\w.\-]*
            (?:password|passwd|pwd|secret|api[_\-]?key|apikey|access[_\-]?token|auth[_\-]?token
               |private[_\-]?key|client[_\-]?secret|token)
            [\w.\-]*["']?\]?\s*[:=]\s*
            (?P<q>["'])(?P<v>[^"'\n]{8,200})(?P=q)
            """
        ),
        group=0,  # replaced manually: only the value inside the quotes
        accept=None,
    ),
)

_PEM_BLOCK = re.compile(
    r"-----BEGIN (?P<label>[A-Z0-9 ]*PRIVATE KEY)-----.*?-----END (?P=label)-----", re.DOTALL
)
_PEM_DANGLING = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[^\n]*")


def _redact_pem(text: str, findings: list[Finding]) -> str:
    def block(m: re.Match[str]) -> str:
        start_line = text.count("\n", 0, m.start()) + 1
        findings.append(Finding("private_key", start_line))
        n_lines = m.group(0).count("\n")
        # One placeholder, then empty lines so the line count is unchanged.
        return _placeholder("private_key") + "\n" * n_lines

    text = _PEM_BLOCK.sub(block, text)

    def dangling(m: re.Match[str]) -> str:
        findings.append(Finding("private_key", text.count("\n", 0, m.start()) + 1))
        return _placeholder("private_key")

    return _PEM_DANGLING.sub(dangling, text)


def _redact_line(line: str, number: int, findings: list[Finding]) -> str:
    for rule in _RULES:
        if rule.kind == "generic_secret":
            line = _redact_generic(rule, line, number, findings)
            continue

        def sub(m: re.Match[str], rule: _Rule = rule) -> str:
            value = m.group(rule.group)
            if rule.accept is not None and not rule.accept(value):
                return m.group(0)
            findings.append(Finding(rule.kind, number))
            if rule.group == 0:
                return _placeholder(rule.kind)
            start = m.start(rule.group) - m.start(0)
            end = m.end(rule.group) - m.start(0)
            whole = m.group(0)
            return whole[:start] + _placeholder(rule.kind) + whole[end:]

        line = rule.pattern.sub(sub, line)
    return line


def _redact_generic(rule: _Rule, line: str, number: int, findings: list[Finding]) -> str:
    def sub(m: re.Match[str]) -> str:
        value = m.group("v")
        if not _looks_like_real_secret(value):
            return m.group(0)
        findings.append(Finding(rule.kind, number))
        whole = m.group(0)
        start = m.start("v") - m.start(0)
        end = m.end("v") - m.start(0)
        return whole[:start] + _placeholder(rule.kind) + whole[end:]

    return rule.pattern.sub(sub, line)


def redact(text: str) -> Redaction:
    """Return the text with secret-looking values replaced, and what was found (kind + line)."""
    findings: list[Finding] = []
    text = _redact_pem(text, findings)
    lines = text.split("\n")
    out = [_redact_line(line, i + 1, findings) for i, line in enumerate(lines)]
    result = "\n".join(out)
    if not findings:
        return Redaction(text=result, findings=())
    return Redaction(text=result, findings=tuple(sorted(findings, key=lambda f: (f.line, f.kind))))
