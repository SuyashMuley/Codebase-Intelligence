from collections.abc import Callable
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Request

from app.core.config import Settings, get_settings
from app.core.errors import TooManyRequests
from app.core.ratelimit import client_ip, client_key
from app.services.github.client import GitHubClient, GitHubGateway


@lru_cache
def _github_client() -> GitHubClient:
    s = get_settings()
    return GitHubClient(
        token=s.github_token.get_secret_value() if s.github_token else None,
        api_base=s.github_api_base,
        timeout=s.github_timeout_seconds,
    )


def get_github() -> GitHubGateway:
    return _github_client()


AppSettings = Annotated[Settings, Depends(get_settings)]


def get_client_key(request: Request, settings: AppSettings) -> str:
    return client_key(client_ip(request, settings.forwarded_for_hops), settings.client_key_salt)


def rate_limit(bucket: str, per_minute: Callable[[Settings], int]) -> Callable[..., None]:
    def dependency(request: Request, settings: AppSettings) -> None:
        key = client_key(client_ip(request, settings.forwarded_for_hops), settings.client_key_salt)
        retry_after = request.app.state.limiter.hit(f"{bucket}:{key}", per_minute(settings))
        if retry_after is not None:
            seconds = max(1, int(retry_after + 0.999))
            raise TooManyRequests(
                "Too many requests. Slow down and retry shortly.",
                details={"retry_after_seconds": seconds},
                headers={"Retry-After": str(seconds)},
            )

    return dependency


general_limit = rate_limit("general", lambda s: s.rate_limit_per_minute)
index_limit = rate_limit("index", lambda s: s.index_rate_limit_per_minute)
