"""GitHub App user-authorization (OAuth web flow) client."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from app.core.errors import GithubAuthFailed, GithubUnavailable, ReauthRequired

_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_USER_AGENT = "codebase-intelligence"


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: str | None
    access_expires_at: datetime | None
    refresh_expires_at: datetime | None


@dataclass(frozen=True)
class GitHubProfile:
    id: int
    login: str
    avatar_url: str | None


class GitHubOAuthGateway(Protocol):
    def authorize_url(self, state: str, code_challenge: str) -> str: ...
    def exchange_code(self, code: str, code_verifier: str) -> TokenSet: ...
    def refresh(self, refresh_token: str) -> TokenSet: ...
    def fetch_profile(self, access_token: str) -> GitHubProfile: ...


class GitHubOAuth:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        oauth_base: str = "https://github.com",
        api_base: str = "https://api.github.com",
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._oauth_base = oauth_base.rstrip("/")
        self._http = httpx.Client(
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )
        self._api_base = api_base.rstrip("/")

    def authorize_url(self, state: str, code_challenge: str) -> str:
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": self._redirect_uri,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self._oauth_base}/login/oauth/authorize?{query}"

    def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        # Credentials travel in the POST body, never in a URL.
        body = {"client_id": self._client_id, "client_secret": self._client_secret, **form}
        try:
            resp = self._http.post(f"{self._oauth_base}/login/oauth/access_token", data=body)
        except httpx.HTTPError:
            raise GithubUnavailable("Could not reach GitHub to sign in.") from None
        if resp.status_code >= 500:
            raise GithubUnavailable("GitHub is unavailable.")
        try:
            data = resp.json()
        except ValueError:
            raise GithubUnavailable("GitHub returned an unreadable response.") from None
        if not isinstance(data, dict):
            raise GithubUnavailable("GitHub returned an unexpected response.")
        return data

    @staticmethod
    def _parse_tokens(data: dict[str, Any]) -> TokenSet:
        access = data.get("access_token")
        if not isinstance(access, str) or not access:
            raise GithubAuthFailed("GitHub did not return an access token.")
        now = datetime.now(UTC)

        def expiry(key: str) -> datetime | None:
            seconds = data.get(key)
            return now + timedelta(seconds=seconds) if isinstance(seconds, int) else None

        refresh = data.get("refresh_token")
        return TokenSet(
            access_token=access,
            refresh_token=refresh if isinstance(refresh, str) and refresh else None,
            access_expires_at=expiry("expires_in"),
            refresh_expires_at=expiry("refresh_token_expires_in"),
        )

    def exchange_code(self, code: str, code_verifier: str) -> TokenSet:
        data = self._token_request(
            {"code": code, "redirect_uri": self._redirect_uri, "code_verifier": code_verifier}
        )
        if "error" in data:
            # GitHub reports bad or reused codes as HTTP 200 with an error body.
            raise GithubAuthFailed("GitHub rejected the sign-in code.")
        return self._parse_tokens(data)

    def refresh(self, refresh_token: str) -> TokenSet:
        data = self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})
        if "error" in data:
            raise ReauthRequired("Your GitHub authorization expired. Please sign in again.")
        return self._parse_tokens(data)

    def fetch_profile(self, access_token: str) -> GitHubProfile:
        try:
            resp = self._http.get(
                f"{self._api_base}/user",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        except httpx.HTTPError:
            raise GithubUnavailable("Could not reach GitHub to read your profile.") from None
        if resp.status_code in (401, 403):
            raise GithubAuthFailed("GitHub rejected the access token.")
        if resp.status_code != 200:
            raise GithubUnavailable("GitHub could not return your profile.")
        data = resp.json()
        user_id, login, avatar = data.get("id"), data.get("login"), data.get("avatar_url")
        if not isinstance(user_id, int) or not isinstance(login, str) or not _LOGIN_RE.match(login):
            raise GithubAuthFailed("GitHub returned an unexpected profile.")
        # Only keep an avatar that is a plain https URL; it is rendered in the browser.
        safe_avatar = avatar if isinstance(avatar, str) and avatar.startswith("https://") else None
        return GitHubProfile(
            id=user_id, login=login, avatar_url=safe_avatar[:500] if safe_avatar else None
        )
