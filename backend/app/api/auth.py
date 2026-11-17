"""Sign in with GitHub (GitHub App user authorization), sign out, and who-am-I."""

import hmac
import json
import logging
from typing import Annotated
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.api.deps import (
    AppSettings,
    CurrentUser,
    DbSession,
    auth_ip_limit,
    csrf_guard,
    get_cipher,
    get_oauth,
    get_sealer,
    ip_backstop,
    session_cookie_name,
    user_general_limit,
)
from app.core.errors import AppError
from app.db.models import User
from app.schemas.auth import MeOut
from app.schemas.repository import ErrorEnvelope
from app.services.auth import credentials, sessions
from app.services.auth.crypto import (
    CipherError,
    StateSealer,
    TokenCipher,
    new_pkce_verifier,
    new_state,
    pkce_challenge,
)
from app.services.auth.oauth import GitHubOAuthGateway

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["auth"])

STATE_COOKIE = "cip_oauth_state"
DEFAULT_NEXT = "/repos"

Sealer = Annotated[StateSealer, Depends(get_sealer)]
Oauth = Annotated[GitHubOAuthGateway, Depends(get_oauth)]
Cipher = Annotated[TokenCipher, Depends(get_cipher)]


def safe_next(value: str | None) -> str:
    """Only allow same-site relative paths, so sign-in cannot be used as an open redirect."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return DEFAULT_NEXT
    if any(ord(c) < 0x20 for c in value) or urlsplit(value).netloc:
        return DEFAULT_NEXT
    return value[:200]


def _fail(settings: AppSettings, code: str) -> RedirectResponse:
    """Send the browser back to the frontend with a short error code, and clear login state."""
    resp = RedirectResponse(
        f"{settings.app_base_url.rstrip('/')}/login?error={quote(code)}", status_code=302
    )
    resp.delete_cookie(STATE_COOKIE, path="/api/auth")
    return resp


@router.get(
    "/auth/github/login",
    status_code=status.HTTP_302_FOUND,
    dependencies=[Depends(auth_ip_limit)],
    responses={503: {"model": ErrorEnvelope, "description": "Sign-in not configured"}},
)
def login(
    settings: AppSettings, oauth: Oauth, sealer: Sealer, next: str | None = None
) -> RedirectResponse:
    """Start sign-in: redirect to GitHub with a one-time state and PKCE challenge."""
    state, verifier = new_state(), new_pkce_verifier()
    sealed = sealer.seal(json.dumps({"s": state, "v": verifier, "n": safe_next(next)}))
    resp = RedirectResponse(oauth.authorize_url(state, pkce_challenge(verifier)), status_code=302)
    resp.set_cookie(
        STATE_COOKIE,
        sealed,
        max_age=settings.login_state_ttl_seconds,
        path="/api/auth",
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",  # must accompany the top-level redirect back from GitHub
    )
    return resp


@router.get(
    "/auth/github/callback",
    status_code=status.HTTP_302_FOUND,
    dependencies=[Depends(auth_ip_limit)],
)
def callback(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    oauth: Oauth,
    sealer: Sealer,
    cipher: Cipher,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    sealed = request.cookies.get(STATE_COOKIE)
    if not sealed or not state:
        return _fail(settings, "invalid_state")
    try:
        payload = json.loads(sealer.open(sealed, settings.login_state_ttl_seconds))
        expected, verifier, next_path = payload["s"], payload["v"], safe_next(payload.get("n"))
    except (CipherError, ValueError, KeyError, TypeError):
        return _fail(settings, "invalid_state")
    # Compare in constant time; the state cookie is cleared by every response below.
    if not hmac.compare_digest(expected.encode(), state.encode()):
        return _fail(settings, "invalid_state")
    if error or not code or len(code) > 200:
        return _fail(settings, "access_denied" if error == "access_denied" else "github_error")

    try:
        tokens = oauth.exchange_code(code, verifier)
        profile = oauth.fetch_profile(tokens.access_token)
    except AppError as exc:
        logger.warning("sign_in_failed", extra={"code": exc.code})
        return _fail(settings, exc.code)

    user = _upsert_user(db, profile.id, profile.login, profile.avatar_url)
    credentials.save_tokens(db, user.id, tokens, cipher)

    # Fresh session at every login (no session fixation); drop any session the browser sent.
    old = request.cookies.get(session_cookie_name(settings))
    if old:
        sessions.revoke_session(db, old)
    sessions.purge_expired(db, settings)
    token = sessions.create_session(db, user.id, settings)

    resp = RedirectResponse(f"{settings.app_base_url.rstrip('/')}{next_path}", status_code=302)
    resp.delete_cookie(STATE_COOKIE, path="/api/auth")
    resp.set_cookie(
        session_cookie_name(settings),
        token,
        max_age=settings.session_absolute_days * 86400,
        path="/",
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )
    logger.info("sign_in", extra={"user_id": str(user.id)})
    return resp


def _upsert_user(db: Session, github_id: int, login: str, avatar_url: str | None) -> User:
    stmt = (
        pg_insert(User)
        .values(github_id=github_id, login=login, avatar_url=avatar_url)
        .on_conflict_do_update(
            index_elements=[User.github_id],
            set_={"login": login, "avatar_url": avatar_url, "last_login_at": func.now()},
        )
    )
    db.execute(stmt)
    db.commit()
    return db.execute(select(User).where(User.github_id == github_id)).scalar_one()


@router.post(
    "/auth/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(ip_backstop), Depends(csrf_guard)],
)
def logout(request: Request, db: DbSession, settings: AppSettings) -> Response:
    """End the session and delete the stored GitHub tokens."""
    name = session_cookie_name(settings)
    token = request.cookies.get(name)
    if token:
        user = sessions.resolve_session(db, token, settings)
        sessions.revoke_session(db, token)
        if user is not None:
            credentials.delete_tokens(db, user.id)
    resp = Response(status_code=status.HTTP_204_NO_CONTENT)
    resp.delete_cookie(name, path="/")
    return resp


@router.get(
    "/me",
    response_model=MeOut,
    dependencies=[Depends(ip_backstop), Depends(user_general_limit)],
    responses={401: {"model": ErrorEnvelope, "description": "Not signed in"}},
)
def me(user: CurrentUser) -> User:
    return user
