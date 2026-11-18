from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.dbrelease import ReleasingRouter
from app.core.errors import NotReady
from app.db.session import get_engine

router = ReleasingRouter(tags=["health"])

_ALEMBIC_DIR = Path(__file__).resolve().parents[2] / "alembic"


def _migration_head() -> str | None:
    cfg = Config()
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    return ScriptDirectory.from_config(cfg).get_current_head()


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness: the process is up. Does not touch the database."""
    return {"status": "ok"}


@router.get("/ready")
def ready() -> dict[str, str]:
    """Readiness: the database is reachable and migrations are at head."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
            current = MigrationContext.configure(conn).get_current_revision()
    except SQLAlchemyError:
        raise NotReady("Database is not reachable.", details={"check": "database"}) from None

    head = _migration_head()
    if current != head:
        raise NotReady(
            "Database migrations are not at head.",
            details={"check": "migrations", "current": current, "head": head},
        )
    return {"status": "ready", "migration": current or ""}
