"""Managed GitHub App API helpers for review workspace orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import quote

import requests

from src.github_api import GitHubApiClient

from .config import ApiSettings
from .github_app import DEFAULT_GITHUB_API_URL, GITHUB_API_VERSION, GitHubAppClient


MANAGED_REVIEW_CHECK_RUN_NAME = "NotebookLens Review Workspace"


class ManagedGitHubClientError(RuntimeError):
    """Raised when the managed GitHub App client cannot complete a request."""


@dataclass(frozen=True)
class ManagedCheckRun:
    """Minimal check-run metadata returned from GitHub."""

    check_run_id: int
    html_url: str | None


class ManagedGitHubClient:
    """GitHub App-backed content and check-run client for the managed review surface."""

    def __init__(
        self,
        *,
        app_client: GitHubAppClient | None = None,
        api_base_url: str = DEFAULT_GITHUB_API_URL,
        session: requests.Session | None = None,
    ) -> None:
        self.app_client = app_client or GitHubAppClient(api_base_url=api_base_url, session=session)
        self.api_base_url = api_base_url.rstrip("/")
        self.session = session or requests.Session()

    def list_pull_request_files(
        self,
        *,
        settings: ApiSettings,
        installation_id: int,
        repository: str,
        pull_number: int,
    ) -> Sequence[Any]:
        client = self._content_client(
            settings=settings,
            installation_id=installation_id,
        )
        return client.list_pull_request_files(
            repository=repository,
            pull_number=pull_number,
        )

    def get_file_content(
        self,
        *,
        settings: ApiSettings,
        installation_id: int,
        repository: str,
        path: str,
        ref: str,
    ) -> str | None:
        client = self._content_client(
            settings=settings,
            installation_id=installation_id,
        )
        return client.get_file_content(
            repository=repository,
            path=path,
            ref=ref,
        )

    def create_or_update_check_run(
        self,
        *,
        settings: ApiSettings,
        installation_id: int,
        repository: str,
        head_sha: str,
        status: str,
        details_url: str,
        external_id: str,
        summary: str,
        title: str = MANAGED_REVIEW_CHECK_RUN_NAME,
        text: str | None = None,
        conclusion: str | None = None,
        check_run_id: int | None = None,
    ) -> ManagedCheckRun:
        token = self.app_client.create_installation_access_token(
            settings=settings,
            installation_id=installation_id,
        )
        payload: dict[str, Any] = {
            "name": MANAGED_REVIEW_CHECK_RUN_NAME,
            "status": status,
            "details_url": details_url,
            "external_id": external_id,
            "output": {
                "title": title,
                "summary": summary,
            },
        }
        if text is not None:
            payload["output"]["text"] = text
        if conclusion is not None:
            payload["conclusion"] = conclusion

        if check_run_id is None:
            method = "POST"
            url = f"{self.api_base_url}/repos/{self._encode_repository(repository)}/check-runs"
            payload["head_sha"] = head_sha
            expected_statuses = {201}
        else:
            method = "PATCH"
            url = (
                f"{self.api_base_url}/repos/{self._encode_repository(repository)}"
                f"/check-runs/{check_run_id}"
            )
            expected_statuses = {200}

        response = self.session.request(
            method=method,
            url=url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token.token}",
                "User-Agent": "notebooklens-managed/0.3.0-beta",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            },
            json=payload,
            timeout=30,
        )
        if int(response.status_code) not in expected_statuses:
            raise ManagedGitHubClientError(
                f"GitHub check run {method} failed with status {response.status_code}"
            )

        body = response.json()
        raw_id = body.get("id")
        if not isinstance(raw_id, int):
            raise ManagedGitHubClientError("GitHub check run response did not include an integer id")
        html_url = body.get("html_url")
        return ManagedCheckRun(
            check_run_id=raw_id,
            html_url=html_url if isinstance(html_url, str) else None,
        )

    def _content_client(
        self,
        *,
        settings: ApiSettings,
        installation_id: int,
    ) -> GitHubApiClient:
        token = self.app_client.create_installation_access_token(
            settings=settings,
            installation_id=installation_id,
        )
        return GitHubApiClient(
            token=token.token,
            api_url=self.api_base_url,
            api_version=GITHUB_API_VERSION,
            session=self.session,
        )

    @staticmethod
    def _encode_repository(repository: str) -> str:
        return quote(repository, safe="/")


__all__ = [
    "MANAGED_REVIEW_CHECK_RUN_NAME",
    "ManagedCheckRun",
    "ManagedGitHubClient",
    "ManagedGitHubClientError",
]
