"""GitHub API adapter and PR comment idempotency for NotebookLens v0.1.0."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
from typing import Any, Dict, Iterable, List, Literal, Mapping, Optional, Sequence, Tuple
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from .diff_engine import CellChange, CellLocator, NotebookDiff, NotebookFileDiff, ReviewResult


DEFAULT_GITHUB_API_URL = "https://api.github.com"
DEFAULT_GITHUB_API_VERSION = "2022-11-28"
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_PER_PAGE = 100
NOTEBOOKLENS_COMMENT_MARKER = "<!-- notebooklens-comment -->"
DEFAULT_ACTION_BOT_LOGINS = ("github-actions[bot]",)

CommentSyncAction = Literal["created", "updated", "deleted", "unchanged", "noop"]


class GitHubApiError(RuntimeError):
    """Raised when a GitHub API request fails."""

    def __init__(self, message: str, *, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class PullRequestComment:
    comment_id: int
    body: str
    author_login: str
    author_type: str
    updated_at: str


@dataclass(frozen=True)
class CommentSyncResult:
    action: CommentSyncAction
    comment_id: Optional[int]
    deleted_comment_ids: List[int]
    details: str


class GitHubApiClient:
    """Concrete GitHub API adapter for action runtime and comment sync."""

    def __init__(
        self,
        *,
        token: Optional[str],
        api_url: str = DEFAULT_GITHUB_API_URL,
        api_version: str = DEFAULT_GITHUB_API_VERSION,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        action_bot_logins: Sequence[str] = DEFAULT_ACTION_BOT_LOGINS,
        session: Optional[Any] = None,
    ) -> None:
        self.token = (token or "").strip()
        self.api_url = api_url.rstrip("/")
        self.api_version = api_version
        self.timeout_seconds = timeout_seconds
        self.action_bot_logins = _normalize_bot_logins(action_bot_logins)
        self.session = session

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "GitHubApiClient":
        import os

        env_map = dict(os.environ if env is None else env)
        token = env_map.get("GITHUB_TOKEN") or env_map.get("INPUT_GITHUB_TOKEN")
        return cls(token=token)

    def list_pull_request_files(self, *, repository: str, pull_number: int) -> Sequence[Any]:
        """Return changed PR files in deterministic GitHub API order."""
        path = f"/repos/{_encode_repository(repository)}/pulls/{pull_number}/files"
        return self._paginate(path=path)

    def get_file_content(self, *, repository: str, path: str, ref: str) -> Optional[str]:
        """Return UTF-8 text file content at ref via GitHub APIs, or None when unavailable."""
        encoded_path = _encode_file_path(path)
        endpoint = f"/repos/{_encode_repository(repository)}/contents/{encoded_path}"
        payload = self._request_json(
            method="GET",
            path=endpoint,
            query={"ref": ref},
            allow_not_found=True,
        )
        if payload is None:
            return None
        if not isinstance(payload, Mapping):
            return None

        content = payload.get("content")
        encoding = payload.get("encoding")
        if isinstance(content, str) and str(encoding).lower() == "base64":
            normalized = content.replace("\n", "")
            try:
                decoded_bytes = base64.b64decode(normalized, validate=False)
            except Exception as exc:  # pragma: no cover - defensive decode path
                raise GitHubApiError(
                    f"Failed to decode base64 content for {path}@{ref}: {exc}"
                ) from exc
            return decoded_bytes.decode("utf-8", errors="replace")

        download_url = payload.get("download_url")
        if isinstance(download_url, str) and download_url.strip():
            return self._request_text_url(download_url.strip(), allow_not_found=True)

        return None

    def list_pull_request_comments(
        self,
        *,
        repository: str,
        pull_number: int,
    ) -> List[PullRequestComment]:
        path = f"/repos/{_encode_repository(repository)}/issues/{pull_number}/comments"
        raw_comments = self._paginate(path=path)
        comments: List[PullRequestComment] = []
        for raw in raw_comments:
            parsed = _parse_pull_request_comment(raw)
            if parsed is not None:
                comments.append(parsed)
        return comments

    def create_pull_request_comment(
        self,
        *,
        repository: str,
        pull_number: int,
        body: str,
    ) -> PullRequestComment:
        path = f"/repos/{_encode_repository(repository)}/issues/{pull_number}/comments"
        payload = self._request_json(method="POST", path=path, body={"body": body})
        parsed = _parse_pull_request_comment(payload)
        if parsed is None:
            raise GitHubApiError("GitHub create comment response was malformed.")
        return parsed

    def update_pull_request_comment(
        self,
        *,
        repository: str,
        comment_id: int,
        body: str,
    ) -> PullRequestComment:
        path = f"/repos/{_encode_repository(repository)}/issues/comments/{comment_id}"
        payload = self._request_json(method="PATCH", path=path, body={"body": body})
        parsed = _parse_pull_request_comment(payload)
        if parsed is None:
            raise GitHubApiError("GitHub update comment response was malformed.")
        return parsed

    def delete_pull_request_comment(self, *, repository: str, comment_id: int) -> None:
        path = f"/repos/{_encode_repository(repository)}/issues/comments/{comment_id}"
        self._request_json(method="DELETE", path=path, expected_statuses=(204,), parse_json=False)

    def list_owned_marker_comments(
        self,
        *,
        repository: str,
        pull_number: int,
        action_bot_logins: Optional[Sequence[str]] = None,
    ) -> List[PullRequestComment]:
        owner_logins = (
            _normalize_bot_logins(action_bot_logins)
            if action_bot_logins is not None
            else self.action_bot_logins
        )
        comments = self.list_pull_request_comments(repository=repository, pull_number=pull_number)
        eligible = [
            comment
            for comment in comments
            if _is_owned_marker_comment(comment=comment, owner_logins=owner_logins)
        ]
        eligible.sort(key=lambda item: (item.updated_at, item.comment_id))
        return eligible

    def upsert_marker_comment(
        self,
        *,
        repository: str,
        pull_number: int,
        body: str,
        action_bot_logins: Optional[Sequence[str]] = None,
    ) -> CommentSyncResult:
        """Create or update the single owned marker comment safely."""
        rendered_body = ensure_marker(body)
        existing = self.list_owned_marker_comments(
            repository=repository,
            pull_number=pull_number,
            action_bot_logins=action_bot_logins,
        )
        deleted_ids: List[int] = []

        if not existing:
            created = self.create_pull_request_comment(
                repository=repository,
                pull_number=pull_number,
                body=rendered_body,
            )
            return CommentSyncResult(
                action="created",
                comment_id=created.comment_id,
                deleted_comment_ids=deleted_ids,
                details="Created marker comment.",
            )

        primary = existing[-1]
        duplicates = existing[:-1]
        for duplicate in duplicates:
            if self._delete_comment_safe(repository=repository, comment_id=duplicate.comment_id):
                deleted_ids.append(duplicate.comment_id)

        if _normalize_comment_body(primary.body) == _normalize_comment_body(rendered_body):
            return CommentSyncResult(
                action="unchanged",
                comment_id=primary.comment_id,
                deleted_comment_ids=deleted_ids,
                details="Marker comment already up to date.",
            )

        try:
            updated = self.update_pull_request_comment(
                repository=repository,
                comment_id=primary.comment_id,
                body=rendered_body,
            )
            return CommentSyncResult(
                action="updated",
                comment_id=updated.comment_id,
                deleted_comment_ids=deleted_ids,
                details="Updated existing marker comment.",
            )
        except GitHubApiError as exc:
            if exc.status_code != 404:
                raise

        created = self.create_pull_request_comment(
            repository=repository,
            pull_number=pull_number,
            body=rendered_body,
        )
        return CommentSyncResult(
            action="created",
            comment_id=created.comment_id,
            deleted_comment_ids=deleted_ids,
            details="Marker comment disappeared during update; created a new one.",
        )

    def delete_marker_comments(
        self,
        *,
        repository: str,
        pull_number: int,
        action_bot_logins: Optional[Sequence[str]] = None,
    ) -> CommentSyncResult:
        """Delete all eligible owned marker comments for this pull request."""
        existing = self.list_owned_marker_comments(
            repository=repository,
            pull_number=pull_number,
            action_bot_logins=action_bot_logins,
        )
        if not existing:
            return CommentSyncResult(
                action="noop",
                comment_id=None,
                deleted_comment_ids=[],
                details="No eligible marker comment found to delete.",
            )

        deleted_ids: List[int] = []
        for comment in existing:
            if self._delete_comment_safe(repository=repository, comment_id=comment.comment_id):
                deleted_ids.append(comment.comment_id)

        return CommentSyncResult(
            action="deleted",
            comment_id=None,
            deleted_comment_ids=deleted_ids,
            details=f"Deleted {len(deleted_ids)} owned marker comment(s).",
        )

    def sync_marker_comment(
        self,
        *,
        repository: str,
        pull_number: int,
        has_notebook_changes: bool,
        body: Optional[str],
        action_bot_logins: Optional[Sequence[str]] = None,
    ) -> CommentSyncResult:
        """Apply marker idempotency contract for create/update/delete behavior."""
        if not has_notebook_changes:
            return self.delete_marker_comments(
                repository=repository,
                pull_number=pull_number,
                action_bot_logins=action_bot_logins,
            )
        if body is None or not body.strip():
            raise ValueError("sync_marker_comment requires non-empty body when notebooks changed.")
        return self.upsert_marker_comment(
            repository=repository,
            pull_number=pull_number,
            body=body,
            action_bot_logins=action_bot_logins,
        )

    def _delete_comment_safe(self, *, repository: str, comment_id: int) -> bool:
        try:
            self.delete_pull_request_comment(repository=repository, comment_id=comment_id)
            return True
        except GitHubApiError as exc:
            if exc.status_code == 404:
                return False
            raise

    def _paginate(
        self,
        *,
        path: str,
        query: Optional[Mapping[str, Any]] = None,
    ) -> List[Any]:
        page = 1
        items: List[Any] = []
        while True:
            page_query: Dict[str, Any] = dict(query or {})
            page_query["per_page"] = DEFAULT_PER_PAGE
            page_query["page"] = page

            payload = self._request_json(method="GET", path=path, query=page_query)
            if not isinstance(payload, list):
                raise GitHubApiError(f"Expected list response from GitHub API for {path}.")
            items.extend(payload)
            if len(payload) < DEFAULT_PER_PAGE:
                break
            page += 1
        return items

    def _request_json(
        self,
        *,
        method: str,
        path: str,
        query: Optional[Mapping[str, Any]] = None,
        body: Optional[Mapping[str, Any]] = None,
        expected_statuses: Tuple[int, ...] = (200, 201),
        parse_json: bool = True,
        allow_not_found: bool = False,
    ) -> Any:
        url = self._build_url(path=path, query=query)
        headers = self._headers()
        payload = json.dumps(body).encode("utf-8") if body is not None else None

        status_code: int
        text_body: str
        if self.session is None:
            status_code, text_body = self._request_with_urllib(
                method=method,
                url=url,
                headers=headers,
                payload=payload,
            )
        else:
            status_code, text_body = self._request_with_session(
                method=method,
                url=url,
                headers=headers,
                body=body,
            )

        if allow_not_found and status_code == 404:
            return None

        accepted_statuses = set(expected_statuses)
        if method == "GET":
            accepted_statuses.add(304)

        if status_code not in accepted_statuses:
            raise GitHubApiError(
                _github_error_message(method=method, url=url, status_code=status_code, body=text_body),
                status_code=status_code,
            )

        if not parse_json:
            return None
        if status_code == 204 or not text_body.strip():
            return {}

        try:
            return json.loads(text_body)
        except json.JSONDecodeError as exc:
            raise GitHubApiError(
                f"GitHub API returned non-JSON response for {method} {url}."
            ) from exc

    def _request_text_url(self, url: str, *, allow_not_found: bool) -> Optional[str]:
        headers = self._headers(accept="application/vnd.github.raw")
        status_code: int
        text_body: str
        if self.session is None:
            status_code, text_body = self._request_with_urllib(
                method="GET",
                url=url,
                headers=headers,
                payload=None,
            )
        else:
            status_code, text_body = self._request_with_session(
                method="GET",
                url=url,
                headers=headers,
                body=None,
            )

        if allow_not_found and status_code == 404:
            return None
        if status_code not in {200, 304}:
            raise GitHubApiError(
                _github_error_message(method="GET", url=url, status_code=status_code, body=text_body),
                status_code=status_code,
            )
        return text_body

    def _request_with_urllib(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        payload: Optional[bytes],
    ) -> Tuple[int, str]:
        request = urllib_request.Request(url=url, method=method, data=payload)
        for key, value in headers.items():
            request.add_header(key, value)

        try:
            with urllib_request.urlopen(request, timeout=self.timeout_seconds) as response:
                status = int(getattr(response, "status", response.getcode()))
                body = response.read().decode("utf-8", errors="replace")
                return status, body
        except urllib_error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return int(exc.code), body
        except urllib_error.URLError as exc:
            raise GitHubApiError(f"GitHub API request failed: {exc}") from exc

    def _request_with_session(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Optional[Mapping[str, Any]],
    ) -> Tuple[int, str]:
        response = self.session.request(
            method=method,
            url=url,
            headers=dict(headers),
            json=body,
            timeout=self.timeout_seconds,
        )
        status = int(getattr(response, "status_code", 0))
        text = str(getattr(response, "text", ""))
        return status, text

    def _build_url(self, *, path: str, query: Optional[Mapping[str, Any]]) -> str:
        query_string = urllib_parse.urlencode(dict(query or {}), doseq=True)
        if query_string:
            return f"{self.api_url}{path}?{query_string}"
        return f"{self.api_url}{path}"

    def _headers(self, *, accept: str = "application/vnd.github+json") -> Dict[str, str]:
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": self.api_version,
            "User-Agent": "notebooklens-action/0.1.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers


def render_pull_request_comment(
    *,
    notebook_diff: NotebookDiff,
    review_result: ReviewResult,
    claude_succeeded: bool,
    notices: Optional[Sequence[str]] = None,
) -> str:
    """Render the single marker PR comment from structured diff/review results."""
    merged_notices = _dedupe_notices([*notebook_diff.notices, *(notices or [])])
    lines: List[str] = [
        NOTEBOOKLENS_COMMENT_MARKER,
        "## NotebookLens",
        "",
        (
            "Reviewed "
            f"**{notebook_diff.total_notebooks_changed}** notebook(s) with "
            f"**{notebook_diff.total_cells_changed}** changed cell(s)."
        ),
        "",
        "### Notebook Changes",
    ]

    if not notebook_diff.notebooks:
        lines.append("- No notebook diffs were produced.")

    for notebook in notebook_diff.notebooks:
        lines.extend(_render_notebook_section(notebook))

    if review_result.flagged_issues:
        lines.append("")
        lines.append("### Flagged Findings")
        for issue in review_result.flagged_issues:
            location = _format_cell_locator(issue.locator)
            confidence = issue.confidence if issue.confidence is not None else "n/a"
            lines.append(
                (
                    f"- **{issue.severity.upper()}** `{issue.notebook_path}` · {location} · "
                    f"`{issue.category}` · {_sanitize_inline(issue.message)} "
                    f"(`{issue.code}`, confidence: {confidence})"
                )
            )

    if claude_succeeded and review_result.summary:
        lines.append("")
        lines.append("<details>")
        lines.append("<summary>AI summary (Claude)</summary>")
        lines.append("")
        lines.append(_sanitize_multiline(review_result.summary))
        lines.append("")
        lines.append("</details>")

    if merged_notices:
        lines.append("")
        lines.append("### Notices")
        for notice in merged_notices:
            lines.append(f"- {_sanitize_inline(notice)}")

    return "\n".join(lines).rstrip() + "\n"


def sync_review_comment(
    *,
    github_api: GitHubApiClient,
    repository: str,
    pull_number: int,
    has_notebook_changes: bool,
    notebook_diff: Optional[NotebookDiff],
    review_result: Optional[ReviewResult],
    claude_succeeded: bool,
    notices: Optional[Sequence[str]] = None,
    action_bot_logins: Optional[Sequence[str]] = None,
) -> CommentSyncResult:
    """Apply the PR comment contract for a completed action run."""
    if not has_notebook_changes:
        return github_api.delete_marker_comments(
            repository=repository,
            pull_number=pull_number,
            action_bot_logins=action_bot_logins,
        )

    if notebook_diff is None or review_result is None:
        raise ValueError(
            "sync_review_comment requires notebook_diff and review_result when notebooks changed."
        )

    body = render_pull_request_comment(
        notebook_diff=notebook_diff,
        review_result=review_result,
        claude_succeeded=claude_succeeded,
        notices=notices,
    )
    return github_api.upsert_marker_comment(
        repository=repository,
        pull_number=pull_number,
        body=body,
        action_bot_logins=action_bot_logins,
    )


def claude_succeeded_from_metadata(metadata: Optional[Any]) -> bool:
    """Detect whether Claude succeeded (not merely attempted or fallback)."""
    if metadata is None:
        return False
    claude_called = bool(_metadata_field(metadata, "claude_called", False))
    used_fallback = bool(_metadata_field(metadata, "used_fallback", False))
    effective_provider = str(_metadata_field(metadata, "effective_provider", "")).strip().lower()
    return claude_called and not used_fallback and effective_provider == "claude"


def ensure_marker(body: str) -> str:
    normalized = body.strip()
    if NOTEBOOKLENS_COMMENT_MARKER in normalized:
        return normalized
    return f"{NOTEBOOKLENS_COMMENT_MARKER}\n{normalized}"


def _render_notebook_section(notebook: NotebookFileDiff) -> List[str]:
    lines: List[str] = [
        "",
        f"#### `{notebook.path}` (`{notebook.change_type}`)",
    ]
    if not notebook.cell_changes:
        lines.append("- No material cell changes detected.")
    else:
        summary = _summarize_notebook_changes(notebook.cell_changes)
        lines.append(
            (
                f"- Changed cells: **{len(notebook.cell_changes)}** "
                f"(added {summary['added']}, modified {summary['modified']}, "
                f"deleted {summary['deleted']}, moved {summary['moved']}, "
                f"output-only {summary['output_changed']})"
            )
        )
        lines.append(f"- Cells with output updates: **{summary['output_cells']}**")
        for change in notebook.cell_changes:
            lines.append(_render_cell_change_line(change))
    if notebook.notices:
        joined = "; ".join(_sanitize_inline(item) for item in notebook.notices)
        lines.append(f"- Notebook notices: {joined}")
    return lines


def _summarize_notebook_changes(cell_changes: Sequence[CellChange]) -> Dict[str, int]:
    counts = {
        "added": 0,
        "modified": 0,
        "deleted": 0,
        "moved": 0,
        "output_changed": 0,
        "output_cells": 0,
    }
    for change in cell_changes:
        counts[change.change_type] += 1
        if change.output_changes:
            counts["output_cells"] += 1
    return counts


def _render_cell_change_line(change: CellChange) -> str:
    location = _format_cell_locator(change.locator)
    output_summary = _format_output_updates(change)
    base_line = (
        f"- {location} · `{change.cell_type}` · `{change.change_type}` · "
        f"{_sanitize_inline(change.summary)}"
    )
    if output_summary:
        return f"{base_line} Output updates: {output_summary}"
    return base_line


def _format_output_updates(change: CellChange) -> str:
    if not change.output_changes:
        return ""
    fragments: List[str] = []
    for item in change.output_changes:
        suffix = " (truncated for AI)" if item.truncated else ""
        fragments.append(f"{_sanitize_inline(item.summary)}{suffix}")
    return "; ".join(fragments)


def _format_cell_locator(locator: CellLocator) -> str:
    if locator.display_index is not None:
        return f"Cell {locator.display_index}"
    if locator.cell_id:
        return f"Cell id `{locator.cell_id}`"
    if locator.head_index is not None:
        return f"Cell {locator.head_index + 1}"
    if locator.base_index is not None:
        return f"Cell {locator.base_index + 1}"
    return "Notebook-level"


def _dedupe_notices(items: Iterable[str]) -> List[str]:
    seen = set()
    deduped: List[str] = []
    for item in items:
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(key)
    return deduped


def _sanitize_inline(text: str) -> str:
    compact = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    return compact.replace(NOTEBOOKLENS_COMMENT_MARKER, "[marker removed]")


def _sanitize_multiline(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return normalized.replace(NOTEBOOKLENS_COMMENT_MARKER, "[marker removed]")


def _metadata_field(metadata: Any, key: str, default: Any) -> Any:
    if isinstance(metadata, Mapping):
        return metadata.get(key, default)
    return getattr(metadata, key, default)


def _normalize_comment_body(body: str) -> str:
    return body.strip().replace("\r\n", "\n")


def _normalize_bot_logins(logins: Sequence[str]) -> Tuple[str, ...]:
    normalized = tuple(login.strip().lower() for login in logins if login and login.strip())
    if normalized:
        return normalized
    return tuple(login.lower() for login in DEFAULT_ACTION_BOT_LOGINS)


def _is_owned_marker_comment(
    *,
    comment: PullRequestComment,
    owner_logins: Sequence[str],
) -> bool:
    if NOTEBOOKLENS_COMMENT_MARKER not in comment.body:
        return False
    if comment.author_type.strip().lower() != "bot":
        return False
    return comment.author_login.strip().lower() in owner_logins


def _parse_pull_request_comment(raw: Any) -> Optional[PullRequestComment]:
    if not isinstance(raw, Mapping):
        return None
    comment_id = raw.get("id")
    body = raw.get("body")
    user = raw.get("user")
    updated_at = raw.get("updated_at")

    if not isinstance(comment_id, int):
        return None
    if not isinstance(body, str):
        return None
    if not isinstance(user, Mapping):
        return None
    author_login = user.get("login")
    author_type = user.get("type")
    if not isinstance(author_login, str) or not isinstance(author_type, str):
        return None

    return PullRequestComment(
        comment_id=comment_id,
        body=body,
        author_login=author_login,
        author_type=author_type,
        updated_at=updated_at if isinstance(updated_at, str) else "",
    )


def _encode_repository(repository: str) -> str:
    parts = repository.split("/")
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise ValueError(
            "repository must be in 'owner/repo' format for GitHub API requests."
        )
    owner = urllib_parse.quote(parts[0].strip(), safe="")
    repo = urllib_parse.quote(parts[1].strip(), safe="")
    return f"{owner}/{repo}"


def _encode_file_path(path: str) -> str:
    trimmed = path.strip().strip("/")
    if not trimmed:
        raise ValueError("path must be non-empty when requesting file content.")
    segments = [urllib_parse.quote(part, safe="") for part in trimmed.split("/")]
    return "/".join(segments)


def _github_error_message(*, method: str, url: str, status_code: int, body: str) -> str:
    detail = ""
    if body.strip():
        try:
            payload = json.loads(body)
            if isinstance(payload, Mapping):
                message = payload.get("message")
                if isinstance(message, str):
                    detail = message.strip()
        except json.JSONDecodeError:
            detail = ""
    if detail:
        return f"GitHub API {method} {url} failed with {status_code}: {detail}"
    return f"GitHub API {method} {url} failed with {status_code}."


__all__ = [
    "CommentSyncResult",
    "GitHubApiClient",
    "GitHubApiError",
    "NOTEBOOKLENS_COMMENT_MARKER",
    "PullRequestComment",
    "claude_succeeded_from_metadata",
    "ensure_marker",
    "render_pull_request_comment",
    "sync_review_comment",
]
