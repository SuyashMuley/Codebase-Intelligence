"""Daily cap on model-backed questions, per user and repository.

A question the system declines because nothing relevant was found costs no model call, so it is
not counted. A question that fails because the model service did is refunded.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.errors import ChatQuotaExceeded
from app.db.models import ChatUsage


def _key(user_id: uuid.UUID) -> str:
    return f"user:{user_id}"


def reserve(db: Session, repository_id: uuid.UUID, user_id: uuid.UUID, limit: int) -> None:
    day = datetime.now(UTC).date()
    stmt = (
        insert(ChatUsage)
        .values(repository_id=repository_id, client_key=_key(user_id), day=day, count=1)
        .on_conflict_do_update(
            constraint="uq_chat_usage_repository_id_client_key_day",
            set_={"count": ChatUsage.count + 1},
        )
        .returning(ChatUsage.count)
    )
    count = db.execute(stmt).scalar_one()
    if count > limit:
        db.rollback()  # do not keep the increment of a refused request
        raise ChatQuotaExceeded(
            f"You have used today's {limit} questions for this repository. Try again tomorrow."
        )
    db.commit()


def refund(db: Session, repository_id: uuid.UUID, user_id: uuid.UUID) -> None:
    db.execute(
        update(ChatUsage)
        .where(
            ChatUsage.repository_id == repository_id,
            ChatUsage.client_key == _key(user_id),
            ChatUsage.day == datetime.now(UTC).date(),
        )
        .values(count=func.greatest(ChatUsage.count - 1, 0))
    )
    db.commit()
