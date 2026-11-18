"""Housekeeping for a scheduler. Not for browsers: it takes a bearer token, never a session."""

import hmac
from typing import Annotated

from fastapi import Depends, Header
from pydantic import BaseModel

from app.api.deps import AppSettings, DbSession, ip_limit
from app.core.dbrelease import ReleasingRouter
from app.core.errors import AppError, RepoNotFound
from app.services import audit
from app.services.repos import retention

# A stricter per-IP limit than the general one: this route is not for interactive use.
_maintenance_limit = ip_limit("maintenance", lambda s: 10)

router = ReleasingRouter(
    prefix="/api/internal", tags=["maintenance"], dependencies=[Depends(_maintenance_limit)]
)


class BadMaintenanceToken(AppError):
    status_code = 401
    code = "maintenance_unauthorized"


class PurgeOut(BaseModel):
    deleted_snapshots: int
    deleted_audit_events: int


@router.post("/purge-expired", response_model=PurgeOut)
def purge_expired(
    db: DbSession,
    settings: AppSettings,
    authorization: Annotated[str | None, Header()] = None,
) -> PurgeOut:
    """Delete private snapshots older than the retention period. Idempotent."""
    if settings.maintenance_token is None:
        raise RepoNotFound("Not found.")  # the endpoint does not exist unless it is configured
    expected = settings.maintenance_token.get_secret_value()
    presented = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(presented.encode(), expected.encode()):
        raise BadMaintenanceToken("Invalid maintenance token.")
    snapshots = retention.purge_expired(db, settings)
    if snapshots:
        audit.record(db, "retention.purge", None, None, {"snapshots": snapshots})
    return PurgeOut(
        deleted_snapshots=snapshots, deleted_audit_events=retention.purge_audit(db, settings)
    )
