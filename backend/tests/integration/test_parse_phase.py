import logging
import random
import string
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.core.config import get_settings
from app.services.indexing import parse_phase
from app.services.parsing.parse import parse_source
from tests.helpers import FakeGitHub
from tests.parse_helpers import check_chunks

pytestmark = pytest.mark.db

_rng = random.Random(11)


def _rand(n: int, alphabet: str = string.ascii_letters + string.digits) -> str:
    return "".join(_rng.choice(alphabet) for _ in range(n))


# Secret-shaped values are assembled at runtime so no token-like literal sits in the repository.
GITHUB_TOKEN = "gh" + "p_" + _rand(36)
DB_PASSWORD = _rand(18)
GENERIC_SECRET = _rand(26)
PEM_BODY = _rand(64, string.ascii_letters + string.digits + "+/")
SECRETS = [GITHUB_TOKEN, DB_PASSWORD, GENERIC_SECRET, PEM_BODY]

PY_SERVICE = '''"""Service layer."""
import os
from .util import slugify, clean as tidy


class Service:
    """Does things."""

    def __init__(self, name):
        self.name = name

    def run(self, item):
        tidy(item)
        return slugify(os.path.join(self.name, item))


def main():
    Service("x").run("y")
'''

REPO_FILES: dict[str, str | bytes] = {
    "pkg/__init__.py": "",
    "pkg/service.py": PY_SERVICE,
    "pkg/util.py": (
        "def slugify(s):\n    return s.lower()\n\n\ndef clean(s):\n    return s.strip()\n"
    ),
    "pkg/broken.py": "def good():\n    return 1\n\ndef broken(:\n    pass\n",
    "web/api.ts": (
        'import type { User } from "./types";\n'
        "export interface Repo { id: string }\n"
        "export async function load(id: string): Promise<Repo> {\n  return fetchRepo(id);\n}\n"
    ),
    "web/view.tsx": "export function View() { return <b onClick={() => go()}>x</b>; }\n",
    "lib/index.js": (
        'const fs = require("fs");\nexports.read = function (p) { return fs.readFileSync(p); };\n'
    ),
    "cmd/main.go": ('package main\n\nimport "fmt"\n\nfunc main() {\n\tfmt.Println("hi")\n}\n'),
    "NOTES.md": "# not parsed\n",
}
PARSEABLE = {p: c for p, c in REPO_FILES.items() if isinstance(c, str) and not p.endswith(".md")}


def register(c: TestClient) -> str:
    r = c.post("/api/repositories", json={"url": "https://github.com/o/r"})
    return str(r.json()["id"])


def tick(c: TestClient, rid: str) -> dict[str, Any]:
    r = c.post(f"/api/repositories/{rid}/index/tick")
    assert r.status_code == 200, r.text
    return dict(r.json())


def run_until(c: TestClient, rid: str, phase: str, limit: int = 60) -> dict[str, Any]:
    for _ in range(limit):
        status = tick(c, rid)["status"]
        if status["phase"] == phase or status["state"] == "failed":
            return dict(status)
    raise AssertionError(f"never reached {phase}")


def index_repo(c: TestClient, fake: FakeGitHub, files: dict[str, Any] | None = None) -> str:
    if files is not None:
        fake.files = files
    rid = register(c)
    assert c.post(f"/api/repositories/{rid}/index").status_code == 202
    return rid


def rows(engine: Engine, sql: str, **params: Any) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(text(sql), params))


def expired(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("UPDATE index_jobs SET lease_until = now() - interval '1 second'"))


def expected_totals() -> dict[str, int]:
    totals = {"symbols": 0, "chunks": 0, "refs": 0}
    settings = get_settings()
    for path, content in PARSEABLE.items():
        lang = {
            "py": "python",
            "ts": "typescript",
            "tsx": "typescript",
            "js": "javascript",
            "go": "go",
        }[path.rsplit(".", 1)[1]]
        r = parse_source(path, lang, content, settings.chunk_max_chars, settings.chunk_merge_below)
        totals["symbols"] += len(r.parsed.symbols)
        totals["chunks"] += len(r.chunks)
        totals["refs"] += len(r.parsed.refs)
    return totals


# --- what gets stored -------------------------------------------------------------------


def test_parsing_stores_symbols_chunks_and_refs_with_correct_lines(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = index_repo(db_client, fake_gh, REPO_FILES)
    status = run_until(db_client, rid, "embed")
    assert status["state"] == "running" and status["phase"] == "embed"
    assert status["parse"]["done"] is True

    totals = expected_totals()
    assert status["parse"]["files"] == len(PARSEABLE)
    assert (status["parse"]["symbols"], status["parse"]["chunks"], status["parse"]["refs"]) == (
        totals["symbols"],
        totals["chunks"],
        totals["refs"],
    )
    assert rows(db_engine, "SELECT count(*) FROM symbols")[0][0] == totals["symbols"]
    assert rows(db_engine, "SELECT count(*) FROM chunks")[0][0] == totals["chunks"]
    assert rows(db_engine, "SELECT count(*) FROM code_refs")[0][0] == totals["refs"]
    assert rows(db_engine, "SELECT chunk_count FROM snapshots")[0][0] == totals["chunks"]

    # Every stored chunk is exactly the cited lines of the stored file, and together they cover
    # every non-blank line once: this is what makes later citations trustworthy.
    for path, content in rows(
        db_engine, "SELECT path, content FROM files WHERE content IS NOT NULL"
    ):
        chunks = rows(
            db_engine,
            "SELECT c.start_line, c.end_line, c.text FROM chunks c "
            "JOIN files f ON f.id = c.file_id WHERE f.path = :p ORDER BY c.start_line",
            p=path,
        )
        lines = content.split("\n")
        for start, end, body in chunks:
            assert body == "\n".join(lines[start - 1 : end]), (path, start, end)
        from app.services.parsing.chunker import Chunk

        check_chunks(content, [Chunk(a, b, None) for a, b, _ in chunks])


def test_symbols_know_their_parents_and_qualified_names(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = index_repo(db_client, fake_gh, REPO_FILES)
    run_until(db_client, rid, "embed")
    got = rows(
        db_engine,
        "SELECT s.kind, s.qualified_name, s.start_line, s.end_line, s.signature, p.qualified_name "
        "FROM symbols s JOIN files f ON f.id = s.file_id LEFT JOIN symbols p ON p.id = s.parent_id "
        "WHERE f.path = 'pkg/service.py' ORDER BY s.start_line",
    )
    assert [(g[0], g[1].split("::")[1], g[2], g[3], g[4], g[5]) for g in got] == [
        ("class", "Service", 6, 14, "class Service:", None),
        (
            "method",
            "Service.__init__",
            9,
            10,
            "def __init__(self, name):",
            "pkg/service.py::Service",
        ),
        ("method", "Service.run", 12, 14, "def run(self, item):", "pkg/service.py::Service"),
        ("function", "main", 17, 18, "def main():", None),
    ]


def test_refs_record_imports_and_calls_with_their_enclosing_symbol(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = index_repo(db_client, fake_gh, REPO_FILES)
    run_until(db_client, rid, "embed")
    refs = rows(
        db_engine,
        "SELECT r.kind, r.name, r.line, s.name FROM code_refs r JOIN files f ON f.id = r.file_id "
        "LEFT JOIN symbols s ON s.id = r.symbol_id WHERE f.path = 'pkg/service.py' ORDER BY r.id",
    )
    assert ("import", "os", 2, None) in refs
    assert ("import", ".util", 3, None) in refs
    assert ("call", "tidy", 13, "run") in refs
    assert ("call", "slugify", 14, "run") in refs
    assert ("call", "run", 18, "main") in refs
    go = rows(
        db_engine,
        "SELECT r.name FROM code_refs r JOIN files f ON f.id = r.file_id "
        "WHERE f.path = 'cmd/main.go' AND r.kind = 'import'",
    )
    assert go == [("fmt",)]


def test_files_that_were_skipped_are_never_parsed(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = index_repo(db_client, fake_gh)  # the default fixture includes an oversized big.py
    run_until(db_client, rid, "embed")
    assert (
        rows(
            db_engine,
            "SELECT count(*) FROM symbols s JOIN files f ON f.id = s.file_id "
            "WHERE f.path = 'src/big.py'",
        )[0][0]
        == 0
    )
    assert rows(db_engine, "SELECT parse_status FROM files WHERE path = 'src/big.py'")[0][0] is None
    assert rows(db_engine, "SELECT parse_status FROM files WHERE path = 'src/app.py'")[0][0] == "ok"


def test_a_file_with_syntax_errors_is_flagged_and_indexing_carries_on(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = index_repo(db_client, fake_gh, REPO_FILES)
    status = run_until(db_client, rid, "embed")
    assert status["parse"]["syntax_error_files"] == 1 and status["state"] == "running"
    assert (
        rows(db_engine, "SELECT parse_status FROM files WHERE path = 'pkg/broken.py'")[0][0]
        == "syntax_errors"
    )
    assert (
        rows(
            db_engine,
            "SELECT count(*) FROM symbols s JOIN files f ON f.id = s.file_id "
            "WHERE f.path = 'pkg/broken.py'",
        )[0][0]
        >= 1
    )


def test_a_repo_with_nothing_to_parse_moves_straight_on(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = index_repo(db_client, fake_gh, {"NOTES.md": "# docs only\n", "data.json": "{}"})
    status = run_until(db_client, rid, "embed")
    assert status["phase"] == "embed" and status["parse"]["files"] == 0
    assert rows(db_engine, "SELECT count(*) FROM chunks")[0][0] == 0


def test_crlf_and_byte_order_marks_are_normalised_before_storage(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    crlf = "﻿def a():\r\n    return 1\r\n\r\ndef b():\r\n    return 2\r\n".encode()
    rid = index_repo(db_client, fake_gh, {"m.py": crlf})
    run_until(db_client, rid, "embed")
    content = rows(db_engine, "SELECT content FROM files WHERE path = 'm.py'")[0][0]
    assert content == "def a():\n    return 1\n\ndef b():\n    return 2\n"
    spans = rows(db_engine, "SELECT name, start_line, end_line FROM symbols ORDER BY start_line")
    assert [tuple(s) for s in spans] == [("a", 1, 2), ("b", 4, 5)]


# --- secrets ----------------------------------------------------------------------------

SECRET_FILES: dict[str, str | bytes] = {
    "config.py": (
        f'GITHUB = "{GITHUB_TOKEN}"\n'
        f'DB = "postgres://admin:{DB_PASSWORD}@db.internal:5432/app"\n'
        f'password = "{GENERIC_SECRET}"\n'
        'safe = os.environ["TOKEN"]\n'
        "\n"
        'def connect(url="postgres://user@localhost/db"):\n'
        "    return url\n"
    ),
    "keys.js": (
        "const key = `\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        f"{PEM_BODY}\n"
        "-----END RSA PRIVATE KEY-----\n"
        "`;\n"
        "function use() { return key; }\n"
    ),
    "main.go": f'package main\n\nvar token = "{GITHUB_TOKEN}"\n\nfunc main() {{}}\n',
}


def _everything_stored(engine: Engine) -> str:
    """Every text-like column in every table that could hold file content."""
    queries = [
        "SELECT content FROM files",
        "SELECT text FROM chunks",
        "SELECT name || ' ' || qualified_name || ' ' || coalesce(signature, '') FROM symbols",
        "SELECT name || ' ' || detail::text FROM code_refs",
        "SELECT coalesce(error, '') || cursor::text FROM index_jobs",
        "SELECT coalesce(error, '') FROM snapshots",
    ]
    out = []
    with engine.connect() as conn:
        for q in queries:
            out += [str(r[0]) for r in conn.execute(text(q)) if r[0] is not None]
    return "\n".join(out)


def test_secret_values_appear_nowhere_in_storage_or_logs(
    db_client: TestClient,
    fake_gh: FakeGitHub,
    db_engine: Engine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    rid = index_repo(db_client, fake_gh, SECRET_FILES)
    status = run_until(db_client, rid, "embed")

    stored = _everything_stored(db_engine)
    for secret in SECRETS:
        assert secret not in stored
        assert secret not in caplog.text
        assert secret not in str(status)
    assert "[REDACTED:github_token]" in stored
    assert "[REDACTED:url_password]" in stored
    assert "[REDACTED:generic_secret]" in stored
    assert "[REDACTED:private_key]" in stored

    # ...and ordinary code next to the secrets is untouched.
    assert 'safe = os.environ["TOKEN"]' in stored
    assert "postgres://user@localhost/db" in stored

    # The counts are reported without the values.
    assert status["progress"]["secrets_redacted"] == 5
    assert status["progress"]["files_redacted"] == 3
    per_file = dict(rows(db_engine, "SELECT path, redactions FROM files"))
    assert per_file == {"config.py": 3, "keys.js": 1, "main.go": 1}


def test_secrets_are_already_gone_right_after_fetching_before_any_parsing(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    """The fetch step is where redaction must happen: a raw credential should never be written
    to the database at all, not merely cleaned up by a later phase."""
    rid = index_repo(db_client, fake_gh, SECRET_FILES)
    status = tick(db_client, rid)["status"]  # fetch only
    assert status["phase"] == "parse"
    stored = _everything_stored(db_engine)
    for secret in SECRETS:
        assert secret not in stored
    assert "[REDACTED:github_token]" in stored
    assert rows(db_engine, "SELECT count(*) FROM chunks")[0][0] == 0  # parsing has not run


def test_redaction_keeps_symbols_and_line_numbers_aligned_with_the_original_file(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = index_repo(db_client, fake_gh, SECRET_FILES)
    run_until(db_client, rid, "embed")
    original = SECRET_FILES["keys.js"]
    assert isinstance(original, str)
    stored = rows(db_engine, "SELECT content FROM files WHERE path = 'keys.js'")[0][0]
    assert stored.count("\n") == original.count("\n")  # the key block kept its line count
    use = rows(db_engine, "SELECT start_line, end_line FROM symbols WHERE name = 'use'")[0]
    assert tuple(use) == (original.split("\n").index("function use() { return key; }") + 1,) * 2


def test_rows_stored_before_redaction_existed_are_cleaned_by_the_parse_phase(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = index_repo(db_client, fake_gh, {"a.py": "x = 1\n"})
    tick(db_client, rid)  # fetch
    with db_engine.begin() as conn:
        conn.execute(
            text("UPDATE files SET content = :c, redactions = 0 WHERE path = 'a.py'"),
            {"c": f'token = "{GITHUB_TOKEN}"\n\ndef f():\n    return 1\n'},
        )
    run_until(db_client, rid, "embed")
    stored = _everything_stored(db_engine)
    assert GITHUB_TOKEN not in stored and "[REDACTED:github_token]" in stored
    assert rows(db_engine, "SELECT redactions FROM files WHERE path = 'a.py'")[0][0] == 1


# --- resumability -----------------------------------------------------------------------


def settings_override(c: TestClient, **kw: Any) -> None:
    base = get_settings()
    c.app.dependency_overrides[get_settings] = lambda: base.model_copy(update=kw)  # type: ignore[attr-defined]


def test_a_zero_time_budget_still_parses_one_file_per_tick_and_resumes(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    settings_override(db_client, parse_budget_seconds=0.0, parse_batch_files=2)
    rid = index_repo(db_client, fake_gh, REPO_FILES)
    tick(db_client, rid)  # fetch
    counts = []
    for _ in range(40):
        status = tick(db_client, rid)["status"]
        if status["phase"] == "embed":
            break
        counts.append(status["parse"]["files"])
    assert counts == list(range(1, len(PARSEABLE))), counts  # exactly one new file per tick
    assert status["parse"]["files"] == len(PARSEABLE) and status["parse"]["done"] is True
    totals = expected_totals()
    assert rows(db_engine, "SELECT count(*) FROM chunks")[0][0] == totals["chunks"]
    assert rows(db_engine, "SELECT count(*) FROM symbols")[0][0] == totals["symbols"]


def test_a_crash_mid_phase_loses_only_that_tick_and_never_duplicates_rows(
    db_client: TestClient,
    fake_gh: FakeGitHub,
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_override(db_client, parse_budget_seconds=0.0)
    rid = index_repo(db_client, fake_gh, REPO_FILES)
    tick(db_client, rid)  # fetch

    real = parse_phase.parse_source  # type: ignore[attr-defined]
    calls = {"n": 0}

    def flaky(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("worker died while parsing")
        return real(*args, **kwargs)

    monkeypatch.setattr("app.services.indexing.parse_phase.parse_source", flaky)
    tick(db_client, rid)
    tick(db_client, rid)
    failed = tick(db_client, rid)["status"]  # third file crashes
    assert failed["error"]["code"] == "internal_error" and failed["attempts"] == 1
    assert "worker died" not in str(failed)
    assert failed["parse"]["files"] == 2  # progress from the two good ticks is kept
    # Two files were parsed, but the first is an empty __init__.py, which has no chunks.
    assert rows(db_engine, "SELECT count(DISTINCT file_id) FROM chunks")[0][0] == 1

    expired(db_engine)
    status = run_until(db_client, rid, "embed")
    assert status["parse"]["files"] == len(PARSEABLE) and status["attempts"] == 0

    totals = expected_totals()
    assert rows(db_engine, "SELECT count(*) FROM chunks")[0][0] == totals["chunks"]
    assert rows(db_engine, "SELECT count(*) FROM symbols")[0][0] == totals["symbols"]
    assert rows(db_engine, "SELECT count(*) FROM code_refs")[0][0] == totals["refs"]
    dupes = rows(
        db_engine,
        "SELECT file_id, start_line, end_line, count(*) FROM chunks "
        "GROUP BY 1, 2, 3 HAVING count(*) > 1",
    )
    assert dupes == []


def test_running_the_phase_again_replaces_rather_than_duplicates(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = index_repo(db_client, fake_gh, REPO_FILES)
    run_until(db_client, rid, "embed")
    before = [
        rows(db_engine, f"SELECT count(*) FROM {t}")[0][0]
        for t in ("symbols", "chunks", "code_refs")
    ]
    with db_engine.begin() as conn:  # rewind the job to the start of the phase
        conn.execute(text("UPDATE index_jobs SET phase = 'parse', cursor = cursor - 'parse'"))
    run_until(db_client, rid, "embed")
    after = [
        rows(db_engine, f"SELECT count(*) FROM {t}")[0][0]
        for t in ("symbols", "chunks", "code_refs")
    ]
    assert before == after


def test_too_many_chunks_fails_the_job_and_rolls_back_that_tick(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    settings_override(db_client, max_chunks=2, parse_budget_seconds=60.0)
    rid = index_repo(db_client, fake_gh, REPO_FILES)
    status = run_until(db_client, rid, "embed")
    assert status["state"] == "failed" and status["can_resume"] is True
    assert status["error"]["code"] == "repo_too_large"
    assert "more than 2 chunks" in status["error"]["message"]
    assert rows(db_engine, "SELECT count(*) FROM chunks")[0][0] == 0  # the whole tick rolled back
