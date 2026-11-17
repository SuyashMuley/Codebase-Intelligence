"""Request dependencies: settings, auth, CSRF, ownership, rate limits, GitHub access."""

import uuid
from collections.abc import Callable
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import (
    AuthNotConfigured,
    EmbeddingNotConfigured,
    ForbiddenOrigin,
    ReauthRequired,
    RepoNotFound,
    TooManyRequests,
    Unauthenticated,
)
from app.core.ratelimit import client_ip, client_key
from app.db.models import Repository, User, UserRepository
from app.db.session import get_db
from app.providers.embeddings.base import Embedder
from app.providers.embeddings.factory import get_embedder as _build_embedder
from app.providers.llm.factory import get_llm as _build_llm
from app.providers.llm.openrouter import OpenRouterClient
from app.services.auth import credentials, sessions
from app.services.auth.crypto import StateSealer, TokenCipher
from app.services.auth.oauth import GitHubOAuth, GitHubOAuthGateway
from app.services.github.client import GitHubClient, GitHubGateway

AppSettings = Annotated[Settings, Depends(get_settings)]
DbSession = Annotated[Session, Depends(get_db)]

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


# --- auth building blocks (overridable in tests) --------------------------------------------


def session_cookie_name(settings: Settings) -> str:
    # The __Host- prefix makes browsers refuse the cookie unless it is Secure, host-only, Path=/.
    return "__Host-cip_session" if settings.cookie_secure else "cip_session"


@lru_cache
def _cipher(key: str) -> TokenCipher:
    return TokenCipher(key)


@lru_cache
def _sealer(secret: str) -> StateSealer:
    return StateSealer(secret)


@lru_cache
def _oauth(
    client_id: str, secret: str, redirect_uri: str, oauth_base: str, api_base: str
) -> GitHubOAuth:
    return GitHubOAuth(client_id, secret, redirect_uri, oauth_base=oauth_base, api_base=api_base)


def get_cipher_or_none(settings: AppSettings) -> TokenCipher | None:
    if not settings.auth_configured or settings.token_encryption_key is None:
        return None
    return _cipher(settings.token_encryption_key.get_secret_value())


def get_oauth_or_none(settings: AppSettings) -> GitHubOAuthGateway | None:
    if not settings.auth_configured:
        return None
    assert settings.github_app_client_id and settings.github_app_client_secret
    return _oauth(
        settings.github_app_client_id,
        settings.github_app_client_secret.get_secret_value(),
        settings.callback_url,
        settings.github_oauth_base,
        settings.github_api_base,
    )


def _not_configured() -> AuthNotConfigured:
    return AuthNotConfigured(
        "Sign-in is not configured on this server. See docs/GITHUB_APP_SETUP.md."
    )


# Strict variants for the sign-in routes. Tests override the *_or_none functions only.
def get_cipher(cipher: Annotated[TokenCipher | None, Depends(get_cipher_or_none)]) -> TokenCipher:
    if cipher is None:
        raise _not_configured()
    return cipher


def get_oauth(
    oauth: Annotated[GitHubOAuthGateway | None, Depends(get_oauth_or_none)],
) -> GitHubOAuthGateway:
    if oauth is None:
        raise _not_configured()
    return oauth


def get_sealer(settings: AppSettings) -> StateSealer:
    if not settings.auth_configured or settings.session_secret is None:
        raise _not_configured()
    return _sealer(settings.session_secret.get_secret_value())


# --- who is calling ---------------------------------------------------------------------------


def current_user(request: Request, db: DbSession, settings: AppSettings) -> User:
    token = request.cookies.get(session_cookie_name(settings))
    user = sessions.resolve_session(db, token, settings) if token else None
    if user is None:
        raise Unauthenticated("Sign in to continue.")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def csrf_guard(request: Request, settings: AppSettings) -> None:
    """Cookie-authenticated, state-changing requests must come from our own frontend.

    SameSite=Lax already blocks cross-site POSTs in modern browsers; this checks Origin too.
    """
    if request.method in SAFE_METHODS:
        return
    if session_cookie_name(settings) not in request.cookies:
        return  # nothing for an attacker to ride on
    origin = (request.headers.get("origin") or "").rstrip("/")
    if origin not in settings.allowed_origins:
        raise ForbiddenOrigin("Request origin is not allowed.")


# --- ownership ------------------------------------------------------------------------------


def get_owned_repo(repository_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Repository:
    """The repository, only if the caller connected it. 404 (not 403) so IDs cannot be probed."""
    repo = db.scalar(
        select(Repository)
        .join(UserRepository, UserRepository.repository_id == Repository.id)
        .where(Repository.id == repository_id, UserRepository.user_id == user.id)
    )
    if repo is None:
        raise RepoNotFound(f"Repository {repository_id} not found.")
    return repo


OwnedRepo = Annotated[Repository, Depends(get_owned_repo)]


# --- GitHub access --------------------------------------------------------------------------


@lru_cache
def _github_client() -> GitHubClient:
    s = get_settings()
    return GitHubClient(
        token=s.github_token.get_secret_value() if s.github_token else None,
        api_base=s.github_api_base,
        timeout=s.github_timeout_seconds,
    )


def get_github(
    user: CurrentUser,
    db: DbSession,
    settings: AppSettings,
    cipher: Annotated[TokenCipher | None, Depends(get_cipher_or_none)],
    oauth: Annotated[GitHubOAuthGateway | None, Depends(get_oauth_or_none)],
) -> GitHubGateway:
    """A GitHub client authenticated as the calling user, so each user has their own rate limit.

    Falls back to the optional platform token only when the user has no usable credentials.
    """
    base = _github_client()
    if cipher is None or oauth is None:
        if settings.github_token:
            return base
        raise _not_configured()
    try:
        token = credentials.get_access_token(db, user.id, cipher, oauth)
    except ReauthRequired:
        if settings.github_token:
            return base
        raise
    return base.bind(token)


# --- rate limits ----------------------------------------------------------------------------


def _hit(request: Request, key: str, limit: int) -> None:
    retry_after = request.app.state.limiter.hit(key, limit)
    if retry_after is not None:
        seconds = max(1, int(retry_after + 0.999))
        raise TooManyRequests(
            "Too many requests. Slow down and retry shortly.",
            details={"retry_after_seconds": seconds},
            headers={"Retry-After": str(seconds)},
        )


def ip_limit(bucket: str, per_minute: Callable[[Settings], int]) -> Callable[..., None]:
    def dependency(request: Request, settings: AppSettings) -> None:
        key = client_key(client_ip(request, settings.forwarded_for_hops), settings.client_key_salt)
        _hit(request, f"ip:{bucket}:{key}", per_minute(settings))

    return dependency


def user_limit(bucket: str, per_minute: Callable[[Settings], int]) -> Callable[..., None]:
    def dependency(request: Request, settings: AppSettings, user: CurrentUser) -> None:
        _hit(request, f"user:{bucket}:{user.id}", per_minute(settings))

    return dependency


ip_backstop = ip_limit("api", lambda s: s.ip_rate_limit_per_minute)
auth_ip_limit = ip_limit("auth", lambda s: s.auth_rate_limit_per_minute)
user_general_limit = user_limit("general", lambda s: s.rate_limit_per_minute)
user_index_limit = user_limit("index", lambda s: s.index_rate_limit_per_minute)
user_search_limit = user_limit("search", lambda s: s.search_rate_limit_per_minute)
user_ai_limit = user_limit("ai", lambda s: s.ai_rate_limit_per_minute)


def get_llm(settings: AppSettings) -> OpenRouterClient:
    """The configured chat model; raises LlmNotConfigured (HTTP 503) with the fix. Tests
    override this."""
    return _build_llm(settings)


def get_embedder(settings: AppSettings) -> Embedder | None:
    """The configured embedder, or None when embeddings are not set up (search then falls back
    to keyword search and says so). Tests override this."""
    try:
        return _build_embedder(settings)
    except EmbeddingNotConfigured:
        return None
