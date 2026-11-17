import ast
import random
import string

import pytest

from app.services.security.secrets import redact


def fake(prefix: str, body: str) -> str:
    """Built at runtime so no literal token-shaped string sits in the repository (which would
    trip push protection and secret scanners)."""
    return prefix + body


ALNUM = string.ascii_letters + string.digits
rng = random.Random(7)


def rand(n: int, alphabet: str = ALNUM) -> str:
    return "".join(rng.choice(alphabet) for _ in range(n))


TOKENS = {
    "aws_access_key": fake("AK", "IA") + rand(16, string.ascii_uppercase + string.digits),
    "github_token": fake("gh", "p_") + rand(36),
    "slack_token": fake("xo", "xb-") + "1234567890-" + rand(24),
    "stripe_key": fake("sk", "_live_") + rand(30),
    "google_api_key": fake("AI", "za") + rand(35, ALNUM + "_-"),
    "anthropic_key": fake("sk", "-ant-api03-") + rand(40, ALNUM + "_-"),
    "openrouter_key": fake("sk", "-or-v1-") + rand(48, "0123456789abcdef"),
    "openai_key": fake("sk", "-proj-") + rand(40),
    "huggingface_token": fake("hf", "_") + rand(36),
    "jwt": fake("eyJ", rand(20)) + "." + fake("eyJ", rand(20)) + "." + rand(30, ALNUM + "_-"),
}


@pytest.mark.parametrize(("kind", "token"), TOKENS.items())
def test_known_token_formats_are_redacted(kind: str, token: str) -> None:
    result = redact(f'client = connect("{token}")\n')
    assert token not in result.text
    assert f"[REDACTED:{kind}]" in result.text
    assert [(f.kind, f.line) for f in result.findings] == [(kind, 1)]
    assert token not in repr(result.findings)  # findings never carry the value


def test_a_token_in_a_comment_or_plain_text_is_also_caught() -> None:
    token = TOKENS["github_token"]
    assert token not in redact(f"# use {token} for ci\n").text
    assert token not in redact(f"Authorization: token {token}").text


@pytest.mark.parametrize(
    "line",
    [
        'password = "{v}"',
        'DB_PASSWORD="{v}"',
        'api_key = "{v}"',
        '"apiKey": "{v}"',
        "secret: '{v}'",
        'client_secret = "{v}"',
        'auth_token="{v}"',
        'self.token = "{v}"',
        'config["access_token"] = "{v}"',
    ],
)
def test_quoted_high_entropy_values_next_to_secret_names_are_redacted(line: str) -> None:
    value = rand(24, ALNUM + "-_")
    result = redact(line.format(v=value) + "\n")
    assert value not in result.text
    assert "[REDACTED:generic_secret]" in result.text
    # The key and quoting are kept, so the code still reads (and parses) sensibly.
    assert line.split("{v}")[0] in result.text


def test_credentials_in_connection_strings_are_redacted_but_host_and_user_stay() -> None:
    password = rand(14)
    for url in (
        f"postgres://admin:{password}@db.internal:5432/app",
        f"postgresql+psycopg://svc:{password}@host/db",
        f"mongodb+srv://u:{password}@cluster.example.net",
        f"https://user:{password}@example.com/path",
    ):
        r = redact(f'URL = "{url}"\n')
        assert password not in r.text
        assert "[REDACTED:url_password]@" in r.text
        assert url.split("://")[0] in r.text and "@" in r.text


def test_private_key_blocks_are_redacted_and_the_line_count_is_unchanged() -> None:
    body = "\n".join(rand(64, ALNUM + "+/") for _ in range(5))
    pem = f"-----BEGIN RSA PRIVATE KEY-----\n{body}\n-----END RSA PRIVATE KEY-----"
    text = f"before = 1\n{pem}\nafter = 2\n"
    r = redact(text)
    assert body.split("\n")[0] not in r.text
    assert "[REDACTED:private_key]" in r.text
    assert "BEGIN RSA PRIVATE KEY-----" not in r.text
    assert r.text.count("\n") == text.count("\n")
    assert r.text.startswith("before = 1\n") and r.text.endswith("after = 2\n")
    assert [(f.kind, f.line) for f in r.findings] == [("private_key", 2)]


def test_an_unterminated_private_key_header_is_still_redacted() -> None:
    r = redact("-----BEGIN OPENSSH PRIVATE KEY-----\nabcdefgh\n")
    assert "BEGIN OPENSSH PRIVATE KEY" not in r.text


@pytest.mark.parametrize(
    "line",
    [
        "password = get_password()",
        "token = tokens.pop()",
        'api_key = os.environ["API_KEY"]',
        'api_key = os.getenv("API_KEY")',
        "const apiKey = process.env.API_KEY;",
        'password = "changeme"',
        'secret = "example-secret-value"',
        'password = "<your-password>"',
        'API_KEY = "${API_KEY}"',
        'token = "{{ token }}"',
        'password = "hunter2"',  # too short to be a credential
        # Real test fixtures found in public repositories: words, not credentials.
        "token: 'invalid-token',",
        "token: 'valid:token',",
        "newToken: 'refresh-token-123'",
        "headers: {'X-Auth-Token': 'origin-token'}",
        "let validToken = 'valid-token';",
        'password = "correct-horse-battery-staple"',
        "api_key = 'my_test_api_key_value'",
        'secret = "all-lowercase-and-hyphenated-words"',
        'auth_token = "1234567890123456"',
        'token_type = "Bearer"',
        'api_key = "aaaaaaaaaaaaaaaaaaaaaaaa"',  # no variety
        'private_key_path = "/etc/ssl/private/key.pem"',
        'secret_url = "https://example.com/a/b"',
        'auth_token = "not a real token because it has spaces"',
        'password = ""',
        "DATABASE_URL = 'postgres://user@localhost/db'",
        "url = 'postgres://user:${PASSWORD}@localhost/db'",
        # Placeholder URL passwords as used in URL-parsing tests and documentation.
        "u = 'http://user:pass@example.com/path'",
        "u = 'https://username:password@host.org'",
        "u = 'http://user:password123@host'",
        "u = 'http://user:a%20secret@host'",
        "u = 'http://user:example-password@host'",
        "u = 'http://user:xxxx@host'",
        "u = 'http://user:{ENCODED_PASSWORD}@host'",
        "u = 'http://user:[secure]@host'",
        'label = "sk-learn pipelines and risk-assessment tools"',
        "def check_password(password: str, hashed: str) -> bool: ...",
        "token = request.headers.get('X-Token')",
    ],
)
def test_ordinary_code_is_left_alone(line: str) -> None:
    result = redact(line + "\n")
    assert result.text == line + "\n", result
    assert result.findings == ()


def test_redaction_never_changes_the_number_of_lines() -> None:
    for seed in range(100):
        r = random.Random(seed)
        parts = []
        for _ in range(r.randint(1, 30)):
            kind = r.choice(list(TOKENS))
            parts.append(r.choice([f'x = "{TOKENS[kind]}"', "", "y = 1", f"# {TOKENS[kind]}"]))
        text = "\n".join(parts)
        assert redact(text).text.count("\n") == text.count("\n")


def test_redaction_is_idempotent_and_the_second_pass_finds_nothing() -> None:
    text = "\n".join(f'v{i} = "{t}"' for i, t in enumerate(TOKENS.values())) + "\n"
    once = redact(text)
    twice = redact(once.text)
    assert twice.text == once.text and twice.findings == ()
    assert once.count == len(TOKENS)


def test_several_secrets_on_one_line_are_each_counted() -> None:
    line = f'a = "{TOKENS["github_token"]}"; b = "{TOKENS["stripe_key"]}"'
    r = redact(line)
    assert [f.kind for f in r.findings] == ["github_token", "stripe_key"]
    assert r.findings[0].line == r.findings[1].line == 1


def test_redacted_python_still_parses() -> None:
    value = rand(24)
    src = f'password = "{value}"\napi_key = "{TOKENS["github_token"]}"\nurl = "postgres://u:{value}@h/db"\n'
    ast.parse(redact(src).text)  # raises SyntaxError if redaction broke the syntax


def test_findings_are_sorted_by_line() -> None:
    text = f'a = 1\nb = "{TOKENS["jwt"]}"\n# {TOKENS["github_token"]}\n'
    assert [f.line for f in redact(text).findings] == [2, 3]


def test_no_secret_means_the_text_is_returned_unchanged() -> None:
    text = "def f():\n    return 1\n"
    r = redact(text)
    assert r.text == text and r.count == 0


def test_a_random_looking_url_password_is_still_redacted_even_with_a_common_word_in_it() -> None:
    for password in (rand(14), "S3cr3tPassw0rd!x", "Tr0ub4dor&3xyz"):
        r = redact(f'u = "postgres://svc:{password}@db/app"\n')
        assert password not in r.text and "[REDACTED:url_password]" in r.text, password
