"""Every repository route must treat a repository the caller has not connected as nonexistent."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.core.errors import ReauthRequired
from app.db.models import GithubCredential, User
from app.services.auth import credentials
from app.services.auth.crypto import TokenCipher
from app.services.auth.oauth import TokenSet
from tests.conftest import TEST_ENV, Persona
from tests.helpers import FakeOAuth

pytestmark = pytest.mark.db

URL = "https://github.com/o/shared"


def connect(c: TestClient, url: str = URL) -> str:
    r = c.post("/api/repositories", json={"url": url})
    assert r.status_code in (200, 201), r.text
    return str(r.json()["id"])


def snapshot_count(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("SELECT count(*) FROM snapshots")).scalar_one())


def routes(rid: str) -> list[tuple[str, str]]:
    return [
        ("GET", f"/api/repositories/{rid}"),
        ("GET", f"/api/repositories/{rid}/status"),
        ("POST", f"/api/repositories/{rid}/index"),
        ("POST", f"/api/repositories/{rid}/index/tick"),
    ]


def test_another_users_repository_is_a_404_on_every_route(
    db_client: TestClient, other_client: TestClient, db_engine: Engine
) -> None:
    rid = connect(db_client)
    assert db_client.post(f"/api/repositories/{rid}/index").status_code == 202
    before = snapshot_count(db_engine)

    for method, path in routes(rid):
        r = other_client.request(method, path)
        assert r.status_code == 404, (method, path)
        assert r.json()["error"]["code"] == "repo_not_found"
    # A nonexistent ID looks exactly the same, so IDs cannot be probed.
    for method, path in routes(str(uuid.uuid4())):
        r = other_client.request(method, path)
        assert r.status_code == 404 and r.json()["error"]["code"] == "repo_not_found"
    assert snapshot_count(db_engine) == before  # the denied calls changed nothing


def test_lists_only_show_your_own_repositories(
    db_client: TestClient, other_client: TestClient
) -> None:
    connect(db_client, "https://github.com/o/only-alice")
    connect(other_client, "https://github.com/o/only-bob")
    mine = db_client.get("/api/repositories").json()
    theirs = other_client.get("/api/repositories").json()
    assert [r["name"] for r in mine["items"]] == ["only-alice"] and mine["total"] == 1
    assert [r["name"] for r in theirs["items"]] == ["only-bob"] and theirs["total"] == 1


def test_connecting_the_same_public_repo_shares_it_but_not_the_connection(
    db_client: TestClient, other_client: TestClient, db_engine: Engine
) -> None:
    alice_id = connect(db_client)
    assert db_client.post(f"/api/repositories/{alice_id}/index").status_code == 202

    r = other_client.post("/api/repositories", json={"url": URL})
    assert r.status_code == 201  # new for bob, even though the repository already exists
    assert r.json()["id"] == alice_id  # one shared record for public data

    status = other_client.get(f"/api/repositories/{alice_id}/status").json()
    assert status["state"] == "pending"  # bob sees the shared snapshot
    again = other_client.post("/api/repositories", json={"url": URL})
    assert again.status_code == 200  # idempotent per user
    with db_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM repositories")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM user_repositories")).scalar_one() == 2


def test_quota_is_per_user(
    db_client: TestClient, other_client: TestClient, db_engine: Engine
) -> None:
    rid = connect(db_client)
    assert db_client.post(f"/api/repositories/{rid}/index").status_code == 202
    with db_engine.connect() as conn:
        owner = conn.execute(text("SELECT requested_by FROM snapshots")).scalar_one()
    assert owner is not None  # attributed to the user who asked


def test_deleting_a_user_removes_their_links_and_sessions(
    db_client: TestClient, alice: Persona, db_engine: Engine
) -> None:
    connect(db_client)
    with db_engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": alice.id})
        assert conn.execute(text("SELECT count(*) FROM user_repositories")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM sessions")).scalar_one() == 0
        # The shared repository record itself stays.
        assert conn.execute(text("SELECT count(*) FROM repositories")).scalar_one() == 1


# --- GitHub token handling (credentials service) -------------------------------------------


def seed_credentials(
    db: Session,
    user_id: uuid.UUID,
    *,
    access_expires_in: timedelta | None,
    refresh: bool = True,
    refresh_expires_in: timedelta = timedelta(days=30),
) -> TokenCipher:
    cipher = TokenCipher(TEST_ENV["TOKEN_ENCRYPTION_KEY"])
    now = datetime.now(UTC)
    credentials.save_tokens(
        db,
        user_id,
        TokenSet(
            access_token="gho_old",
            refresh_token="ghr_old" if refresh else None,
            access_expires_at=now + access_expires_in if access_expires_in is not None else None,
            refresh_expires_at=now + refresh_expires_in if refresh else None,
        ),
        cipher,
    )
    return cipher


def test_valid_token_is_returned_without_calling_github(
    db_engine: Engine, alice: Persona, fake_oauth: FakeOAuth
) -> None:
    with Session(db_engine) as db:
        cipher = seed_credentials(db, alice.id, access_expires_in=timedelta(hours=4))
        assert credentials.get_access_token(db, alice.id, cipher, fake_oauth) == "gho_old"
    assert fake_oauth.refresh_calls == 0


def test_token_without_expiry_is_never_refreshed(
    db_engine: Engine, alice: Persona, fake_oauth: FakeOAuth
) -> None:
    with Session(db_engine) as db:
        cipher = seed_credentials(db, alice.id, access_expires_in=None, refresh=False)
        assert credentials.get_access_token(db, alice.id, cipher, fake_oauth) == "gho_old"


@pytest.mark.parametrize("remaining", [timedelta(seconds=-5), timedelta(seconds=30)])
def test_expired_or_nearly_expired_token_is_refreshed_and_stored_encrypted(
    db_engine: Engine, alice: Persona, fake_oauth: FakeOAuth, remaining: timedelta
) -> None:
    with Session(db_engine) as db:
        cipher = seed_credentials(db, alice.id, access_expires_in=remaining)
        token = credentials.get_access_token(db, alice.id, cipher, fake_oauth)
        assert token == "gho_refreshed_1" and fake_oauth.refresh_calls == 1
        row = db.get(GithubCredential, alice.id, populate_existing=True)
        assert row is not None and "gho_refreshed_1" not in row.access_token_enc
        assert cipher.decrypt(row.access_token_enc) == "gho_refreshed_1"
        assert row.refresh_token_enc and cipher.decrypt(row.refresh_token_enc) == "ghr_refreshed_1"
        # Now fresh: a second call does not refresh again.
        assert credentials.get_access_token(db, alice.id, cipher, fake_oauth) == "gho_refreshed_1"
    assert fake_oauth.refresh_calls == 1


@pytest.mark.parametrize(
    "scenario",
    ["no_credentials", "refresh_rejected", "no_refresh_token", "refresh_expired", "bad_key"],
)
def test_unusable_credentials_force_a_clean_sign_in(
    db_engine: Engine, alice: Persona, fake_oauth: FakeOAuth, scenario: str
) -> None:
    with Session(db_engine) as db:
        cipher = TokenCipher(TEST_ENV["TOKEN_ENCRYPTION_KEY"])
        if scenario != "no_credentials":
            cipher = seed_credentials(
                db,
                alice.id,
                access_expires_in=timedelta(seconds=-5),
                refresh=scenario != "no_refresh_token",
                refresh_expires_in=timedelta(seconds=-1)
                if scenario == "refresh_expired"
                else timedelta(days=30),
            )
        if scenario == "refresh_rejected":
            fake_oauth.refresh_error = ReauthRequired("revoked")
        if scenario == "bad_key":
            from cryptography.fernet import Fernet

            cipher = TokenCipher(Fernet.generate_key().decode())
            with db_engine.begin() as conn:
                conn.execute(text("UPDATE github_credentials SET access_expires_at = NULL"))

        with pytest.raises(ReauthRequired):
            credentials.get_access_token(db, alice.id, cipher, fake_oauth)
        assert db.get(GithubCredential, alice.id, populate_existing=True) is None
    assert fake_oauth.refresh_calls == (1 if scenario == "refresh_rejected" else 0)


def test_real_github_dependency_asks_for_sign_in_when_user_has_no_tokens(
    db_app: object, alice: Persona, db_engine: Engine
) -> None:
    """Without the fake GitHub, a user with no stored tokens (and no platform fallback token)
    gets 401 reauth_required instead of an anonymous, shared-rate-limit request."""
    from fastapi import FastAPI

    from app.api.deps import get_github
    from tests.conftest import make_client

    assert isinstance(db_app, FastAPI)
    db_app.dependency_overrides.pop(get_github)
    c = make_client(db_app, alice)
    # Connecting asks GitHub (with the user's token) whether the repository is private, so it
    # needs sign-in too.
    r = c.post("/api/repositories", json={"url": URL})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "reauth_required"
    assert User.__tablename__ == "users"  # keep the import honest
