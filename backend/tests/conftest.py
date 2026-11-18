import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from cryptography.fernet import Fernet

# Configure the app for tests BEFORE any app module reads settings. Values already present in
# a developer's backend/.env are overridden here so tests never touch real credentials.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://cip:cip@localhost:5432/cip_test"
)
TEST_ORIGIN = "http://localhost:3000"
TEST_ENV = {
    "DATABASE_URL": TEST_DATABASE_URL,
    "ENVIRONMENT": "test",
    "GITHUB_TOKEN": "",
    # Tests never call a real embedding service or use a developer's credentials.
    "EMBEDDING_PROVIDER": "hash",
    "CLOUDFLARE_ACCOUNT_ID": "",
    "CLOUDFLARE_API_TOKEN": "",
    # Nor a real language model: tests that need one inject a fake.
    "OPENROUTER_API_KEY": "",
    "GITHUB_APP_CLIENT_ID": "test-client-id",
    "GITHUB_APP_CLIENT_SECRET": "test-client-secret-value",
    "SESSION_SECRET": "test-session-secret-0123456789abcdef",
    "TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    "APP_BASE_URL": TEST_ORIGIN,
    "CORS_ORIGINS": TEST_ORIGIN,
    "COOKIE_SECURE": "false",
    "FORWARDED_FOR_HOPS": "0",
}
# An empty value in the real environment beats any .env file, so a developer's own
# ALEMBIC_DATABASE_URL (e.g. a hosted database) can never be used by the tests.
os.environ.update(TEST_ENV)
os.environ["ALEMBIC_DATABASE_URL"] = ""

import pytest  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import Engine, create_engine, text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from alembic import command  # noqa: E402
from app.api.deps import (  # noqa: E402
    current_user,
    get_github,
    get_github_factory,
    get_oauth_or_none,
)
from app.core.config import get_settings  # noqa: E402
from app.db.models import User  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services.auth import sessions  # noqa: E402
from tests.helpers import FakeGitHub, FakeOAuth  # noqa: E402

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSION_COOKIE = "cip_session"  # COOKIE_SECURE=false, so no __Host- prefix


def _require_db() -> bool:
    # CI sets REQUIRE_DB=1 so a missing database fails loudly instead of skipping.
    return os.environ.get("REQUIRE_DB") == "1"


@dataclass
class Persona:
    id: uuid.UUID
    login: str
    token: str  # raw session cookie value


def build_app(fake_gh: FakeGitHub, fake_oauth: FakeOAuth | None) -> FastAPI:
    app = create_app()  # fresh app = fresh in-process rate limiter
    app.dependency_overrides[get_github] = lambda: fake_gh
    app.dependency_overrides[get_github_factory] = lambda: lambda: fake_gh
    if fake_oauth is not None:
        app.dependency_overrides[get_oauth_or_none] = lambda: fake_oauth
    return app


def make_client(
    app: FastAPI, persona: Persona | None = None, origin: str | None = TEST_ORIGIN
) -> TestClient:
    c = TestClient(app, raise_server_exceptions=False)
    if origin:
        c.headers["Origin"] = origin
    if persona:
        c.cookies.set(SESSION_COOKIE, persona.token)
    return c


@pytest.fixture(autouse=True)
def _public_provider_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never touch DNS: every provider host 'resolves' to a public address, unless a test
    says otherwise."""
    from app.services.providers import ssrf

    monkeypatch.setattr(ssrf, "resolver", lambda host: ["93.184.216.34"])


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    engine = create_engine(TEST_DATABASE_URL)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        engine.dispose()
        if _require_db():
            raise
        pytest.skip(f"Postgres not reachable at TEST_DATABASE_URL ({exc.__class__.__name__})")

    # Start from an empty schema so the migrations themselves are what gets tested.
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    cfg = Config(os.path.join(BACKEND_DIR, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    command.upgrade(cfg, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def db_clean(db_engine: Engine) -> Engine:
    with db_engine.begin() as conn:
        conn.execute(text("TRUNCATE repositories, users, audit_events CASCADE"))
    return db_engine


@pytest.fixture
def fake_gh() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def fake_oauth() -> FakeOAuth:
    return FakeOAuth()


@pytest.fixture
def make_user(db_engine: Engine) -> Callable[[int, str], Persona]:
    def _make(github_id: int, login: str) -> Persona:
        with Session(db_engine) as db:
            user = User(github_id=github_id, login=login, avatar_url=None)
            db.add(user)
            db.commit()
            token = sessions.create_session(db, user.id, get_settings())
            return Persona(id=user.id, login=login, token=token)

    return _make


@pytest.fixture
def client(fake_gh: FakeGitHub, fake_oauth: FakeOAuth) -> TestClient:
    """No database: the signed-in user is faked, so only code that never reaches the DB runs."""
    app = build_app(fake_gh, fake_oauth)
    unit_user = User(id=uuid.uuid4(), github_id=1, login="unit-user", avatar_url=None)
    app.dependency_overrides[current_user] = lambda: unit_user
    return make_client(app)


@pytest.fixture
def db_app(db_clean: Engine, fake_gh: FakeGitHub, fake_oauth: FakeOAuth) -> FastAPI:
    return build_app(fake_gh, fake_oauth)


@pytest.fixture
def alice(db_clean: Engine, make_user: Callable[[int, str], Persona]) -> Persona:
    return make_user(1001, "alice")


@pytest.fixture
def bob(db_clean: Engine, make_user: Callable[[int, str], Persona]) -> Persona:
    return make_user(1002, "bob")


@pytest.fixture
def db_client(db_app: FastAPI, alice: Persona) -> TestClient:
    """Real sessions and ownership, signed in as alice."""
    return make_client(db_app, alice)


@pytest.fixture
def other_client(db_app: FastAPI, bob: Persona) -> TestClient:
    return make_client(db_app, bob)


@pytest.fixture
def anon_client(db_app: FastAPI) -> TestClient:
    return make_client(db_app)
