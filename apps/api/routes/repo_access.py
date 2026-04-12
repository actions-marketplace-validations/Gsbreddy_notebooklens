"""Shared repository-access checks for authenticated review routes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from ..oauth import GitHubOAuthClient
from .auth import AuthenticatedUser


_ACCESS_CACHE_TTL = timedelta(minutes=5)
_REPO_ACCESS_CACHE: dict[tuple[int, str], tuple[datetime, bool]] = {}


def ensure_repo_access(
    *,
    current_user: AuthenticatedUser,
    owner: str,
    repo: str,
    oauth_client: GitHubOAuthClient,
) -> None:
    """Require live GitHub repository access for the authenticated user."""
    cache_key = (current_user.github_user_id, f"{owner}/{repo}")
    now = datetime.now(timezone.utc)
    cached = _REPO_ACCESS_CACHE.get(cache_key)
    if cached is not None and cached[0] >= now:
        allowed = cached[1]
    else:
        allowed = oauth_client.can_access_repository(
            current_user.access_token,
            owner=owner,
            repo=repo,
        )
        _REPO_ACCESS_CACHE[cache_key] = (now + _ACCESS_CACHE_TTL, allowed)
    if not allowed:
        raise HTTPException(status_code=403, detail="Repository access denied")


def reset_repo_access_cache() -> None:
    """Clear the in-memory access cache, primarily for tests."""
    _REPO_ACCESS_CACHE.clear()
