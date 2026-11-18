import logging
import uuid
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.api.deps import get_oauth_or_none
from app.core.config import Settings, get_settings
from app.core.errors import GithubAuthFailed
from app.services.auth.crypto import TokenCipher, pkce_challenge
from app.services.auth.oauth import GitHubProfile
from tests.conftest import SESSION_COOKIE, TEST_ENV, TEST_ORIGIN, Persona, build_app, make_client
from tests.helpers import FakeGitHub, FakeOAuth

pytestmark = pytest.mark.db

STATE_COOKIE = "cip_oauth_state"


def set_cookies(resp: Any) -> dict[str, str]:
    """Raw Set-Cookie header lines by cookie name."""
    return {line.split("=", 1)[0]: line for line in resp.headers.get_list("set-cookie")}


def start_login(c: TestClient, next_path: str | None = None) -> str:
    r = c.get(
        "/api/auth/github/login",
        params={"next": next_path} if next_path else None,
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"].startswith("https://github.test/authorize")
    return str(parse_qs(urlsplit(r.headers["location"]).query)["state"][0])


def callback(c: TestClient, code: str, state: str, **extra: str) -> Any:
    return c.get(
        "/api/auth/github/callback",
        params={"code": code, "state": state, **extra},
        follow_redirects=False,
    )


def register_code(fake: FakeOAuth, code: str, gid: int = 5001, login: str = "carol") -> None:
    fake.codes[code] = GitHubProfile(gid, login, "https://avatars.githubusercontent.com/u/1")


def count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())


def sign_in(c: TestClient, fake: FakeOAuth, code: str = "c1", **kw: Any) -> Any:
    register_code(fake, code, **kw)
    return callback(c, code, start_login(c))


# --- protection ---------------------------------------------------------------------------


PROTECTED = [
    ("GET", "/api/me"),
    ("GET", "/api/repositories"),
    ("POST", "/api/repositories"),
    ("GET", f"/api/repositories/{uuid.uuid4()}"),
    ("POST", f"/api/repositories/{uuid.uuid4()}/index"),
    ("POST", f"/api/repositories/{uuid.uuid4()}/index/tick"),
    ("GET", f"/api/repositories/{uuid.uuid4()}/status"),
]


@pytest.mark.parametrize(("method", "path"), PROTECTED)
def test_every_protected_route_requires_a_session(
    anon_client: TestClient, method: str, path: str
) -> None:
    r = anon_client.request(method, path, json={"url": "https://github.com/o/r"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthenticated"


@pytest.mark.parametrize(("method", "path"), PROTECTED)
def test_garbage_session_cookie_is_rejected(db_app: FastAPI, method: str, path: str) -> None:
    c = make_client(db_app)
    c.cookies.set(SESSION_COOKIE, "x" * 43)
    r = c.request(method, path, json={"url": "https://github.com/o/r"})
    assert r.status_code == 401


def test_health_and_login_are_public(anon_client: TestClient) -> None:
    assert anon_client.get("/health").status_code == 200
    assert anon_client.get("/ready").status_code == 200
    assert anon_client.get("/api/auth/github/login", follow_redirects=False).status_code == 302


def test_me_returns_only_public_profile_fields(db_client: TestClient, alice: Persona) -> None:
    r = db_client.get("/api/me")
    assert r.status_code == 200
    assert set(r.json()) == {"id", "login", "avatar_url"}
    assert r.json()["login"] == "alice" and r.json()["id"] == str(alice.id)


# --- sign in -----------------------------------------------------------------------------


def test_login_redirects_with_state_and_pkce_and_a_sealed_cookie(
    anon_client: TestClient, fake_oauth: FakeOAuth
) -> None:
    r = anon_client.get("/api/auth/github/login", follow_redirects=False)
    query = parse_qs(urlsplit(r.headers["location"]).query)
    assert len(query["state"][0]) >= 43
    cookie = set_cookies(r)[STATE_COOKIE]
    lowered = cookie.lower()
    assert "httponly" in lowered and "samesite=lax" in lowered and "path=/api/auth" in lowered
    assert "max-age=600" in lowered
    value = cookie.split("=", 1)[1].split(";")[0]
    assert query["state"][0] not in value  # sealed: the browser cannot read or forge it
    assert fake_oauth.last_challenge == query["cc"][0]


def test_login_is_unavailable_when_not_configured(db_clean: Engine, fake_gh: FakeGitHub) -> None:
    app = build_app(fake_gh, None)
    base = get_settings()
    app.dependency_overrides[get_settings] = lambda: base.model_copy(
        update={"github_app_client_id": None}
    )
    r = make_client(app).get("/api/auth/github/login", follow_redirects=False)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "auth_not_configured"


def test_successful_callback_creates_user_session_and_encrypted_tokens(
    anon_client: TestClient, fake_oauth: FakeOAuth, db_engine: Engine
) -> None:
    r = sign_in(anon_client, fake_oauth, "c1", gid=5001, login="carol")
    assert r.status_code == 302
    assert r.headers["location"] == f"{TEST_ORIGIN}/repos"

    cookies = set_cookies(r)
    session = cookies[SESSION_COOKIE].lower()
    assert "httponly" in session and "samesite=lax" in session and "path=/" in session
    assert "max-age=0" in cookies[STATE_COOKIE].lower()  # one-time state is cleared

    # PKCE: the verifier we exchanged is the one whose challenge GitHub was given.
    assert fake_oauth.last_verifier is not None
    assert pkce_challenge(fake_oauth.last_verifier) == fake_oauth.last_challenge

    me = anon_client.get("/api/me")
    assert me.status_code == 200 and me.json()["login"] == "carol"

    with db_engine.connect() as conn:
        raw_token = anon_client.cookies.get(SESSION_COOKIE)
        assert raw_token is not None
        sess = conn.execute(text("SELECT id_hash FROM sessions")).scalar_one()
        assert sess != raw_token and len(sess) == 64  # only a hash is stored
        creds = conn.execute(
            text("SELECT access_token_enc, refresh_token_enc FROM github_credentials")
        ).one()
    assert "gho_access_c1" not in creds[0] and "ghr_refresh_c1" not in creds[1]
    cipher = TokenCipher(TEST_ENV["TOKEN_ENCRYPTION_KEY"])
    assert cipher.decrypt(creds[0]) == "gho_access_c1"
    assert cipher.decrypt(creds[1]) == "ghr_refresh_c1"


def test_signing_in_again_updates_the_same_account(
    anon_client: TestClient, db_app: FastAPI, fake_oauth: FakeOAuth, db_engine: Engine
) -> None:
    sign_in(anon_client, fake_oauth, "c1", gid=5001, login="carol")
    other = make_client(db_app)
    sign_in(other, fake_oauth, "c2", gid=5001, login="carol-renamed")
    with db_engine.connect() as conn:
        rows = conn.execute(text("SELECT github_id, login FROM users")).all()
    assert [(r[0], r[1]) for r in rows] == [(5001, "carol-renamed")]
    assert count(db_engine, "github_credentials") == 1


def test_login_issues_a_new_session_and_revokes_the_old_one(
    db_app: FastAPI, fake_oauth: FakeOAuth, db_engine: Engine
) -> None:
    c = make_client(db_app)
    sign_in(c, fake_oauth, "c1")
    first = c.cookies.get(SESSION_COOKIE)
    assert first
    sign_in(c, fake_oauth, "c2")  # still sending the first session cookie
    second = c.cookies.get(SESSION_COOKIE)
    assert second and second != first  # no session fixation

    stale = make_client(db_app)
    stale.cookies.set(SESSION_COOKIE, first)
    assert stale.get("/api/me").status_code == 401
    assert c.get("/api/me").status_code == 200
    assert count(db_engine, "sessions") == 1


@pytest.mark.parametrize(
    "evil", ["//evil.example", "https://evil.example/x", "/\\evil.example", "javascript:alert(1)"]
)
def test_next_parameter_cannot_redirect_off_site(
    anon_client: TestClient, fake_oauth: FakeOAuth, evil: str
) -> None:
    register_code(fake_oauth, "c1")
    r = callback(anon_client, "c1", start_login(anon_client, next_path=evil))
    assert r.headers["location"] == f"{TEST_ORIGIN}/repos"


def test_next_parameter_is_kept_when_local(anon_client: TestClient, fake_oauth: FakeOAuth) -> None:
    register_code(fake_oauth, "c1")
    r = callback(anon_client, "c1", start_login(anon_client, next_path="/repos/abc"))
    assert r.headers["location"] == f"{TEST_ORIGIN}/repos/abc"


# --- callback failures: never a session --------------------------------------------------


def assert_failed(r: Any, error: str, engine: Engine) -> None:
    assert r.status_code == 302
    assert r.headers["location"] == f"{TEST_ORIGIN}/login?error={error}"
    assert SESSION_COOKIE not in set_cookies(r)
    assert count(engine, "sessions") == 0 and count(engine, "users") == 0


def test_callback_without_state_cookie_is_rejected(
    anon_client: TestClient, db_app: FastAPI, fake_oauth: FakeOAuth, db_engine: Engine
) -> None:
    register_code(fake_oauth, "c1")
    state = start_login(anon_client)
    attacker = make_client(db_app)  # a different browser: no state cookie
    assert_failed(callback(attacker, "c1", state), "invalid_state", db_engine)


def test_callback_with_wrong_state_is_rejected(
    anon_client: TestClient, fake_oauth: FakeOAuth, db_engine: Engine
) -> None:
    register_code(fake_oauth, "c1")
    start_login(anon_client)
    assert_failed(callback(anon_client, "c1", "a-different-state"), "invalid_state", db_engine)


def test_callback_with_tampered_state_cookie_is_rejected(
    anon_client: TestClient, fake_oauth: FakeOAuth, db_engine: Engine
) -> None:
    register_code(fake_oauth, "c1")
    state = start_login(anon_client)
    # Replace the genuine cookie. (Some cookie jars keep both when only the value differs, which
    # would send the real one and test nothing.)
    anon_client.cookies.clear()
    anon_client.cookies.set(STATE_COOKIE, "forged-value", path="/api/auth")
    assert [c.value for c in anon_client.cookies.jar if c.name == STATE_COOKIE] == ["forged-value"]
    assert_failed(callback(anon_client, "c1", state), "invalid_state", db_engine)


def test_callback_after_state_expiry_is_rejected(
    anon_client: TestClient, db_app: FastAPI, fake_oauth: FakeOAuth, db_engine: Engine
) -> None:
    register_code(fake_oauth, "c1")
    state = start_login(anon_client)
    base = get_settings()
    db_app.dependency_overrides[get_settings] = lambda: base.model_copy(
        update={"login_state_ttl_seconds": -1}
    )
    assert_failed(callback(anon_client, "c1", state), "invalid_state", db_engine)


def test_replaying_a_callback_fails_the_second_time(
    anon_client: TestClient, fake_oauth: FakeOAuth, db_engine: Engine
) -> None:
    register_code(fake_oauth, "c1")
    state = start_login(anon_client)
    assert callback(anon_client, "c1", state).headers["location"].endswith("/repos")
    replay = callback(anon_client, "c1", state)
    assert replay.headers["location"].endswith("/login?error=invalid_state")  # state is one-time
    assert count(db_engine, "users") == 1


def test_user_denying_access_is_reported(anon_client: TestClient, db_engine: Engine) -> None:
    state = start_login(anon_client)
    r = anon_client.get(
        "/api/auth/github/callback",
        params={"error": "access_denied", "state": state},
        follow_redirects=False,
    )
    assert_failed(r, "access_denied", db_engine)


def test_rejected_code_exchange_creates_nothing(
    anon_client: TestClient, fake_oauth: FakeOAuth, db_engine: Engine
) -> None:
    state = start_login(anon_client)
    assert_failed(callback(anon_client, "never-issued", state), "github_auth_failed", db_engine)

    state = start_login(anon_client)
    fake_oauth.exchange_error = GithubAuthFailed("GitHub is unhappy.")
    assert_failed(callback(anon_client, "c1", state), "github_auth_failed", db_engine)


# --- session lifetime -------------------------------------------------------------------


def test_idle_session_expires(db_client: TestClient, db_engine: Engine) -> None:
    assert db_client.get("/api/me").status_code == 200
    with db_engine.begin() as conn:
        conn.execute(text("UPDATE sessions SET last_seen_at = now() - interval '25 hours'"))
    assert db_client.get("/api/me").status_code == 401


def test_session_past_absolute_expiry_is_rejected(db_client: TestClient, db_engine: Engine) -> None:
    with db_engine.begin() as conn:
        conn.execute(text("UPDATE sessions SET expires_at = now() - interval '1 second'"))
    assert db_client.get("/api/me").status_code == 401


def test_active_session_is_touched_but_not_on_every_request(
    db_client: TestClient, db_engine: Engine
) -> None:
    with db_engine.begin() as conn:
        conn.execute(text("UPDATE sessions SET last_seen_at = now() - interval '10 minutes'"))
    db_client.get("/api/me")
    with db_engine.connect() as conn:
        age = conn.execute(
            text("SELECT extract(epoch FROM now() - last_seen_at) FROM sessions")
        ).scalar_one()
    assert age < 60  # refreshed
    with db_engine.begin() as conn:
        conn.execute(text("UPDATE sessions SET last_seen_at = now() - interval '1 minute'"))
    db_client.get("/api/me")
    with db_engine.connect() as conn:
        age = conn.execute(
            text("SELECT extract(epoch FROM now() - last_seen_at) FROM sessions")
        ).scalar_one()
    assert age >= 60  # a recent session is left alone (no write per request)


# --- sign out ---------------------------------------------------------------------------


def test_logout_removes_session_and_stored_tokens(
    db_app: FastAPI, fake_oauth: FakeOAuth, db_engine: Engine
) -> None:
    c = make_client(db_app)
    sign_in(c, fake_oauth)
    assert count(db_engine, "github_credentials") == 1
    r = c.post("/api/auth/logout")
    assert r.status_code == 204
    assert "max-age=0" in set_cookies(r)[SESSION_COOKIE].lower()
    assert count(db_engine, "sessions") == 0
    assert count(db_engine, "github_credentials") == 0  # the server holds no tokens for them
    assert c.get("/api/me").status_code == 401


def test_logout_without_a_session_is_harmless(anon_client: TestClient) -> None:
    assert anon_client.post("/api/auth/logout").status_code == 204


# --- CSRF -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "origin", [None, "https://evil.example", "http://localhost:3000.evil.example", "null", ""]
)
def test_state_changing_requests_need_an_allowed_origin(
    db_app: FastAPI, alice: Persona, origin: str | None
) -> None:
    c = make_client(db_app, alice, origin=origin)
    for path, body in [
        ("/api/repositories", {"url": "https://github.com/o/r"}),
        ("/api/auth/logout", None),
    ]:
        r = c.post(path, json=body)
        assert r.status_code == 403, path
        assert r.json()["error"]["code"] == "forbidden_origin"
    # Nothing happened: the session survived and no repository was created.
    assert make_client(db_app, alice).get("/api/repositories").json()["total"] == 0


def test_safe_methods_and_trailing_slash_origin_are_fine(db_app: FastAPI, alice: Persona) -> None:
    assert make_client(db_app, alice, origin=None).get("/api/me").status_code == 200
    c = make_client(db_app, alice, origin=TEST_ORIGIN + "/")
    assert c.post("/api/repositories", json={"url": "https://github.com/o/r"}).status_code == 201


# --- rate limits ------------------------------------------------------------------------


def test_sign_in_endpoints_are_rate_limited_per_ip(anon_client: TestClient) -> None:
    codes = [
        anon_client.get("/api/auth/github/login", follow_redirects=False).status_code
        for _ in range(31)
    ]
    assert codes[:30] == [302] * 30
    assert codes[30] == 429


def test_rate_limits_are_per_user(db_client: TestClient, other_client: TestClient) -> None:
    rid = db_client.post("/api/repositories", json={"url": "https://github.com/o/r"}).json()["id"]
    codes = [db_client.post(f"/api/repositories/{rid}/index").status_code for _ in range(11)]
    assert codes[10] == 429
    # Another user, even from the same IP, has their own budget.
    other_rid = other_client.post(
        "/api/repositories", json={"url": "https://github.com/o/r"}
    ).json()["id"]
    assert other_client.post(f"/api/repositories/{other_rid}/index").status_code in (200, 202)


# --- secrets never leak -----------------------------------------------------------------


def test_no_secret_appears_in_logs_or_responses(
    db_app: FastAPI, fake_oauth: FakeOAuth, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    c = make_client(db_app)
    bodies = []
    r = sign_in(c, fake_oauth, "c1")
    session_token = c.cookies.get(SESSION_COOKIE)
    assert session_token
    bodies.append(r.text)
    for resp in (
        c.get("/api/me"),
        c.post("/api/repositories", json={"url": "https://github.com/o/r"}),
        c.get("/api/repositories"),
        c.post("/api/auth/logout"),
    ):
        bodies.append(resp.text)

    secrets = [
        "gho_access_c1",
        "ghr_refresh_c1",
        session_token,
        TEST_ENV["GITHUB_APP_CLIENT_SECRET"],
        TEST_ENV["SESSION_SECRET"],
        TEST_ENV["TOKEN_ENCRYPTION_KEY"],
    ]
    haystack = caplog.text + "\n".join(bodies)
    for secret in secrets:
        assert secret not in haystack


def test_settings_repr_does_not_reveal_secrets() -> None:
    text_repr = repr(Settings()) + str(Settings().model_dump())
    for secret in (TEST_ENV["GITHUB_APP_CLIENT_SECRET"], TEST_ENV["SESSION_SECRET"]):
        assert secret not in text_repr


def test_overriding_oauth_is_the_only_way_tests_bypass_github(db_app: FastAPI) -> None:
    # Guard against accidentally testing against the real GitHub: the override must be in place.
    assert get_oauth_or_none in db_app.dependency_overrides
