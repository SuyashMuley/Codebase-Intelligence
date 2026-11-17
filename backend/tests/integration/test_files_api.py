import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from tests.helpers import FakeGitHub
from tests.integration.test_parse_phase import REPO_FILES, SECRET_FILES, SECRETS, run_until

pytestmark = pytest.mark.db


def make_repo(
    c: TestClient, fake: FakeGitHub, files: dict[str, Any], url: str = "https://github.com/o/r"
) -> str:
    fake.files = files
    rid = str(c.post("/api/repositories", json={"url": url}).json()["id"])
    assert c.post(f"/api/repositories/{rid}/index").status_code in (200, 202)
    return rid


def indexed(c: TestClient, fake: FakeGitHub, files: dict[str, Any] | None = None) -> str:
    rid = make_repo(c, fake, REPO_FILES if files is None else files)
    run_until(c, rid, "embed")
    return rid


def test_nothing_is_available_before_a_repository_is_indexed(db_client: TestClient) -> None:
    rid = str(
        db_client.post("/api/repositories", json={"url": "https://github.com/o/r"}).json()["id"]
    )
    for path in ("/files", "/files/a.py", "/stats"):
        r = db_client.get(f"/api/repositories/{rid}{path}")
        assert r.status_code == 409 and r.json()["error"]["code"] == "snapshot_not_ready"


def test_file_list_has_every_stored_file_without_content(
    db_client: TestClient, fake_gh: FakeGitHub
) -> None:
    rid = indexed(db_client, fake_gh)
    body = db_client.get(f"/api/repositories/{rid}/files").json()
    assert body["truncated"] is False
    assert body["snapshot"]["state"] == "running"
    paths = [i["path"] for i in body["items"]]
    assert paths == sorted(paths)
    parseable = {p for p in REPO_FILES if not p.endswith(".md")}
    assert (
        set(paths) == parseable
    )  # README.md is not stored: unsupported file types are only counted
    by_path = {i["path"]: i for i in body["items"]}
    service = by_path["pkg/service.py"]
    assert service["language"] == "python" and service["symbol_count"] == 4
    assert service["parse_status"] == "ok" and service["redactions"] == 0
    assert by_path["pkg/broken.py"]["parse_status"] == "syntax_errors"
    assert all("content" not in i for i in body["items"])


def test_skipped_files_are_listed_with_their_reason(
    db_client: TestClient, fake_gh: FakeGitHub
) -> None:
    rid = indexed(db_client, fake_gh, FakeGitHub().files)  # the default fixture has a huge file
    items = {i["path"]: i for i in db_client.get(f"/api/repositories/{rid}/files").json()["items"]}
    assert items["src/big.py"]["skipped_reason"] == "too_large"
    assert items["src/big.py"]["parse_status"] is None and items["src/big.py"]["symbol_count"] == 0
    assert items["src/app.py"]["skipped_reason"] is None


def test_file_detail_returns_text_symbols_and_parent_links(
    db_client: TestClient, fake_gh: FakeGitHub
) -> None:
    rid = indexed(db_client, fake_gh)
    r = db_client.get(f"/api/repositories/{rid}/files/pkg/service.py")
    assert r.status_code == 200
    body = r.json()
    assert body["content"] == REPO_FILES["pkg/service.py"]
    assert body["language"] == "python" and body["line_count"] == body["content"].count("\n") + 1
    symbols = {s["name"]: s for s in body["symbols"]}
    assert [(s["kind"], s["name"], s["start_line"], s["end_line"]) for s in body["symbols"]] == [
        ("class", "Service", 6, 14),
        ("method", "__init__", 9, 10),
        ("method", "run", 12, 14),
        ("function", "main", 17, 18),
    ]
    assert symbols["run"]["parent_id"] == symbols["Service"]["id"]
    assert symbols["main"]["parent_id"] is None
    assert symbols["run"]["signature"] == "def run(self, item):"


def test_a_skipped_file_has_no_content_to_show(db_client: TestClient, fake_gh: FakeGitHub) -> None:
    rid = indexed(db_client, fake_gh, FakeGitHub().files)
    body = db_client.get(f"/api/repositories/{rid}/files/src/big.py").json()
    assert body["content"] is None and body["skipped_reason"] == "too_large"
    assert body["line_count"] == 0 and body["symbols"] == []


def test_file_content_is_redacted_in_the_api_too(
    db_client: TestClient, fake_gh: FakeGitHub
) -> None:
    rid = indexed(db_client, fake_gh, SECRET_FILES)
    bodies = [db_client.get(f"/api/repositories/{rid}/files/{p}").text for p in SECRET_FILES] + [
        db_client.get(f"/api/repositories/{rid}/files").text,
        db_client.get(f"/api/repositories/{rid}/stats").text,
    ]
    for secret in SECRETS:
        assert all(secret not in b for b in bodies)
    config = db_client.get(f"/api/repositories/{rid}/files/config.py").json()
    assert config["redactions"] == 3 and "[REDACTED:github_token]" in config["content"]


@pytest.mark.parametrize(
    "path",
    [
        "does/not/exist.py",
        "../../etc/passwd",
        "..%2f..%2fetc%2fpasswd",
        "pkg/../../etc/passwd",
        "/etc/passwd",
        "pkg//service.py",
        "pkg/service.py/",
        "pkg\\service.py",
        "x" * 600,
        "pkg/service.py%00.png",
        "PKG/SERVICE.PY",
    ],
)
def test_unknown_or_hostile_paths_are_a_clean_404(
    db_client: TestClient, fake_gh: FakeGitHub, path: str
) -> None:
    rid = indexed(db_client, fake_gh)
    r = db_client.get(f"/api/repositories/{rid}/files/{path}")
    assert r.status_code == 404, (path, r.status_code)
    assert r.json()["error"]["code"] in ("file_not_found", "not_found")
    assert "root:" not in r.text  # nothing from the real filesystem can ever appear


def test_snapshot_parameter_is_validated_and_scoped_to_the_repository(
    db_client: TestClient, fake_gh: FakeGitHub
) -> None:
    rid = indexed(db_client, fake_gh)
    snap = db_client.get(f"/api/repositories/{rid}/files").json()["snapshot"]["id"]
    assert (
        db_client.get(f"/api/repositories/{rid}/files", params={"snapshot": snap}).status_code
        == 200
    )
    r = db_client.get(f"/api/repositories/{rid}/files", params={"snapshot": str(uuid.uuid4())})
    assert r.status_code == 404 and r.json()["error"]["code"] == "snapshot_not_found"
    assert (
        db_client.get(f"/api/repositories/{rid}/files", params={"snapshot": "nope"}).status_code
        == 422
    )

    # A snapshot id from a different repository cannot be used to read that repository's files.
    other = make_repo(
        db_client, FakeGitHub(), {"x.py": "def x(): ...\n"}, "https://github.com/o/other"
    )
    other_snap = db_client.get(f"/api/repositories/{other}/files").json()["snapshot"]["id"]
    r = db_client.get(f"/api/repositories/{rid}/files", params={"snapshot": other_snap})
    assert r.status_code == 404 and r.json()["error"]["code"] == "snapshot_not_found"


def test_stats_report_what_was_indexed(
    db_client: TestClient, fake_gh: FakeGitHub, db_engine: Engine
) -> None:
    rid = indexed(db_client, fake_gh)
    stats = db_client.get(f"/api/repositories/{rid}/stats").json()
    assert stats["parsing_done"] is True
    assert stats["files"]["by_language"] == {"go": 1, "javascript": 1, "python": 4, "typescript": 2}
    assert stats["files"]["parsed"] == 8
    assert stats["files"]["skipped"] == {"unsupported": 1}  # README.md
    assert stats["syntax_error_files"] == 1
    with db_engine.connect() as conn:
        assert (
            stats["symbols"]["total"]
            == conn.execute(text("SELECT count(*) FROM symbols")).scalar_one()
        )
        assert (
            stats["chunks"]["total"]
            == conn.execute(text("SELECT count(*) FROM chunks")).scalar_one()
        )
        calls = conn.execute(
            text("SELECT count(*) FROM code_refs WHERE kind = 'call'")
        ).scalar_one()
    assert stats["refs"]["calls"] == calls and stats["refs"]["imports"] > 0
    assert sum(stats["symbols"]["by_kind"].values()) == stats["symbols"]["total"]
    assert 0 < stats["chunks"]["average_chars"] <= stats["chunks"]["largest_chars"] <= 1800
    assert stats["redactions"] == {"spans": 0, "files": 0}


def test_stats_count_redactions_without_revealing_them(
    db_client: TestClient, fake_gh: FakeGitHub
) -> None:
    rid = indexed(db_client, fake_gh, SECRET_FILES)
    stats = db_client.get(f"/api/repositories/{rid}/stats").json()
    assert stats["redactions"] == {"spans": 5, "files": 3}


def test_stats_before_parsing_finishes_say_so(db_client: TestClient, fake_gh: FakeGitHub) -> None:
    rid = make_repo(db_client, fake_gh, REPO_FILES)
    db_client.post(f"/api/repositories/{rid}/index/tick")  # fetch only
    stats = db_client.get(f"/api/repositories/{rid}/stats").json()
    assert stats["parsing_done"] is False and stats["symbols"]["total"] == 0
    assert stats["files"]["stored"] == 8


# --- access control -------------------------------------------------------------------------


def read_routes(rid: str) -> list[str]:
    return [f"/api/repositories/{rid}{p}" for p in ("/files", "/files/pkg/service.py", "/stats")]


def test_another_users_files_stats_and_symbols_are_a_404(
    db_client: TestClient, other_client: TestClient, fake_gh: FakeGitHub
) -> None:
    rid = indexed(db_client, fake_gh)
    for url in read_routes(rid):
        assert db_client.get(url).status_code == 200
        r = other_client.get(url)
        assert r.status_code == 404 and r.json()["error"]["code"] == "repo_not_found", url
    for url in read_routes(str(uuid.uuid4())):
        assert other_client.get(url).status_code == 404


def test_after_connecting_the_same_public_repo_a_user_can_read_the_shared_index(
    db_client: TestClient, other_client: TestClient, fake_gh: FakeGitHub
) -> None:
    rid = indexed(db_client, fake_gh)
    assert (
        other_client.post("/api/repositories", json={"url": "https://github.com/o/r"}).status_code
        == 201
    )
    r = other_client.get(f"/api/repositories/{rid}/files/pkg/service.py")
    assert r.status_code == 200 and r.json()["content"] == REPO_FILES["pkg/service.py"]


def test_read_routes_need_a_session(anon_client: TestClient) -> None:
    for url in read_routes(str(uuid.uuid4())):
        r = anon_client.get(url)
        assert r.status_code == 401 and r.json()["error"]["code"] == "unauthenticated"
