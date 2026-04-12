"""GitHub App authentication primitives for the managed API skeleton."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import jwt
import requests

from .config import ApiSettings


GITHUB_API_VERSION = "2022-11-28"
DEFAULT_GITHUB_API_URL = "https://api.github.com"


class GitHubAppAuthError(RuntimeError):
    """Raised when GitHub App authentication fails."""


def build_github_app_jwt(
    *,
    app_id: str,
    private_key_pem: str,
    issued_at: datetime | None = None,
    expires_in_seconds: int = 540,
) -> str:
    """Create a GitHub App RS256 JWT."""
    if expires_in_seconds <= 0:
        raise ValueError("expires_in_seconds must be positive")
    now = issued_at or datetime.now(timezone.utc)
    payload = {
        "iat": int((now - timedelta(seconds=60)).timestamp()),
        "exp": int((now + timedelta(seconds=expires_in_seconds)).timestamp()),
        "iss": app_id,
    }
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


def build_github_app_headers(jwt_token: str) -> dict[str, str]:
    """Build standard GitHub App request headers."""
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {jwt_token}",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }


@dataclass(frozen=True)
class InstallationAccessToken:
    token: str
    expires_at: datetime
    permissions: Mapping[str, str]


def _parse_github_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GitHubAppClient:
    """Minimal GitHub App client primitives for installation tokens."""

    def __init__(
        self,
        *,
        api_base_url: str = DEFAULT_GITHUB_API_URL,
        session: requests.Session | None = None,
    ) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.session = session or requests.Session()

    def create_installation_access_token(
        self,
        *,
        settings: ApiSettings,
        installation_id: int,
    ) -> InstallationAccessToken:
        jwt_token = build_github_app_jwt(
            app_id=settings.github_app_id,
            private_key_pem=settings.github_app_private_key,
        )
        response = self.session.post(
            f"{self.api_base_url}/app/installations/{installation_id}/access_tokens",
            headers=build_github_app_headers(jwt_token),
            timeout=30,
        )
        if response.status_code >= 400:
            raise GitHubAppAuthError(
                f"GitHub installation token request failed with status {response.status_code}"
            )
        payload: dict[str, Any] = response.json()
        token = payload.get("token")
        expires_at = payload.get("expires_at")
        if not token or not expires_at:
            raise GitHubAppAuthError("GitHub installation token response was incomplete")
        return InstallationAccessToken(
            token=token,
            expires_at=_parse_github_timestamp(expires_at),
            permissions=payload.get("permissions", {}),
        )
