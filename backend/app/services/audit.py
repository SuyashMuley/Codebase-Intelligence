"""The audit log: a record that a private repository was used, never of what was in it.

Only fixed action names, ids, "owner/name", and small non-sensitive facts (a provider's kind and
mode, a count) are written. Callers must never put file contents, questions, answers, prompts or
keys in `detail`; `record` also refuses values that are not short plain scalars, as a guard
against doing so by accident.
"""

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import request_id_var
from app.db.models import AuditEvent, Repository

_MAX_VALUE_CHARS = 60


def _clean(detail: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (detail or {}).items():
        if isinstance(value, bool | int | float) or value is None:
            out[key] = value
        elif isinstance(value, str) and len(value) <= _MAX_VALUE_CHARS and "\n" not in value:
            out[key] = value
        else:
            raise ValueError(f"audit detail {key!r} must be a short plain value")
    return out


def record(
    db: Session,
    action: str,
    user_id: uuid.UUID | None,
    repo: Repository | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditEvent(
            user_id=user_id,
            repository_id=repo.id if repo else None,
            repository=f"{repo.owner}/{repo.name}"[:140] if repo else None,
            action=action,
            detail=_clean(detail),
            request_id=request_id_var.get(),
        )
    )
    db.commit()
