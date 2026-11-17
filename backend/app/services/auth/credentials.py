"""Storage and refresh of a user's GitHub tokens."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.errors import ReauthRequired
from app.db.models import GithubCredential
from app.services.auth.crypto import CipherError, TokenCipher
from app.services.auth.oauth import GitHubOAuthGateway, TokenSet

_REFRESH_MARGIN = timedelta(seconds=60)


def save_tokens(db: Session, user_id: UUID, tokens: TokenSet, cipher: TokenCipher) -> None:
    row = db.get(GithubCredential, user_id)
    if row is None:
        row = GithubCredential(user_id=user_id, access_token_enc="")
        db.add(row)
    row.access_token_enc = cipher.encrypt(tokens.access_token)
    row.refresh_token_enc = cipher.encrypt(tokens.refresh_token) if tokens.refresh_token else None
    row.access_expires_at = tokens.access_expires_at
    row.refresh_expires_at = tokens.refresh_expires_at
    db.commit()


def delete_tokens(db: Session, user_id: UUID) -> None:
    db.execute(delete(GithubCredential).where(GithubCredential.user_id == user_id))
    db.commit()


def _needs_refresh(row: GithubCredential, now: datetime) -> bool:
    return row.access_expires_at is not None and row.access_expires_at <= now + _REFRESH_MARGIN


def get_access_token(
    db: Session,
    user_id: UUID,
    cipher: TokenCipher,
    oauth: GitHubOAuthGateway,
    now: datetime | None = None,
) -> str:
    """The user's current GitHub access token, refreshing it if it is about to expire.

    Raises ReauthRequired if there are no usable credentials; unusable ones are deleted.
    """
    now = now or datetime.now(UTC)
    row = db.get(GithubCredential, user_id)
    if row is None:
        raise ReauthRequired("Please sign in with GitHub.")
    try:
        if not _needs_refresh(row, now):
            return cipher.decrypt(row.access_token_enc)

        # GitHub refresh tokens are single-use, so serialize concurrent refreshes on this row.
        locked = db.execute(
            select(GithubCredential)
            .where(GithubCredential.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if locked is None:
            db.rollback()
            raise ReauthRequired("Please sign in with GitHub.")
        if not _needs_refresh(locked, now):  # another request refreshed it while we waited
            token = cipher.decrypt(locked.access_token_enc)
            db.rollback()
            return token

        refresh_expired = locked.refresh_expires_at is not None and locked.refresh_expires_at <= now
        if not locked.refresh_token_enc or refresh_expired:
            raise ReauthRequired("Your GitHub authorization expired. Please sign in again.")
        tokens = oauth.refresh(cipher.decrypt(locked.refresh_token_enc))
        save_tokens(db, user_id, tokens, cipher)
        return tokens.access_token
    except (ReauthRequired, CipherError):
        db.rollback()
        delete_tokens(db, user_id)  # unusable: force a clean sign-in
        raise ReauthRequired("Your GitHub authorization expired. Please sign in again.") from None
