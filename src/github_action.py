"""Runtime orchestration for the NotebookLens Docker GitHub Action.
- pull request event/input handling for Docker action runtime
- GitHub API notebook discovery/content retrieval without checkout
- provider selection (`none` | `claude`) with fork-safe fallback behavior
- deterministic orchestration using existing diff/provider contracts
- markdown comment rendering
- marker comment create/update/delete idempotency
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Mapping, Optional, Protocol, Sequence, Tuple
import yaml

from .claude_integration import (
    ProviderConfig,
    ProviderInterface,
    ProviderName,
    ProviderRunMetadata,
    build_base_reviewer_guidance,
    build_provider,
)
from .diff_engine import DiffLimits, NotebookDiff, NotebookInput, ReviewResult, build_notebook_diff
from .github_api import CommentSyncResult, GitHubApiClient, claude_succeeded_from_metadata, sync_review_comment


SUPPORTED_EVENT_NAME = "pull_request"
SUPPORTED_EVENT_ACTIONS = {"opened", "synchronize", "reopened"}
NOTEBOOK_EXTENSION = ".ipynb"
CONFIG_FILE_PATH = ".github/notebooklens.yml"


@dataclass(frozen=True)
class PullRequestContext:
    repository: str
    base_repository: str
    head_repository: str
    pull_number: int
    base_sha: str
    head_sha: str
    is_fork: bool
    event_name: str
    event_action: str


@dataclass(frozen=True)
class ReviewerPlaybookConfig:
    name: str
    paths: Tuple[str, ...]
    prompts: Tuple[str, ...]


@dataclass(frozen=True)
class NotebookLensConfig:
    version: int
    reviewer_playbooks: Tuple[ReviewerPlaybookConfig, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ActionInputs:
    ai_provider: ProviderName = "none"
    ai_api_key: Optional[str] = None
    redact_secrets: bool = True
    redact_emails: bool = True


@dataclass(frozen=True)
class PullRequestFile:
    path: str
    status: str
    previous_path: Optional[str] = None
    size_bytes: Optional[int] = None


@dataclass(frozen=True)
class ActionRunMetadata:
    requested_provider: ProviderName
    effective_provider: ProviderName
    claude_called: bool
    used_fallback: bool
    fallback_reason: Optional[str]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    estimated_cost_usd: Optional[float]


RunStatus = Literal["unsupported_event", "no_notebook_changes", "review_ready"]


@dataclass(frozen=True)
class ActionRunResult:
    status: RunStatus
    skip_reason: Optional[str]
    context: Optional[PullRequestContext]
    changed_notebook_paths: List[str]
    notebook_diff: Optional[NotebookDiff]
    review_result: Optional[ReviewResult]
    notices: List[str]
    metadata: ActionRunMetadata
    config_content: Optional[str]
    config: Optional[NotebookLensConfig]
    config_notices: List[str] = field(default_factory=list)


class GitHubNotebookApiClient(Protocol):
    """Boundary for GitHub API access used by the action runtime."""

    def list_pull_request_files(self, *, repository: str, pull_number: int) -> Sequence[Any]:
        """Return files changed in the pull request in deterministic API order."""

    def get_file_content(self, *, repository: str, path: str, ref: str) -> Optional[str]:
        """Return decoded file content at a given ref, or None when unavailable."""


@dataclass(frozen=True)
class _NotebookSelection:
    path: str
    change_type: Literal["added", "modified", "deleted"]
    base_path: Optional[str]
    head_path: Optional[str]
    declared_head_size_bytes: Optional[int]


def load_action_inputs(env: Optional[Mapping[str, str]] = None) -> ActionInputs:
    env_map = dict(os.environ if env is None else env)

    ai_provider_raw = (_read_action_input(env_map, "ai-provider") or "none").strip().lower()
    if ai_provider_raw not in {"none", "claude"}:
        raise ValueError(f"Unsupported ai-provider for v0.1.0: {ai_provider_raw}")

    ai_api_key = (_read_action_input(env_map, "ai-api-key") or "").strip() or None
    redact_secrets = _parse_bool(_read_action_input(env_map, "redact-secrets"), default=True)
    redact_emails = _parse_bool(_read_action_input(env_map, "redact-emails"), default=True)

    return ActionInputs(
        ai_provider=ai_provider_raw,
        ai_api_key=ai_api_key,
        redact_secrets=redact_secrets,
        redact_emails=redact_emails,
    )


def load_pull_request_context(
    *,
    env: Optional[Mapping[str, str]] = None,
    event_payload: Optional[Mapping[str, Any]] = None,
) -> PullRequestContext:
    env_map = dict(os.environ if env is None else env)
    payload = dict(event_payload) if event_payload is not None else _load_event_payload_from_path(env_map)

    event_name = str(env_map.get("GITHUB_EVENT_NAME", "")).strip() or str(
        payload.get("event_name", "")
    ).strip()
    event_action = str(payload.get("action", "")).strip()

    repository = str(env_map.get("GITHUB_REPOSITORY", "")).strip()
    if not repository:
        repository = _read_nested_str(payload, ("repository", "full_name"))
    if not repository:
        raise ValueError("Missing repository in runtime context (GITHUB_REPOSITORY or event payload).")

    pull_number = _read_nested_int(payload, ("number",))
    if pull_number is None:
        pull_number = _read_nested_int(payload, ("pull_request", "number"))
    if pull_number is None:
        raise ValueError("Missing pull request number in event payload.")

    base_sha = _read_nested_str(payload, ("pull_request", "base", "sha"))
    head_sha = _read_nested_str(payload, ("pull_request", "head", "sha"))
    if not base_sha or not head_sha:
        raise ValueError("Missing pull request base/head SHA in event payload.")

    head_repo_fork = _read_nested_bool(payload, ("pull_request", "head", "repo", "fork"))
    head_repo_full_name = _read_nested_str(payload, ("pull_request", "head", "repo", "full_name"))
    is_fork = bool(head_repo_fork)
    if head_repo_full_name:
        is_fork = is_fork or (head_repo_full_name != repository)

    base_repo_full_name = _read_nested_str(payload, ("pull_request", "base", "repo", "full_name"))
    if not base_repo_full_name:
        base_repo_full_name = repository
    head_repo_full_name = head_repo_full_name or repository

    return PullRequestContext(
        repository=repository,
        base_repository=base_repo_full_name,
        head_repository=head_repo_full_name,
        pull_number=pull_number,
        base_sha=base_sha,
        head_sha=head_sha,
        is_fork=is_fork,
        event_name=event_name,
        event_action=event_action,
    )


def run_action(
    *,
    github_api: GitHubNotebookApiClient,
    env: Optional[Mapping[str, str]] = None,
    event_payload: Optional[Mapping[str, Any]] = None,
    context: Optional[PullRequestContext] = None,
    inputs: Optional[ActionInputs] = None,
    limits: DiffLimits = DiffLimits(),
    provider_factory: Callable[[ProviderConfig], ProviderInterface] = build_provider,
    emit_logs: bool = True,
) -> ActionRunResult:
    """Run action orchestration and return structured review output."""
    action_inputs = load_action_inputs(env=env) if inputs is None else inputs
    pr_context = (
        load_pull_request_context(env=env, event_payload=event_payload)
        if context is None
        else context
    )

    if not _is_supported_pr_event(pr_context):
        reason = (
            f"Skipping event {pr_context.event_name}:{pr_context.event_action}; "
            "supported pull_request actions are opened, synchronize, reopened."
        )
        result = ActionRunResult(
            status="unsupported_event",
            skip_reason=reason,
            context=pr_context,
            changed_notebook_paths=[],
            notebook_diff=None,
            review_result=None,
            notices=[reason],
            metadata=_metadata_for_skip(action_inputs),
            config_content=None,
            config=None,
            config_notices=[],
        )
        if emit_logs:
            log_run_result(result)
        return result

    config_content, config_notices = _fetch_notebooklens_config(
        github_api=github_api,
        context=pr_context,
    )
    parsed_config, parse_notices = _parse_notebooklens_config(config_content)
    config_notices.extend(parse_notices)

    raw_files = github_api.list_pull_request_files(
        repository=pr_context.repository,
        pull_number=pr_context.pull_number,
    )
    pr_files = [_coerce_pr_file(raw_file) for raw_file in raw_files]
    notebook_files = _select_notebook_files(pr_files)
    changed_notebook_paths = [item.path for item in notebook_files]

    if not notebook_files:
        reason = "No changed .ipynb files in pull request; exiting without review output."
        notices = list(config_notices)
        notices.append(reason)
        result = ActionRunResult(
            status="no_notebook_changes",
            skip_reason=reason,
            context=pr_context,
            changed_notebook_paths=[],
            notebook_diff=None,
            review_result=None,
            notices=notices,
            metadata=_metadata_for_skip(action_inputs),
            config_content=config_content,
            config=parsed_config,
            config_notices=list(config_notices),
        )
        if emit_logs:
            log_run_result(result)
        return result

    notebook_inputs, discovery_notices = _build_notebook_inputs(
        github_api=github_api,
        context=pr_context,
        notebook_files=notebook_files,
        limits=limits,
    )

    notebook_diff = build_notebook_diff(notebook_inputs, limits=limits)
    base_reviewer_guidance = build_base_reviewer_guidance(
        notebook_diff,
        parsed_config.reviewer_playbooks if parsed_config is not None else (),
    )
    provider, requested_provider, selected_provider, provider_notices = _resolve_provider(
        context=pr_context,
        inputs=action_inputs,
        base_reviewer_guidance=base_reviewer_guidance,
        provider_factory=provider_factory,
    )

    review_result = provider.review(notebook_diff)
    provider_meta = provider.last_run_metadata
    runtime_effective_provider = "none" if provider_meta.used_fallback else selected_provider

    notices = list(config_notices)
    notices.extend(discovery_notices)
    notices.extend(provider_notices)
    if provider_meta.used_fallback and provider_meta.fallback_reason:
        notices.append(f"Claude fallback to none: {provider_meta.fallback_reason}")

    merged_notices = list(notebook_diff.notices)
    merged_notices.extend(notices)
    notebook_diff = NotebookDiff(
        notebooks=notebook_diff.notebooks,
        total_notebooks_changed=notebook_diff.total_notebooks_changed,
        total_cells_changed=notebook_diff.total_cells_changed,
        notices=merged_notices,
    )

    metadata = ActionRunMetadata(
        requested_provider=requested_provider,
        effective_provider=runtime_effective_provider,
        claude_called=provider_meta.claude_called,
        used_fallback=provider_meta.used_fallback or selected_provider != requested_provider,
        fallback_reason=_merge_fallback_reason(
            selected_provider=selected_provider,
            requested_provider=requested_provider,
            provider_meta=provider_meta,
            provider_notices=provider_notices,
        ),
        input_tokens=provider_meta.input_tokens,
        output_tokens=provider_meta.output_tokens,
        estimated_cost_usd=None,
    )

    result = ActionRunResult(
        status="review_ready",
        skip_reason=None,
        context=pr_context,
        changed_notebook_paths=changed_notebook_paths,
        notebook_diff=notebook_diff,
        review_result=review_result,
        notices=list(notebook_diff.notices),
        metadata=metadata,
        config_content=config_content,
        config=parsed_config,
        config_notices=list(config_notices),
    )
    if emit_logs:
        log_run_result(result)
    return result


def log_run_result(result: ActionRunResult) -> None:
    """Emit structured runtime metadata for validation and debugging."""
    payload = {
        "status": result.status,
        "skip_reason": result.skip_reason,
        "requested_provider": result.metadata.requested_provider,
        "effective_provider": result.metadata.effective_provider,
        "claude_called": result.metadata.claude_called,
        "used_fallback": result.metadata.used_fallback,
        "fallback_reason": result.metadata.fallback_reason,
        "input_tokens": result.metadata.input_tokens,
        "output_tokens": result.metadata.output_tokens,
        "estimated_cost_usd": result.metadata.estimated_cost_usd,
        "changed_notebooks": len(result.changed_notebook_paths),
        "total_cells_changed": (
            result.notebook_diff.total_cells_changed if result.notebook_diff is not None else 0
        ),
    }
    print(f"notebooklens.runtime {json.dumps(payload, sort_keys=True)}")


def run_action_from_env(
    *,
    env: Optional[Mapping[str, str]] = None,
    event_payload: Optional[Mapping[str, Any]] = None,
    github_api: Optional[GitHubApiClient] = None,
    limits: DiffLimits = DiffLimits(),
    provider_factory: Callable[[ProviderConfig], ProviderInterface] = build_provider,
    emit_logs: bool = True,
) -> Tuple[ActionRunResult, Optional[CommentSyncResult]]:
    """Execute runtime flow with concrete GitHub API + comment sync wiring."""
    env_map = dict(os.environ if env is None else env)
    resolved_api = github_api if github_api is not None else GitHubApiClient.from_env(env_map)

    run_result = run_action(
        github_api=resolved_api,
        env=env_map,
        event_payload=event_payload,
        limits=limits,
        provider_factory=provider_factory,
        emit_logs=emit_logs,
    )

    if run_result.status == "unsupported_event" or run_result.context is None:
        _write_action_outputs(run_result, env_map)
        return run_result, None

    comment_sync = sync_review_comment(
        github_api=resolved_api,
        repository=run_result.context.repository,
        pull_number=run_result.context.pull_number,
        has_notebook_changes=bool(run_result.changed_notebook_paths),
        notebook_diff=run_result.notebook_diff,
        review_result=run_result.review_result,
        claude_succeeded=claude_succeeded_from_metadata(run_result.metadata),
        notices=run_result.notices,
    )
    if emit_logs:
        payload = {
            "action": comment_sync.action,
            "comment_id": comment_sync.comment_id,
            "deleted_comment_ids": comment_sync.deleted_comment_ids,
            "details": comment_sync.details,
        }
        print(f"notebooklens.comment_sync {json.dumps(payload, sort_keys=True)}")
    _write_action_outputs(run_result, env_map)
    return run_result, comment_sync


def main() -> int:
    """Container entrypoint for GitHub Actions runtime."""
    run_action_from_env()
    return 0


def _build_notebook_inputs(
    *,
    github_api: GitHubNotebookApiClient,
    context: PullRequestContext,
    notebook_files: Sequence[_NotebookSelection],
    limits: DiffLimits,
) -> Tuple[List[NotebookInput], List[str]]:
    notebook_inputs: List[NotebookInput] = []
    notices: List[str] = []

    for index, selection in enumerate(notebook_files):
        should_fetch_content = index < limits.max_notebooks_per_pr
        skip_head_for_size = (
            selection.declared_head_size_bytes is not None
            and selection.declared_head_size_bytes > limits.max_notebook_bytes
        )

        base_content: Optional[str] = None
        head_content: Optional[str] = None

        if should_fetch_content and selection.base_path is not None and not skip_head_for_size:
            base_content, base_notice = _safe_fetch_file_content(
                github_api=github_api,
                repository=context.base_repository,
                path=selection.base_path,
                ref=context.base_sha,
            )
            if base_notice is not None:
                notices.append(f"{selection.path}: {base_notice}")

        if should_fetch_content and selection.head_path is not None and not skip_head_for_size:
            head_content, head_notice = _safe_fetch_file_content(
                github_api=github_api,
                repository=context.head_repository,
                path=selection.head_path,
                ref=context.head_sha,
            )
            if head_notice is not None:
                notices.append(f"{selection.path}: {head_notice}")

        notebook_inputs.append(
            NotebookInput(
                path=selection.path,
                change_type=selection.change_type,
                base_content=base_content,
                head_content=head_content,
                base_size_bytes=None,
                head_size_bytes=selection.declared_head_size_bytes,
            )
        )

    return notebook_inputs, notices


def _fetch_notebooklens_config(
    *,
    github_api: GitHubNotebookApiClient,
    context: PullRequestContext,
) -> Tuple[Optional[str], List[str]]:
    content, notice = _safe_fetch_file_content(
        github_api=github_api,
        repository=context.head_repository,
        path=CONFIG_FILE_PATH,
        ref=context.head_sha,
    )
    notices: List[str] = []
    if notice is not None:
        notices.append(f"{CONFIG_FILE_PATH}: {notice}")
    return content, notices


def _parse_notebooklens_config(
    content: Optional[str],
) -> Tuple[Optional[NotebookLensConfig], List[str]]:
    if content is None:
        return None, []

    try:
        raw_config = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        return None, [_invalid_config_notice(f"invalid YAML ({_truncate_reason(exc)})")]

    try:
        return _validate_notebooklens_config(raw_config), []
    except ValueError as exc:
        return None, [_invalid_config_notice(str(exc))]


def _validate_notebooklens_config(raw_config: Any) -> NotebookLensConfig:
    if not isinstance(raw_config, Mapping):
        raise ValueError("expected a top-level YAML mapping")

    version = raw_config.get("version")
    if version != 1:
        raise ValueError("version must be set to 1")

    reviewer_guidance_raw = raw_config.get("reviewer_guidance")
    if reviewer_guidance_raw is None:
        return NotebookLensConfig(version=1)
    if not isinstance(reviewer_guidance_raw, Mapping):
        raise ValueError("reviewer_guidance must be a mapping when present")

    playbooks_raw = reviewer_guidance_raw.get("playbooks")
    if playbooks_raw is None:
        return NotebookLensConfig(version=1)
    if not isinstance(playbooks_raw, list):
        raise ValueError("reviewer_guidance.playbooks must be a list")

    reviewer_playbooks = tuple(
        _normalize_reviewer_playbook(item, index=index)
        for index, item in enumerate(playbooks_raw)
    )
    return NotebookLensConfig(version=1, reviewer_playbooks=reviewer_playbooks)


def _normalize_reviewer_playbook(raw_playbook: Any, *, index: int) -> ReviewerPlaybookConfig:
    if not isinstance(raw_playbook, Mapping):
        raise ValueError(f"reviewer_guidance.playbooks[{index}] must be a mapping")

    name = _normalize_required_string(
        raw_playbook.get("name"),
        field_name=f"reviewer_guidance.playbooks[{index}].name",
    )
    paths = _normalize_string_list(
        raw_playbook.get("paths"),
        field_name=f"reviewer_guidance.playbooks[{index}].paths",
        normalizer=_normalize_playbook_path,
    )
    prompts = _normalize_string_list(
        raw_playbook.get("prompts"),
        field_name=f"reviewer_guidance.playbooks[{index}].prompts",
    )
    return ReviewerPlaybookConfig(name=name, paths=paths, prompts=prompts)


def _normalize_required_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a non-empty string")
    normalized = " ".join(value.strip().split())
    if not normalized:
        raise ValueError(f"{field_name} must be a non-empty string")
    return normalized


def _normalize_string_list(
    value: Any,
    *,
    field_name: str,
    normalizer: Optional[Callable[[str], str]] = None,
) -> Tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a non-empty list of strings")

    seen: Dict[str, None] = {}
    normalized_items: List[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{field_name} must contain only non-empty strings")
        normalized = item.strip()
        if normalizer is not None:
            normalized = normalizer(normalized)
        if not normalized:
            raise ValueError(f"{field_name} must contain only non-empty strings")
        if normalized in seen:
            continue
        seen[normalized] = None
        normalized_items.append(normalized)

    if not normalized_items:
        raise ValueError(f"{field_name} must be a non-empty list of strings")

    return tuple(normalized_items)


def _normalize_playbook_path(value: str) -> str:
    return value.replace("\\", "/")


def _invalid_config_notice(reason: str) -> str:
    return (
        f"Ignored reviewer guidance playbooks from {CONFIG_FILE_PATH}: {reason}. "
        "Continuing with built-in guidance only."
    )


def _safe_fetch_file_content(
    *,
    github_api: GitHubNotebookApiClient,
    repository: str,
    path: str,
    ref: str,
) -> Tuple[Optional[str], Optional[str]]:
    try:
        content = github_api.get_file_content(repository=repository, path=path, ref=ref)
        return content, None
    except Exception as exc:  # pragma: no cover - runtime integration boundary
        return None, f"failed to fetch content from GitHub API ({_truncate_reason(exc)})"


def _resolve_provider(
    *,
    context: PullRequestContext,
    inputs: ActionInputs,
    base_reviewer_guidance: Sequence[Any],
    provider_factory: Callable[[ProviderConfig], ProviderInterface],
) -> Tuple[ProviderInterface, ProviderName, ProviderName, List[str]]:
    requested = inputs.ai_provider
    notices: List[str] = []

    if requested == "none":
        return (
            provider_factory(_provider_config(inputs, "none", base_reviewer_guidance)),
            requested,
            "none",
            notices,
        )

    api_key_present = bool((inputs.ai_api_key or "").strip())
    if not api_key_present:
        if context.is_fork:
            notices.append(
                "Fork PR has no ai-api-key for ai-provider=claude; falling back to none mode."
            )
        else:
            notices.append("ai-provider=claude requested without ai-api-key; falling back to none mode.")
        return (
            provider_factory(_provider_config(inputs, "none", base_reviewer_guidance)),
            requested,
            "none",
            notices,
        )

    provider = provider_factory(_provider_config(inputs, "claude", base_reviewer_guidance))
    return provider, requested, "claude", notices


def _provider_config(
    inputs: ActionInputs,
    ai_provider: ProviderName,
    base_reviewer_guidance: Sequence[Any],
) -> ProviderConfig:
    return ProviderConfig(
        ai_provider=ai_provider,
        ai_api_key=inputs.ai_api_key,
        redact_secrets=inputs.redact_secrets,
        redact_emails=inputs.redact_emails,
        base_reviewer_guidance=tuple(base_reviewer_guidance),
    )


def _select_notebook_files(pr_files: Sequence[PullRequestFile]) -> List[_NotebookSelection]:
    selections: List[_NotebookSelection] = []
    for file in pr_files:
        status = file.status.lower().strip()
        current_is_notebook = _is_notebook_path(file.path)
        previous_is_notebook = _is_notebook_path(file.previous_path or "")

        if not current_is_notebook and not previous_is_notebook:
            continue

        if status in {"removed", "deleted"}:
            if current_is_notebook:
                selections.append(
                    _NotebookSelection(
                        path=file.path,
                        change_type="deleted",
                        base_path=file.path,
                        head_path=None,
                        declared_head_size_bytes=file.size_bytes,
                    )
                )
            continue

        if status == "added":
            if current_is_notebook:
                selections.append(
                    _NotebookSelection(
                        path=file.path,
                        change_type="added",
                        base_path=None,
                        head_path=file.path,
                        declared_head_size_bytes=file.size_bytes,
                    )
                )
            continue

        if status == "renamed":
            if previous_is_notebook and current_is_notebook:
                selections.append(
                    _NotebookSelection(
                        path=file.path,
                        change_type="modified",
                        base_path=file.previous_path,
                        head_path=file.path,
                        declared_head_size_bytes=file.size_bytes,
                    )
                )
            elif previous_is_notebook and not current_is_notebook:
                selections.append(
                    _NotebookSelection(
                        path=file.previous_path or file.path,
                        change_type="deleted",
                        base_path=file.previous_path,
                        head_path=None,
                        declared_head_size_bytes=file.size_bytes,
                    )
                )
            elif current_is_notebook and not previous_is_notebook:
                selections.append(
                    _NotebookSelection(
                        path=file.path,
                        change_type="added",
                        base_path=None,
                        head_path=file.path,
                        declared_head_size_bytes=file.size_bytes,
                    )
                )
            continue

        if current_is_notebook:
            selections.append(
                _NotebookSelection(
                    path=file.path,
                    change_type="modified",
                    base_path=file.path,
                    head_path=file.path,
                    declared_head_size_bytes=file.size_bytes,
                )
            )

    return selections


def _coerce_pr_file(raw_file: Any) -> PullRequestFile:
    if isinstance(raw_file, PullRequestFile):
        return raw_file

    if isinstance(raw_file, Mapping):
        return _coerce_pr_file_from_mapping(raw_file)

    path = getattr(raw_file, "path", None) or getattr(raw_file, "filename", None)
    status = getattr(raw_file, "status", None)
    previous_path = getattr(raw_file, "previous_path", None) or getattr(
        raw_file, "previous_filename", None
    )
    size_bytes = _coerce_optional_int(getattr(raw_file, "size_bytes", None))
    if size_bytes is None:
        size_bytes = _coerce_optional_int(getattr(raw_file, "size", None))
    if not isinstance(path, str) or not isinstance(status, str):
        raise ValueError("Pull request file entries must provide path/filename and status.")
    return PullRequestFile(
        path=path,
        status=status,
        previous_path=previous_path if isinstance(previous_path, str) else None,
        size_bytes=size_bytes,
    )


def _coerce_pr_file_from_mapping(raw_file: Mapping[str, Any]) -> PullRequestFile:
    path = raw_file.get("path", raw_file.get("filename"))
    status = raw_file.get("status")
    previous_path = raw_file.get("previous_path", raw_file.get("previous_filename"))
    size_bytes = _coerce_optional_int(raw_file.get("size_bytes"))
    if size_bytes is None:
        size_bytes = _coerce_optional_int(raw_file.get("size"))
    if not isinstance(path, str) or not isinstance(status, str):
        raise ValueError("Pull request file entries must provide path/filename and status.")
    return PullRequestFile(
        path=path,
        status=status,
        previous_path=previous_path if isinstance(previous_path, str) else None,
        size_bytes=size_bytes,
    )


def _is_notebook_path(path: str) -> bool:
    return path.lower().endswith(NOTEBOOK_EXTENSION)


def _is_supported_pr_event(context: PullRequestContext) -> bool:
    return (
        context.event_name == SUPPORTED_EVENT_NAME
        and context.event_action in SUPPORTED_EVENT_ACTIONS
    )


def _metadata_for_skip(inputs: ActionInputs) -> ActionRunMetadata:
    return ActionRunMetadata(
        requested_provider=inputs.ai_provider,
        effective_provider="none",
        claude_called=False,
        used_fallback=False,
        fallback_reason=None,
        input_tokens=None,
        output_tokens=None,
        estimated_cost_usd=None,
    )


def _write_action_outputs(result: ActionRunResult, env: Mapping[str, str]) -> None:
    output_path = str(env.get("GITHUB_OUTPUT", "")).strip()
    if not output_path:
        return

    outputs = {
        "effective-provider": result.metadata.effective_provider,
        "changed-notebooks": str(len(result.changed_notebook_paths)),
        "total-cells-changed": str(
            result.notebook_diff.total_cells_changed if result.notebook_diff is not None else 0
        ),
        "fallback-reason": result.metadata.fallback_reason or "",
    }

    with Path(output_path).open("a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            delimiter = f"NOTEBOOKLENS_{key.replace('-', '_').upper()}_EOF"
            handle.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")


def _merge_fallback_reason(
    *,
    selected_provider: ProviderName,
    requested_provider: ProviderName,
    provider_meta: ProviderRunMetadata,
    provider_notices: Sequence[str],
) -> Optional[str]:
    reasons: List[str] = []
    if selected_provider != requested_provider and provider_notices:
        reasons.extend(provider_notices)
    if provider_meta.fallback_reason:
        reasons.append(provider_meta.fallback_reason)
    if not reasons:
        return None
    return " | ".join(reasons)


def _load_event_payload_from_path(env: Mapping[str, str]) -> Dict[str, Any]:
    event_path = str(env.get("GITHUB_EVENT_PATH", "")).strip()
    if not event_path:
        raise ValueError("Missing GITHUB_EVENT_PATH for pull request event payload.")
    payload_text = Path(event_path).read_text(encoding="utf-8")
    raw_payload = json.loads(payload_text)
    if not isinstance(raw_payload, dict):
        raise ValueError("GitHub event payload must be a JSON object.")
    return raw_payload


def _read_action_input(env: Mapping[str, str], key: str) -> Optional[str]:
    upper_key = key.upper()
    hyphen_key = f"INPUT_{upper_key}"
    underscore_key = f"INPUT_{upper_key.replace('-', '_')}"
    for env_key in (underscore_key, hyphen_key, key, key.replace("-", "_")):
        value = env.get(env_key)
        if value is not None:
            return value
    return None


def _parse_bool(raw_value: Optional[str], *, default: bool) -> bool:
    if raw_value is None:
        return default
    value = raw_value.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _read_nested_str(payload: Mapping[str, Any], path: Sequence[str]) -> str:
    current: Any = payload
    for key in path:
        if not isinstance(current, Mapping):
            return ""
        current = current.get(key)
    if isinstance(current, str):
        return current.strip()
    return ""


def _read_nested_int(payload: Mapping[str, Any], path: Sequence[str]) -> Optional[int]:
    current: Any = payload
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    if isinstance(current, int):
        return current
    return None


def _read_nested_bool(payload: Mapping[str, Any], path: Sequence[str]) -> Optional[bool]:
    current: Any = payload
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    if isinstance(current, bool):
        return current
    return None


def _coerce_optional_int(value: Any) -> Optional[int]:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
    return None


def _truncate_reason(exc: BaseException) -> str:
    text = str(exc).strip()
    if not text:
        return exc.__class__.__name__
    return text[:220]


__all__ = [
    "ActionInputs",
    "ActionRunMetadata",
    "ActionRunResult",
    "GitHubNotebookApiClient",
    "NotebookLensConfig",
    "PullRequestContext",
    "PullRequestFile",
    "ReviewerPlaybookConfig",
    "load_action_inputs",
    "load_pull_request_context",
    "log_run_result",
    "main",
    "run_action",
    "run_action_from_env",
]


if __name__ == "__main__":  # pragma: no cover - exercised via runtime entrypoint tests
    raise SystemExit(main())
