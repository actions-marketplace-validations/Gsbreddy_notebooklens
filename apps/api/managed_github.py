"""Managed GitHub App API helpers for review workspace orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import quote, urlparse, urlunparse

import requests

from src import __display_version__
from src.github_api import GitHubApiClient

from .config import ApiSettings
from .github_app import DEFAULT_GITHUB_API_URL, GITHUB_API_VERSION, GitHubAppClient
from .models import GitHubHostKind


MANAGED_REVIEW_CHECK_RUN_NAME = "NotebookLens Review Workspace"


class ManagedGitHubClientError(RuntimeError):
    """Raised when the managed GitHub App client cannot complete a request."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ManagedCheckRun:
    """Minimal check-run metadata returned from GitHub."""

    check_run_id: int
    html_url: str | None


@dataclass(frozen=True)
class ManagedComment:
    """Minimal issue/review comment metadata returned from GitHub."""

    comment_id: int
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
        self.github_host_kind, self.github_web_base_url = infer_github_host_metadata(self.api_base_url)
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
                "User-Agent": f"notebooklens-managed/{__display_version__}",
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

    def upsert_issue_comment(
        self,
        *,
        settings: ApiSettings,
        installation_id: int,
        repository: str,
        pull_number: int,
        body: str,
        comment_id: int | None = None,
        access_token: str | None = None,
    ) -> ManagedComment:
        if comment_id is not None:
            try:
                return self.update_issue_comment(
                    settings=settings,
                    installation_id=installation_id,
                    repository=repository,
                    comment_id=comment_id,
                    body=body,
                    access_token=access_token,
                )
            except ManagedGitHubClientError as exc:
                if exc.status_code != 404:
                    raise
        return self.create_issue_comment(
            settings=settings,
            installation_id=installation_id,
            repository=repository,
            pull_number=pull_number,
            body=body,
            access_token=access_token,
        )

    def create_issue_comment(
        self,
        *,
        settings: ApiSettings,
        installation_id: int,
        repository: str,
        pull_number: int,
        body: str,
        access_token: str | None = None,
    ) -> ManagedComment:
        payload = self._request_json(
            method="POST",
            url=(
                f"{self.api_base_url}/repos/{self._encode_repository(repository)}"
                f"/issues/{pull_number}/comments"
            ),
            token=self._resolve_token(
                settings=settings,
                installation_id=installation_id,
                access_token=access_token,
            ),
            body={"body": body},
            expected_statuses={201},
        )
        return _parse_managed_comment(payload)

    def update_issue_comment(
        self,
        *,
        settings: ApiSettings,
        installation_id: int,
        repository: str,
        comment_id: int,
        body: str,
        access_token: str | None = None,
    ) -> ManagedComment:
        payload = self._request_json(
            method="PATCH",
            url=(
                f"{self.api_base_url}/repos/{self._encode_repository(repository)}"
                f"/issues/comments/{comment_id}"
            ),
            token=self._resolve_token(
                settings=settings,
                installation_id=installation_id,
                access_token=access_token,
            ),
            body={"body": body},
            expected_statuses={200},
        )
        return _parse_managed_comment(payload)

    def upsert_review_comment(
        self,
        *,
        settings: ApiSettings,
        installation_id: int,
        repository: str,
        pull_number: int,
        commit_id: str,
        path: str,
        line: int,
        side: str,
        body: str,
        comment_id: int | None = None,
        access_token: str | None = None,
    ) -> ManagedComment:
        if comment_id is not None:
            try:
                return self.update_review_comment(
                    settings=settings,
                    installation_id=installation_id,
                    repository=repository,
                    comment_id=comment_id,
                    body=body,
                    access_token=access_token,
                )
            except ManagedGitHubClientError as exc:
                if exc.status_code != 404:
                    raise
        payload = self._request_json(
            method="POST",
            url=(
                f"{self.api_base_url}/repos/{self._encode_repository(repository)}"
                f"/pulls/{pull_number}/comments"
            ),
            token=self._resolve_token(
                settings=settings,
                installation_id=installation_id,
                access_token=access_token,
            ),
            body={
                "body": body,
                "commit_id": commit_id,
                "path": path,
                "line": line,
                "side": side,
            },
            expected_statuses={201},
        )
        return _parse_managed_comment(payload)

    def create_review_comment_reply(
        self,
        *,
        settings: ApiSettings,
        installation_id: int,
        repository: str,
        pull_number: int,
        comment_id: int,
        body: str,
        access_token: str | None = None,
    ) -> ManagedComment:
        payload = self._request_json(
            method="POST",
            url=(
                f"{self.api_base_url}/repos/{self._encode_repository(repository)}"
                f"/pulls/{pull_number}/comments/{comment_id}/replies"
            ),
            token=self._resolve_token(
                settings=settings,
                installation_id=installation_id,
                access_token=access_token,
            ),
            body={"body": body},
            expected_statuses={201},
        )
        return _parse_managed_comment(payload)

    def update_review_comment(
        self,
        *,
        settings: ApiSettings,
        installation_id: int,
        repository: str,
        comment_id: int,
        body: str,
        access_token: str | None = None,
    ) -> ManagedComment:
        payload = self._request_json(
            method="PATCH",
            url=(
                f"{self.api_base_url}/repos/{self._encode_repository(repository)}"
                f"/pulls/comments/{comment_id}"
            ),
            token=self._resolve_token(
                settings=settings,
                installation_id=installation_id,
                access_token=access_token,
            ),
            body={"body": body},
            expected_statuses={200},
        )
        return _parse_managed_comment(payload)

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

    def _resolve_token(
        self,
        *,
        settings: ApiSettings,
        installation_id: int,
        access_token: str | None,
    ) -> str:
        if isinstance(access_token, str) and access_token.strip():
            return access_token.strip()
        return self.app_client.create_installation_access_token(
            settings=settings,
            installation_id=installation_id,
        ).token

    def _request_json(
        self,
        *,
        method: str,
        url: str,
        token: str,
        body: Mapping[str, Any] | None,
        expected_statuses: set[int],
    ) -> Mapping[str, Any]:
        response = self.session.request(
            method=method,
            url=url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": f"notebooklens-managed/{__display_version__}",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            },
            json=dict(body) if body is not None else None,
            timeout=30,
        )
        if int(response.status_code) not in expected_statuses:
            detail = response.text.strip()
            raise ManagedGitHubClientError(
                (
                    f"GitHub {method} failed with status {response.status_code}"
                    if not detail
                    else f"GitHub {method} failed with status {response.status_code}: {detail[:300]}"
                ),
                status_code=int(response.status_code),
            )
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise ManagedGitHubClientError("GitHub response body was not a JSON object")
        return payload

    @staticmethod
    def _encode_repository(repository: str) -> str:
        return quote(repository, safe="/")


def infer_github_host_metadata(api_base_url: str) -> tuple[GitHubHostKind, str]:
    normalized_api_url = api_base_url.rstrip("/")
    if normalized_api_url == DEFAULT_GITHUB_API_URL:
        return GitHubHostKind.GITHUB_COM, "https://github.com"

    parsed = urlparse(normalized_api_url)
    trimmed_path = parsed.path.rstrip("/")
    if trimmed_path.endswith("/api/v3"):
        trimmed_path = trimmed_path[: -len("/api/v3")]
    netloc = parsed.netloc
    if not trimmed_path and netloc.startswith("api."):
        netloc = netloc[4:]
    web_url = urlunparse((parsed.scheme, netloc, trimmed_path, "", "", "")).rstrip("/")
    return GitHubHostKind.GHES, web_url or normalized_api_url


def _parse_managed_comment(payload: Mapping[str, Any]) -> ManagedComment:
    raw_id = payload.get("id")
    if not isinstance(raw_id, int):
        raise ManagedGitHubClientError("GitHub comment response did not include an integer id")
    html_url = payload.get("html_url")
    return ManagedComment(
        comment_id=raw_id,
        html_url=html_url if isinstance(html_url, str) else None,
    )


__all__ = [
    "MANAGED_REVIEW_CHECK_RUN_NAME",
    "ManagedCheckRun",
    "ManagedComment",
    "ManagedGitHubClient",
    "ManagedGitHubClientError",
    "infer_github_host_metadata",
]
