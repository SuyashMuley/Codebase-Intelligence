"""Request dependencies: settings, auth, CSRF, ownership, rate limits, GitHub access."""

import uuid
from collections.abc import Callable, Iterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import (
    AppError,
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
from app.services import audit
from app.services.auth import credentials, sessions
from app.services.auth.crypto import StateSealer, TokenCipher
from app.services.auth.oauth import GitHubOAuth, GitHubOAuthGateway
from app.services.github.client import GitHubClient, GitHubGateway
from app.services.providers.resolve import RepoProviders
from app.services.providers.resolve import for_repository as providers_for_repository
from app.services.qa.graph import ChatModel
from app.services.repos import retention
from app.services.repos.access import ensure_private_access

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


def release(db: Session) -> None:
    """End the current (read-only) transaction so the connection goes back to the pool now.

    FastAPI runs each synchronous dependency and the endpoint as separate tasks on a shared pool of
    worker threads. A connection held *between* those tasks is held while the request waits for a
    free thread, and under load every connection ends up held by a request waiting for a thread
    while every thread waits for a connection: nothing moves until the pool times out. So a
    dependency that only reads calls this before it returns. Loaded objects stay usable (the
    session does not expire them on commit), and the endpoint starts its own transaction.
    """
    db.commit()


def current_user(request: Request, db: DbSession, settings: AppSettings) -> User:
    token = request.cookies.get(session_cookie_name(settings))
    user = sessions.resolve_session(db, token, settings) if token else None
    if user is None:
        db.rollback()  # nothing was loaded; do not hold the connection while the error is built
        raise Unauthenticated("Sign in to continue.")
    release(db)
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


# --- GitHub access --------------------------------------------------------------------------


@lru_cache
def _github_client() -> GitHubClient:
    s = get_settings()
    return GitHubClient(
        token=s.github_token.get_secret_value() if s.github_token else None,
        api_base=s.github_api_base,
        timeout=s.github_timeout_seconds,
    )


def github_for_user(
    user_id: uuid.UUID,
    db: Session,
    settings: Settings,
    cipher: TokenCipher | None,
    oauth: GitHubOAuthGateway | None,
) -> GitHubGateway:
    """A GitHub client authenticated as this user (also used by the background worker)."""
    base = _github_client()
    if cipher is None or oauth is None:
        if settings.github_token:
            return base
        raise _not_configured()
    try:
        token = credentials.get_access_token(db, user_id, cipher, oauth)
    except ReauthRequired:
        if settings.github_token:
            return base
        raise
    return base.bind(token)


def _build_github(
    user: User,
    db: Session,
    settings: Settings,
    cipher: TokenCipher | None,
    oauth: GitHubOAuthGateway | None,
) -> GitHubGateway:
    return github_for_user(user.id, db, settings, cipher, oauth)


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
    return _build_github(user, db, settings, cipher, oauth)


GitHubFactory = Callable[[], GitHubGateway]


def get_github_factory(
    user: CurrentUser,
    db: DbSession,
    settings: AppSettings,
    cipher: Annotated[TokenCipher | None, Depends(get_cipher_or_none)],
    oauth: Annotated[GitHubOAuthGateway | None, Depends(get_oauth_or_none)],
) -> GitHubFactory:
    """A client built only when called, so routes that rarely need GitHub (every route of a
    public repository) do not load or refresh the user's token. Tests override this."""
    return lambda: _build_github(user, db, settings, cipher, oauth)


# --- ownership ------------------------------------------------------------------------------


# Called every few seconds while a page is open: recording each call would bury the real events.
_UNAUDITED_ENDPOINTS = frozenset({"get_status", "tick_index"})


def get_owned_repo_unchecked(
    repository_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> Repository:
    """The repository, only if the caller connected it. 404 (not 403) so IDs cannot be probed.

    No GitHub recheck: used only where the user must still be able to reach a private
    repository whose access was lost (to see it, and to disconnect it and delete its data)."""
    repo = db.scalar(
        select(Repository)
        .join(UserRepository, UserRepository.repository_id == Repository.id)
        .where(Repository.id == repository_id, UserRepository.user_id == user.id)
    )
    release(db)  # found or not: see release()
    if repo is None:
        raise RepoNotFound(f"Repository {repository_id} not found.")
    return repo


GitHubFactoryDep = Annotated[GitHubFactory, Depends(get_github_factory)]


def get_owned_repo(
    request: Request,
    repo: Annotated[Repository, Depends(get_owned_repo_unchecked)],
    db: DbSession,
    settings: AppSettings,
    github: GitHubFactoryDep,
) -> Repository:
    """As above, and for a private repository GitHub is asked whether the owner can still see
    it (see services/repos/access.py). Every route that reads or changes repository data uses
    this one."""
    if repo.is_private:
        ensure_private_access(db, repo, github(), settings)
        # Expired data is removed before anything can read it.
        purged = retention.purge_repository(db, repo, settings)
        if purged:
            audit.record(
                db, "private.retention_purge", repo.owner_user_id, repo, {"snapshots": purged}
            )
        endpoint = getattr(request.scope.get("endpoint"), "__name__", "unknown")
        if endpoint not in _UNAUDITED_ENDPOINTS:
            audit.record(db, f"private.{endpoint}", repo.owner_user_id, repo)
    return repo


OwnedRepo = Annotated[Repository, Depends(get_owned_repo)]
OwnedRepoUnchecked = Annotated[Repository, Depends(get_owned_repo_unchecked)]


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
user_chat_limit = user_limit("chat", lambda s: s.chat_rate_limit_per_minute)


def get_llm(settings: AppSettings) -> ChatModel | AppError:
    """The platform's chat model, or the error that says why it is not configured. It returns the
    error instead of raising so a private repository (which never uses it) is not refused because
    the platform has no key. Callers go through RepoProviders.need_llm(). Tests override this."""
    try:
        return _build_llm(settings)
    except AppError as exc:
        return exc


def get_embedder(settings: AppSettings) -> Embedder | None:
    """The configured embedder, or None when embeddings are not set up (search then falls back
    to keyword search and says so). Tests override this."""
    try:
        return _build_embedder(settings)
    except EmbeddingNotConfigured:
        return None


def get_repo_providers(
    request: Request,
    repo: OwnedRepo,
    user: CurrentUser,
    db: DbSession,
    settings: AppSettings,
    cipher: Annotated[TokenCipher | None, Depends(get_cipher_or_none)],
    platform_embedder: Annotated[Embedder | None, Depends(get_embedder)],
    platform_llm: Annotated[ChatModel | AppError, Depends(get_llm)],
) -> Iterator[RepoProviders]:
    """The embedding and chat providers this request may use for this repository: the platform's
    for a public one, only the owner's own for a private one (see services/providers/resolve.py).
    Browser-held keys arrive in the X-Embedding-Key and X-Chat-Key headers."""
    providers = providers_for_repository(
        repo, user, db, request.headers, settings, cipher, platform_embedder, platform_llm
    )
    try:
        yield providers
    finally:
        providers.close()


Providers = Annotated[RepoProviders, Depends(get_repo_providers)]
