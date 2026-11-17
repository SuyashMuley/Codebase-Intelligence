"""Server-side sessions. The cookie holds a random token; only its SHA-256 is stored."""

import hashlib
import secrets
from datetime import timedelta
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import User, UserSession

_TOUCH_INTERVAL = timedelta(minutes=5)  # avoid a write on every request


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: Session, user_id: UUID, settings: Settings) -> str:
    """Create a session and return the raw token for the cookie (256 bits of randomness)."""
    token = secrets.token_urlsafe(32)
    db.add(
        UserSession(
            id_hash=_hash(token),
            user_id=user_id,
            expires_at=func.now() + timedelta(days=settings.session_absolute_days),
        )
    )
    db.commit()
    return token


def resolve_session(db: Session, token: str, settings: Settings) -> User | None:
    """Return the session's user, or None if it is unknown, idle too long, or expired."""
    if not token or len(token) > 128:
        return None
    digest = _hash(token)
    row = db.execute(
        select(User)
        .join(UserSession, UserSession.user_id == User.id)
        .where(
            UserSession.id_hash == digest,
            UserSession.expires_at > func.now(),
            UserSession.last_seen_at > func.now() - timedelta(hours=settings.session_idle_hours),
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    result = db.execute(
        update(UserSession)
        .where(
            UserSession.id_hash == digest, UserSession.last_seen_at < func.now() - _TOUCH_INTERVAL
        )
        .values(last_seen_at=func.now())
    )
    if result.rowcount:  # type: ignore[attr-defined]
        db.commit()
    return row


def revoke_session(db: Session, token: str) -> None:
    db.execute(delete(UserSession).where(UserSession.id_hash == _hash(token)))
    db.commit()


def purge_expired(db: Session, settings: Settings) -> None:
    db.execute(
        delete(UserSession).where(
            (UserSession.expires_at <= func.now())
            | (
                UserSession.last_seen_at
                <= func.now() - timedelta(hours=settings.session_idle_hours)
            )
        )
    )
    db.commit()
