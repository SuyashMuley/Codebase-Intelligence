"""Your own audit trail: when your private repositories and providers were used."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, ip_backstop, user_general_limit
from app.core.dbrelease import ReleasingRouter
from app.db.models import AuditEvent
from app.schemas.repository import ErrorEnvelope

router = ReleasingRouter(
    prefix="/api/audit",
    tags=["audit"],
    dependencies=[Depends(ip_backstop), Depends(user_general_limit)],
)


class AuditEventOut(BaseModel):
    at: datetime
    action: str
    repository: str | None
    repository_id: uuid.UUID | None
    detail: dict[str, Any]


class AuditList(BaseModel):
    items: list[AuditEventOut]


@router.get(
    "",
    response_model=AuditList,
    responses={401: {"model": ErrorEnvelope, "description": "Not signed in"}},
)
def my_audit_events(
    db: DbSession,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AuditList:
    """The most recent events about your own private repositories and providers. It says that
    something happened and when, never what was in it."""
    rows = db.scalars(
        select(AuditEvent)
        .where(AuditEvent.user_id == user.id)
        .order_by(AuditEvent.at.desc(), AuditEvent.id.desc())
        .limit(limit)
    ).all()
    return AuditList(
        items=[
            AuditEventOut(
                at=r.at,
                action=r.action,
                repository=r.repository,
                repository_id=r.repository_id,
                detail=r.detail,
            )
            for r in rows
        ]
    )
