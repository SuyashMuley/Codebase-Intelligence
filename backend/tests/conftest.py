import os
from collections.abc import Iterator

# Point the app at the test database before any app module reads settings.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://cip:cip@localhost:5432/cip_test"
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.pop("ALEMBIC_DATABASE_URL", None)

import pytest  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import Engine, create_engine, text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from alembic import command  # noqa: E402
from app.api.deps import get_github  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.helpers import FakeGitHub  # noqa: E402

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _require_db() -> bool:
    # CI sets REQUIRE_DB=1 so a missing database fails loudly instead of skipping.
    return os.environ.get("REQUIRE_DB") == "1"


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

    # Start from an empty schema so the migration itself is what gets tested.
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
        conn.execute(text("TRUNCATE repositories CASCADE"))
    return db_engine


@pytest.fixture
def fake_gh() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def client(fake_gh: FakeGitHub) -> Iterator[TestClient]:
    app = create_app()  # fresh app = fresh in-process rate limiter
    app.dependency_overrides[get_github] = lambda: fake_gh
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def db_client(db_clean: Engine, client: TestClient) -> TestClient:
    return client
