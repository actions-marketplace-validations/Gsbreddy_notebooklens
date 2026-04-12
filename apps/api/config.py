"""Environment-driven settings for the managed API skeleton."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Mapping
import os
from urllib.parse import urlparse


class ApiConfigurationError(ValueError):
    """Raised when managed API configuration is missing or invalid."""


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ApiConfigurationError(f"Missing required environment variable: {name}")
    return value


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ApiConfigurationError(f"Invalid boolean value: {value}")


def _normalize_origin_url(environ: Mapping[str, str], name: str) -> str:
    value = _required(environ, name).rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ApiConfigurationError(f"{name} must be an absolute http(s) origin")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise ApiConfigurationError(f"{name} must be an origin without a path, query, or fragment")
    return f"{parsed.scheme}://{parsed.netloc}"


@dataclass(frozen=True)
class ApiSettings:
    """Configuration required by the managed NotebookLens API."""

    database_url: str
    app_base_url: str
    session_secret: str
    encryption_key: str
    github_app_id: str
    github_app_private_key: str
    github_webhook_secret: str
    github_oauth_client_id: str
    github_oauth_client_secret: str
    email_provider: str
    email_api_key: str
    email_from: str
    snapshot_retention_days: int = 90
    managed_review_beta_enabled: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ApiSettings":
        source = dict(os.environ if environ is None else environ)
        retention = int(source.get("SNAPSHOT_RETENTION_DAYS", "90"))
        if retention <= 0:
            raise ApiConfigurationError("SNAPSHOT_RETENTION_DAYS must be positive")
        private_key = _required(source, "GITHUB_APP_PRIVATE_KEY").replace("\\n", "\n")
        return cls(
            database_url=_required(source, "DATABASE_URL"),
            app_base_url=_normalize_origin_url(source, "APP_BASE_URL"),
            session_secret=_required(source, "SESSION_SECRET"),
            encryption_key=_required(source, "ENCRYPTION_KEY"),
            github_app_id=_required(source, "GITHUB_APP_ID"),
            github_app_private_key=private_key,
            github_webhook_secret=_required(source, "GITHUB_WEBHOOK_SECRET"),
            github_oauth_client_id=_required(source, "GITHUB_OAUTH_CLIENT_ID"),
            github_oauth_client_secret=_required(source, "GITHUB_OAUTH_CLIENT_SECRET"),
            email_provider=_required(source, "EMAIL_PROVIDER"),
            email_api_key=_required(source, "EMAIL_API_KEY"),
            email_from=_required(source, "EMAIL_FROM"),
            snapshot_retention_days=retention,
            managed_review_beta_enabled=_parse_bool(
                source.get("MANAGED_REVIEW_BETA_ENABLED", "false")
            ),
        )

    @property
    def github_oauth_callback_url(self) -> str:
        return f"{self.app_base_url}/api/auth/github/callback"


@lru_cache(maxsize=1)
def get_settings() -> ApiSettings:
    """Load and cache managed API settings from the environment."""
    return ApiSettings.from_env()


def reset_settings_cache() -> None:
    """Clear the cached settings instance, primarily for tests."""
    get_settings.cache_clear()
