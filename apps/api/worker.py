"""Managed snapshot worker entrypoints."""

from __future__ import annotations

from .config import ApiSettings, get_settings
from .database import session_scope
from .managed_github import ManagedGitHubClient
from .orchestration import SnapshotBuildResult, run_snapshot_build_worker_once


def process_snapshot_build_job_once(
    *,
    settings: ApiSettings | None = None,
    github_client: ManagedGitHubClient | None = None,
) -> SnapshotBuildResult:
    """Claim and process one managed snapshot build job."""
    resolved_settings = settings or get_settings()
    resolved_github_client = github_client or ManagedGitHubClient()
    with session_scope(resolved_settings) as db_session:
        return run_snapshot_build_worker_once(
            settings=resolved_settings,
            db_session=db_session,
            github_client=resolved_github_client,
        )


__all__ = ["process_snapshot_build_job_once"]
