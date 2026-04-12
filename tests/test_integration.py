from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.claude_integration import NoneProvider, ProviderConfig, ProviderInterface, ProviderRunMetadata
from src.diff_engine import NotebookDiff, ReviewResult
from src.github_action import ActionInputs, PullRequestContext, main, run_action, run_action_from_env
from src.github_api import (
    NOTEBOOKLENS_COMMENT_MARKER,
    GitHubApiClient,
    GitHubApiError,
    PullRequestComment,
    claude_succeeded_from_metadata,
    sync_review_comment,
)


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def parse_github_output(path: Path) -> Dict[str, str]:
    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    parsed: Dict[str, str] = {}
    index = 0
    while index < len(lines):
        header = lines[index]
        key, delimiter = header.split("<<", 1)
        index += 1
        value_lines: List[str] = []
        while index < len(lines) and lines[index] != delimiter:
            value_lines.append(lines[index])
            index += 1
        parsed[key] = "\n".join(value_lines)
        index += 1
    return parsed


@dataclass
class InMemoryGitHubApiClient(GitHubApiClient):
    pr_files: List[Dict[str, Any]]
    contents_by_ref: Dict[Tuple[str, str], Optional[str]]
    comments: List[PullRequestComment]

    def __init__(
        self,
        *,
        pr_files: Sequence[Mapping[str, Any]],
        contents_by_ref: Mapping[Tuple[str, str], Optional[str]],
        comments: Optional[Sequence[PullRequestComment]] = None,
    ) -> None:
        super().__init__(token="test-token")
        self.pr_files = [dict(item) for item in pr_files]
        self.contents_by_ref = dict(contents_by_ref)
        self.comments = list(comments or [])
        self.created_bodies: List[str] = []
        self.updated_bodies: Dict[int, str] = {}
        self.deleted_comment_ids: List[int] = []
        self.content_requests: List[Tuple[str, str, str]] = []
        self._next_comment_id = max((item.comment_id for item in self.comments), default=0) + 1

    def list_pull_request_files(self, *, repository: str, pull_number: int) -> Sequence[Any]:
        return list(self.pr_files)

    def get_file_content(self, *, repository: str, path: str, ref: str) -> Optional[str]:
        self.content_requests.append((repository, path, ref))
        return self.contents_by_ref.get((path, ref))

    def list_pull_request_comments(self, *, repository: str, pull_number: int) -> List[PullRequestComment]:
        return list(self.comments)

    def create_pull_request_comment(
        self,
        *,
        repository: str,
        pull_number: int,
        body: str,
    ) -> PullRequestComment:
        comment = PullRequestComment(
            comment_id=self._next_comment_id,
            body=body,
            author_login="github-actions[bot]",
            author_type="Bot",
            updated_at=f"2026-04-10T00:00:{self._next_comment_id:02d}Z",
        )
        self._next_comment_id += 1
        self.comments.append(comment)
        self.created_bodies.append(body)
        return comment

    def update_pull_request_comment(
        self,
        *,
        repository: str,
        comment_id: int,
        body: str,
    ) -> PullRequestComment:
        for idx, existing in enumerate(self.comments):
            if existing.comment_id != comment_id:
                continue
            updated = PullRequestComment(
                comment_id=existing.comment_id,
                body=body,
                author_login=existing.author_login,
                author_type=existing.author_type,
                updated_at=f"2026-04-10T00:01:{comment_id:02d}Z",
            )
            self.comments[idx] = updated
            self.updated_bodies[comment_id] = body
            return updated
        raise GitHubApiError(
            f"comment {comment_id} not found",
            status_code=404,
        )

    def delete_pull_request_comment(self, *, repository: str, comment_id: int) -> None:
        for idx, existing in enumerate(self.comments):
            if existing.comment_id != comment_id:
                continue
            del self.comments[idx]
            self.deleted_comment_ids.append(comment_id)
            return
        raise GitHubApiError(
            f"comment {comment_id} not found",
            status_code=404,
        )


def _context(*, is_fork: bool, action: str = "opened") -> PullRequestContext:
    repository = "acme/notebooklens-fixture"
    return PullRequestContext(
        repository=repository,
        base_repository=repository,
        head_repository="fork-user/notebooklens-fixture" if is_fork else repository,
        pull_number=42,
        base_sha="base-sha",
        head_sha="head-sha",
        is_fork=is_fork,
        event_name="pull_request",
        event_action=action,
    )


def _modified_notebook_files() -> List[Dict[str, Any]]:
    return [
        {
            "filename": "analysis/notebook.ipynb",
            "status": "modified",
            "size": 1_024,
        }
    ]


def _reviewer_guidance_training_files() -> List[Dict[str, Any]]:
    return [
        {
            "filename": "notebooks/training/churn_model.ipynb",
            "status": "modified",
            "size": 2_048,
        }
    ]


def _renamed_reviewer_guidance_training_files() -> List[Dict[str, Any]]:
    return [
        {
            "filename": "notebooks/training/churn_model.ipynb",
            "previous_filename": "analysis/churn_model.ipynb",
            "status": "renamed",
            "size": 2_048,
        }
    ]


def _readme_only_files() -> List[Dict[str, Any]]:
    return [
        {
            "filename": "README.md",
            "status": "modified",
            "size": 256,
        }
    ]


def _contents_for_modified_notebook() -> Dict[Tuple[str, str], Optional[str]]:
    path = "analysis/notebook.ipynb"
    return {
        (path, "base-sha"): fixture_text("simple_base.ipynb"),
        (path, "head-sha"): fixture_text("simple_head.ipynb"),
    }


def _contents_for_medium_notebook() -> Dict[Tuple[str, str], Optional[str]]:
    path = "analysis/notebook.ipynb"
    return {
        (path, "base-sha"): fixture_text("medium_base.ipynb"),
        (path, "head-sha"): fixture_text("medium_head.ipynb"),
    }


def _contents_for_reviewer_guidance_training_notebook() -> Dict[Tuple[str, str], Optional[str]]:
    path = "notebooks/training/churn_model.ipynb"
    return {
        (path, "base-sha"): fixture_text("reviewer_guidance_training_base.ipynb"),
        (path, "head-sha"): fixture_text("reviewer_guidance_training_head.ipynb"),
    }


def _contents_for_renamed_reviewer_guidance_training_notebook() -> Dict[Tuple[str, str], Optional[str]]:
    return {
        ("analysis/churn_model.ipynb", "base-sha"): fixture_text(
            "reviewer_guidance_training_base.ipynb"
        ),
        ("notebooks/training/churn_model.ipynb", "head-sha"): fixture_text(
            "reviewer_guidance_training_head.ipynb"
        ),
    }


def _seed_comment(
    *,
    comment_id: int,
    body: str,
    author_login: str,
    author_type: str,
    updated_at: str,
) -> PullRequestComment:
    return PullRequestComment(
        comment_id=comment_id,
        body=body,
        author_login=author_login,
        author_type=author_type,
        updated_at=updated_at,
    )


def _run_and_sync(
    *,
    github_api: InMemoryGitHubApiClient,
    context: PullRequestContext,
    inputs: ActionInputs,
    provider_factory: Optional[Any] = None,
) -> Tuple[Any, Any]:
    result = run_action(
        github_api=github_api,
        context=context,
        inputs=inputs,
        provider_factory=provider_factory if provider_factory is not None else NoneProviderFactory(),
        emit_logs=False,
    )
    sync_result = sync_review_comment(
        github_api=github_api,
        repository=context.repository,
        pull_number=context.pull_number,
        has_notebook_changes=bool(result.changed_notebook_paths),
        notebook_diff=result.notebook_diff,
        review_result=result.review_result,
        claude_succeeded=claude_succeeded_from_metadata(result.metadata),
        notices=result.notices,
    )
    return result, sync_result


class NoneProviderFactory:
    def __call__(self, config: ProviderConfig) -> ProviderInterface:
        return NoneProvider(base_reviewer_guidance=config.base_reviewer_guidance)


class FailingClaudeProvider(ProviderInterface):
    def __init__(self, *, base_reviewer_guidance: Sequence[Any] = ()) -> None:
        super().__init__()
        self.base_reviewer_guidance = tuple(base_reviewer_guidance)

    def review(self, diff: NotebookDiff) -> ReviewResult:
        fallback = NoneProvider(base_reviewer_guidance=self.base_reviewer_guidance).review(diff)
        self.last_run_metadata = ProviderRunMetadata(
            provider="claude",
            claude_called=True,
            used_fallback=True,
            fallback_reason="simulated provider failure",
            input_tokens=128,
            output_tokens=0,
        )
        return ReviewResult(
            summary="Claude unavailable: simulated provider failure. Used deterministic local findings.",
            flagged_issues=fallback.flagged_issues,
        )


class FailingProviderFactory:
    def __call__(self, config: ProviderConfig) -> ProviderInterface:
        if config.ai_provider == "claude":
            return FailingClaudeProvider(base_reviewer_guidance=config.base_reviewer_guidance)
        return NoneProvider(base_reviewer_guidance=config.base_reviewer_guidance)


def test_end_to_end_action_flow_creates_marker_comment() -> None:
    api = InMemoryGitHubApiClient(
        pr_files=_modified_notebook_files(),
        contents_by_ref=_contents_for_modified_notebook(),
    )
    context = _context(is_fork=False)
    inputs = ActionInputs(ai_provider="none", ai_api_key=None, redact_secrets=True, redact_emails=True)

    result, sync_result = _run_and_sync(github_api=api, context=context, inputs=inputs)

    assert result.status == "review_ready"
    assert sync_result.action == "created"
    assert len(api.comments) == 1
    body = api.comments[0].body
    assert NOTEBOOKLENS_COMMENT_MARKER in body
    assert "## NotebookLens" in body
    assert "### Notebook Changes" in body
    assert "<summary>AI summary (Claude)</summary>" not in body


def test_fork_pr_without_key_falls_back_to_none_with_visible_notice() -> None:
    api = InMemoryGitHubApiClient(
        pr_files=_modified_notebook_files(),
        contents_by_ref=_contents_for_modified_notebook(),
    )
    context = _context(is_fork=True)
    inputs = ActionInputs(ai_provider="claude", ai_api_key=None, redact_secrets=True, redact_emails=True)

    result, sync_result = _run_and_sync(github_api=api, context=context, inputs=inputs)

    assert result.status == "review_ready"
    assert result.metadata.requested_provider == "claude"
    assert result.metadata.effective_provider == "none"
    assert result.metadata.used_fallback is True
    assert result.metadata.fallback_reason is not None
    assert "Fork PR has no ai-api-key" in result.metadata.fallback_reason
    assert sync_result.action == "created"
    assert "Fork PR has no ai-api-key for ai-provider=claude; falling back to none mode." in api.comments[0].body


def test_provider_failure_falls_back_to_none_with_visible_notice() -> None:
    api = InMemoryGitHubApiClient(
        pr_files=_modified_notebook_files(),
        contents_by_ref=_contents_for_modified_notebook(),
    )
    context = _context(is_fork=False)
    inputs = ActionInputs(
        ai_provider="claude",
        ai_api_key="dummy-key",
        redact_secrets=True,
        redact_emails=True,
    )

    result, sync_result = _run_and_sync(
        github_api=api,
        context=context,
        inputs=inputs,
        provider_factory=FailingProviderFactory(),
    )

    assert result.status == "review_ready"
    assert result.metadata.requested_provider == "claude"
    assert result.metadata.effective_provider == "none"
    assert result.metadata.used_fallback is True
    assert result.metadata.fallback_reason is not None
    assert "simulated provider failure" in result.metadata.fallback_reason
    assert any("Claude fallback to none: simulated provider failure" in item for item in result.notices)
    assert sync_result.action == "created"
    assert "Claude fallback to none: simulated provider failure" in api.comments[0].body


def test_no_notebook_exit_returns_cleanly_and_keeps_comments_untouched() -> None:
    api = InMemoryGitHubApiClient(
        pr_files=_readme_only_files(),
        contents_by_ref={},
    )
    context = _context(is_fork=False)
    inputs = ActionInputs(ai_provider="none", ai_api_key=None, redact_secrets=True, redact_emails=True)

    result = run_action(
        github_api=api,
        context=context,
        inputs=inputs,
        provider_factory=NoneProviderFactory(),
        emit_logs=False,
    )
    sync_result = sync_review_comment(
        github_api=api,
        repository=context.repository,
        pull_number=context.pull_number,
        has_notebook_changes=bool(result.changed_notebook_paths),
        notebook_diff=result.notebook_diff,
        review_result=result.review_result,
        claude_succeeded=claude_succeeded_from_metadata(result.metadata),
        notices=result.notices,
    )

    assert result.status == "no_notebook_changes"
    assert result.changed_notebook_paths == []
    assert result.notebook_diff is None
    assert result.review_result is None
    assert sync_result.action == "noop"
    assert api.comments == []


def test_invalid_config_warns_once_and_continues_with_built_in_guidance() -> None:
    api = InMemoryGitHubApiClient(
        pr_files=_modified_notebook_files(),
        contents_by_ref={
            **_contents_for_medium_notebook(),
            (".github/notebooklens.yml", "head-sha"): "version: 2\n",
        },
    )
    context = _context(is_fork=False)
    inputs = ActionInputs(ai_provider="none", ai_api_key=None, redact_secrets=True, redact_emails=True)

    result, sync_result = _run_and_sync(github_api=api, context=context, inputs=inputs)

    assert result.status == "review_ready"
    assert result.config is None
    assert len(result.config_notices) == 1
    assert result.notices.count(result.config_notices[0]) == 1
    assert ".github/notebooklens.yml" in result.config_notices[0]
    assert "Continuing with built-in guidance only." in result.config_notices[0]
    assert sync_result.action == "created"
    assert result.review_result is not None
    assert result.review_result.reviewer_guidance
    assert result.config_notices[0] in api.comments[0].body


def test_same_repo_missing_config_stays_silent_and_keeps_built_in_guidance() -> None:
    api = InMemoryGitHubApiClient(
        pr_files=_reviewer_guidance_training_files(),
        contents_by_ref=_contents_for_reviewer_guidance_training_notebook(),
    )
    context = _context(is_fork=False)
    inputs = ActionInputs(ai_provider="none", ai_api_key=None, redact_secrets=True, redact_emails=True)

    result, sync_result = _run_and_sync(github_api=api, context=context, inputs=inputs)

    assert result.status == "review_ready"
    assert sync_result.action == "created"
    assert result.config_content is None
    assert result.config is None
    assert result.config_notices == []
    assert result.review_result is not None
    assert [item.source for item in result.review_result.reviewer_guidance] == ["built_in"]
    assert [item.message for item in result.review_result.reviewer_guidance] == [
        (
            "Inspect changed outputs for unexplained metric shifts, stale rendered "
            "results, or outputs that now need updated narrative."
        )
    ]
    assert (
        "acme/notebooklens-fixture",
        ".github/notebooklens.yml",
        "head-sha",
    ) in api.content_requests
    assert ".github/notebooklens.yml" not in api.comments[0].body
    assert "Training notebooks:" not in api.comments[0].body


def test_run_action_fetches_config_from_fork_head_repository_and_sha() -> None:
    api = InMemoryGitHubApiClient(
        pr_files=_readme_only_files(),
        contents_by_ref={
            (".github/notebooklens.yml", "head-sha"): (
                "version: 1\n"
                "reviewer_guidance:\n"
                "  playbooks:\n"
                "    - name: Training notebooks\n"
                "      paths:\n"
                "        - notebooks/training/**/*.ipynb\n"
                "      prompts:\n"
                "        - Verify seeds.\n"
            )
        },
    )
    context = PullRequestContext(
        repository="acme/notebooklens-fixture",
        base_repository="acme/notebooklens-fixture",
        head_repository="contrib/notebooklens-fixture",
        pull_number=42,
        base_sha="base-sha",
        head_sha="head-sha",
        is_fork=True,
        event_name="pull_request",
        event_action="opened",
    )
    inputs = ActionInputs(ai_provider="none", ai_api_key=None, redact_secrets=True, redact_emails=True)

    result = run_action(
        github_api=api,
        context=context,
        inputs=inputs,
        provider_factory=NoneProviderFactory(),
        emit_logs=False,
    )

    assert result.status == "no_notebook_changes"
    assert result.config is not None
    assert result.config.reviewer_playbooks[0].name == "Training notebooks"
    assert (
        "contrib/notebooklens-fixture",
        ".github/notebooklens.yml",
        "head-sha",
    ) in api.content_requests
    assert (
        "acme/notebooklens-fixture",
        ".github/notebooklens.yml",
        "head-sha",
    ) not in api.content_requests


def test_fork_pr_head_config_participates_in_reviewer_guidance() -> None:
    api = InMemoryGitHubApiClient(
        pr_files=_reviewer_guidance_training_files(),
        contents_by_ref={
            **_contents_for_reviewer_guidance_training_notebook(),
            (".github/notebooklens.yml", "head-sha"): (
                "version: 1\n"
                "reviewer_guidance:\n"
                "  playbooks:\n"
                "    - name: Training notebooks\n"
                "      paths:\n"
                "        - notebooks/training/**/*.ipynb\n"
                "      prompts:\n"
                "        - Verify the dataset split and random seed changes are intentional.\n"
            ),
        },
    )
    context = PullRequestContext(
        repository="acme/notebooklens-fixture",
        base_repository="acme/notebooklens-fixture",
        head_repository="contrib/notebooklens-fixture",
        pull_number=42,
        base_sha="base-sha",
        head_sha="head-sha",
        is_fork=True,
        event_name="pull_request",
        event_action="opened",
    )
    inputs = ActionInputs(ai_provider="none", ai_api_key=None, redact_secrets=True, redact_emails=True)

    result, sync_result = _run_and_sync(github_api=api, context=context, inputs=inputs)

    assert result.status == "review_ready"
    assert sync_result.action == "created"
    assert result.config is not None
    assert [item.source for item in result.review_result.reviewer_guidance] == [
        "built_in",
        "playbook",
    ]
    assert (
        "contrib/notebooklens-fixture",
        ".github/notebooklens.yml",
        "head-sha",
    ) in api.content_requests
    assert (
        "acme/notebooklens-fixture",
        "notebooks/training/churn_model.ipynb",
        "base-sha",
    ) in api.content_requests
    assert (
        "contrib/notebooklens-fixture",
        "notebooks/training/churn_model.ipynb",
        "head-sha",
    ) in api.content_requests
    assert (
        "- Training notebooks: Verify the dataset split and random seed changes are intentional."
        in api.comments[0].body
    )


def test_training_notebook_fixture_adds_output_guidance_and_playbook_prompts() -> None:
    api = InMemoryGitHubApiClient(
        pr_files=_reviewer_guidance_training_files(),
        contents_by_ref={
            **_contents_for_reviewer_guidance_training_notebook(),
            (".github/notebooklens.yml", "head-sha"): (
                "version: 1\n"
                "reviewer_guidance:\n"
                "  playbooks:\n"
                "    - name: Training notebooks\n"
                "      paths:\n"
                "        - notebooks/training/**/*.ipynb\n"
                "      prompts:\n"
                "        - Verify the dataset split and random seed changes are intentional.\n"
                "        - Check whether metric changes are explained in markdown or the PR description.\n"
            ),
        },
    )
    context = _context(is_fork=False)
    inputs = ActionInputs(ai_provider="none", ai_api_key=None, redact_secrets=True, redact_emails=True)

    result, sync_result = _run_and_sync(github_api=api, context=context, inputs=inputs)

    assert result.status == "review_ready"
    assert sync_result.action == "created"
    assert result.review_result is not None
    assert [item.notebook_path for item in result.review_result.reviewer_guidance] == [
        "notebooks/training/churn_model.ipynb",
        "notebooks/training/churn_model.ipynb",
        "notebooks/training/churn_model.ipynb",
    ]
    assert [item.message for item in result.review_result.reviewer_guidance] == [
        (
            "Inspect changed outputs for unexplained metric shifts, stale rendered "
            "results, or outputs that now need updated narrative."
        ),
        "Verify the dataset split and random seed changes are intentional.",
        "Check whether metric changes are explained in markdown or the PR description.",
    ]
    assert [item.source for item in result.review_result.reviewer_guidance] == [
        "built_in",
        "playbook",
        "playbook",
    ]
    body = api.comments[0].body
    assert "#### `notebooks/training/churn_model.ipynb` (`modified`)" in body
    assert "##### Reviewer Guidance" in body
    assert (
        "- Inspect changed outputs for unexplained metric shifts, stale rendered results, "
        "or outputs that now need updated narrative."
    ) in body
    assert (
        "- Training notebooks: Verify the dataset split and random seed changes are intentional."
    ) in body
    assert (
        "- Training notebooks: Check whether metric changes are explained in markdown or the PR description."
    ) in body


def test_renamed_notebook_matches_playbook_using_head_path() -> None:
    api = InMemoryGitHubApiClient(
        pr_files=_renamed_reviewer_guidance_training_files(),
        contents_by_ref={
            **_contents_for_renamed_reviewer_guidance_training_notebook(),
            (".github/notebooklens.yml", "head-sha"): (
                "version: 1\n"
                "reviewer_guidance:\n"
                "  playbooks:\n"
                "    - name: Training notebooks\n"
                "      paths:\n"
                "        - notebooks/training/**/*.ipynb\n"
                "      prompts:\n"
                "        - Verify the dataset split and random seed changes are intentional.\n"
            ),
        },
    )
    context = _context(is_fork=False)
    inputs = ActionInputs(ai_provider="none", ai_api_key=None, redact_secrets=True, redact_emails=True)

    result, sync_result = _run_and_sync(github_api=api, context=context, inputs=inputs)

    assert result.status == "review_ready"
    assert sync_result.action == "created"
    assert result.changed_notebook_paths == ["notebooks/training/churn_model.ipynb"]
    assert [item.notebook_path for item in result.review_result.reviewer_guidance] == [
        "notebooks/training/churn_model.ipynb",
        "notebooks/training/churn_model.ipynb",
    ]
    assert (
        "acme/notebooklens-fixture",
        "analysis/churn_model.ipynb",
        "base-sha",
    ) in api.content_requests
    assert (
        "acme/notebooklens-fixture",
        "notebooks/training/churn_model.ipynb",
        "head-sha",
    ) in api.content_requests
    body = api.comments[0].body
    assert "#### `notebooks/training/churn_model.ipynb` (`modified`)" in body
    assert "#### `analysis/churn_model.ipynb`" not in body
    assert (
        "- Training notebooks: Verify the dataset split and random seed changes are intentional."
        in body
    )


def test_malformed_training_config_warns_once_and_skips_playbook_prompts() -> None:
    api = InMemoryGitHubApiClient(
        pr_files=_reviewer_guidance_training_files(),
        contents_by_ref={
            **_contents_for_reviewer_guidance_training_notebook(),
            (".github/notebooklens.yml", "head-sha"): (
                "version: 1\n"
                "reviewer_guidance: []\n"
            ),
        },
    )
    context = _context(is_fork=False)
    inputs = ActionInputs(ai_provider="none", ai_api_key=None, redact_secrets=True, redact_emails=True)

    result, sync_result = _run_and_sync(github_api=api, context=context, inputs=inputs)

    assert result.status == "review_ready"
    assert sync_result.action == "created"
    assert result.config is None
    assert len(result.config_notices) == 1
    assert result.review_result is not None
    assert [item.source for item in result.review_result.reviewer_guidance] == ["built_in"]
    body = api.comments[0].body
    assert body.count(".github/notebooklens.yml") == 1
    assert "Continuing with built-in guidance only." in body
    assert "Training notebooks:" not in body


def test_stale_owned_marker_comment_is_deleted_when_notebook_changes_disappear() -> None:
    stale_bot_comment = _seed_comment(
        comment_id=10,
        body=f"{NOTEBOOKLENS_COMMENT_MARKER}\nold review body",
        author_login="github-actions[bot]",
        author_type="Bot",
        updated_at="2026-04-10T00:00:10Z",
    )
    unrelated_user_comment = _seed_comment(
        comment_id=11,
        body=f"{NOTEBOOKLENS_COMMENT_MARKER}\nuser marker that must remain",
        author_login="alice",
        author_type="User",
        updated_at="2026-04-10T00:00:11Z",
    )
    api = InMemoryGitHubApiClient(
        pr_files=_readme_only_files(),
        contents_by_ref={},
        comments=[stale_bot_comment, unrelated_user_comment],
    )
    context = _context(is_fork=False)
    inputs = ActionInputs(ai_provider="none", ai_api_key=None, redact_secrets=True, redact_emails=True)

    result = run_action(
        github_api=api,
        context=context,
        inputs=inputs,
        provider_factory=NoneProviderFactory(),
        emit_logs=False,
    )
    sync_result = sync_review_comment(
        github_api=api,
        repository=context.repository,
        pull_number=context.pull_number,
        has_notebook_changes=bool(result.changed_notebook_paths),
        notebook_diff=result.notebook_diff,
        review_result=result.review_result,
        claude_succeeded=claude_succeeded_from_metadata(result.metadata),
        notices=result.notices,
    )

    assert result.status == "no_notebook_changes"
    assert sync_result.action == "deleted"
    assert sync_result.deleted_comment_ids == [10]
    assert len(api.comments) == 1
    assert api.comments[0].comment_id == 11


def test_only_owned_marker_comment_is_updated() -> None:
    user_marker = _seed_comment(
        comment_id=20,
        body=f"{NOTEBOOKLENS_COMMENT_MARKER}\nuser marker stays untouched",
        author_login="bob",
        author_type="User",
        updated_at="2026-04-10T00:00:20Z",
    )
    bot_without_marker = _seed_comment(
        comment_id=21,
        body="bot comment without marker",
        author_login="github-actions[bot]",
        author_type="Bot",
        updated_at="2026-04-10T00:00:21Z",
    )
    owned_marker = _seed_comment(
        comment_id=22,
        body=f"{NOTEBOOKLENS_COMMENT_MARKER}\nold bot review body",
        author_login="github-actions[bot]",
        author_type="Bot",
        updated_at="2026-04-10T00:00:22Z",
    )
    api = InMemoryGitHubApiClient(
        pr_files=_modified_notebook_files(),
        contents_by_ref=_contents_for_modified_notebook(),
        comments=[user_marker, bot_without_marker, owned_marker],
    )
    context = _context(is_fork=False)
    inputs = ActionInputs(ai_provider="none", ai_api_key=None, redact_secrets=True, redact_emails=True)

    result, sync_result = _run_and_sync(github_api=api, context=context, inputs=inputs)

    assert result.status == "review_ready"
    assert sync_result.action == "updated"
    assert sync_result.comment_id == 22
    assert 22 in api.updated_bodies
    assert api.updated_bodies[22].startswith(NOTEBOOKLENS_COMMENT_MARKER)

    comment_map = {item.comment_id: item for item in api.comments}
    assert comment_map[20].body == user_marker.body
    assert comment_map[21].body == bot_without_marker.body
    assert "## NotebookLens" in comment_map[22].body


def test_run_action_from_env_executes_full_runtime_and_upserts_comment() -> None:
    api = InMemoryGitHubApiClient(
        pr_files=_modified_notebook_files(),
        contents_by_ref=_contents_for_modified_notebook(),
    )
    env = {
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_REPOSITORY": "acme/notebooklens-fixture",
        "INPUT_AI_PROVIDER": "none",
        "INPUT_REDACT_SECRETS": "true",
        "INPUT_REDACT_EMAILS": "true",
    }
    event_payload = {
        "action": "opened",
        "number": 42,
        "pull_request": {
            "base": {"sha": "base-sha"},
            "head": {"sha": "head-sha", "repo": {"fork": False, "full_name": "acme/notebooklens-fixture"}},
        },
    }

    result, sync_result = run_action_from_env(
        env=env,
        event_payload=event_payload,
        github_api=api,
        provider_factory=NoneProviderFactory(),
        emit_logs=False,
    )

    assert result.status == "review_ready"
    assert sync_result is not None
    assert sync_result.action == "created"
    assert len(api.comments) == 1
    assert "## NotebookLens" in api.comments[0].body


def test_run_action_from_env_writes_outputs_for_review_ready(tmp_path: Path) -> None:
    api = InMemoryGitHubApiClient(
        pr_files=_modified_notebook_files(),
        contents_by_ref=_contents_for_modified_notebook(),
    )
    output_path = tmp_path / "github-output.txt"
    env = {
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_REPOSITORY": "acme/notebooklens-fixture",
        "GITHUB_OUTPUT": str(output_path),
        "INPUT_AI_PROVIDER": "none",
        "INPUT_REDACT_SECRETS": "true",
        "INPUT_REDACT_EMAILS": "true",
    }
    event_payload = {
        "action": "opened",
        "number": 42,
        "pull_request": {
            "base": {"sha": "base-sha"},
            "head": {"sha": "head-sha", "repo": {"fork": False, "full_name": "acme/notebooklens-fixture"}},
        },
    }

    result, sync_result = run_action_from_env(
        env=env,
        event_payload=event_payload,
        github_api=api,
        provider_factory=NoneProviderFactory(),
        emit_logs=False,
    )

    outputs = parse_github_output(output_path)
    assert result.status == "review_ready"
    assert sync_result is not None
    assert outputs == {
        "effective-provider": "none",
        "changed-notebooks": "1",
        "total-cells-changed": str(result.notebook_diff.total_cells_changed),
        "fallback-reason": "",
    }


def test_run_action_from_env_writes_outputs_for_no_notebook_changes(tmp_path: Path) -> None:
    api = InMemoryGitHubApiClient(
        pr_files=_readme_only_files(),
        contents_by_ref={},
    )
    output_path = tmp_path / "github-output.txt"
    env = {
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_REPOSITORY": "acme/notebooklens-fixture",
        "GITHUB_OUTPUT": str(output_path),
        "INPUT_AI_PROVIDER": "none",
    }
    event_payload = {
        "action": "opened",
        "number": 42,
        "pull_request": {
            "base": {"sha": "base-sha"},
            "head": {"sha": "head-sha", "repo": {"fork": False, "full_name": "acme/notebooklens-fixture"}},
        },
    }

    result, sync_result = run_action_from_env(
        env=env,
        event_payload=event_payload,
        github_api=api,
        provider_factory=NoneProviderFactory(),
        emit_logs=False,
    )

    outputs = parse_github_output(output_path)
    assert result.status == "no_notebook_changes"
    assert sync_result is not None
    assert outputs == {
        "effective-provider": "none",
        "changed-notebooks": "0",
        "total-cells-changed": "0",
        "fallback-reason": "",
    }


def test_run_action_from_env_writes_outputs_for_unsupported_event(tmp_path: Path) -> None:
    api = InMemoryGitHubApiClient(
        pr_files=_modified_notebook_files(),
        contents_by_ref=_contents_for_modified_notebook(),
    )
    output_path = tmp_path / "github-output.txt"
    env = {
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_REPOSITORY": "acme/notebooklens-fixture",
        "GITHUB_OUTPUT": str(output_path),
        "INPUT_AI_PROVIDER": "claude",
        "INPUT_AI_API_KEY": "dummy-key",
    }
    event_payload = {
        "action": "closed",
        "number": 42,
        "pull_request": {
            "base": {"sha": "base-sha"},
            "head": {"sha": "head-sha", "repo": {"fork": False, "full_name": "acme/notebooklens-fixture"}},
        },
    }

    result, sync_result = run_action_from_env(
        env=env,
        event_payload=event_payload,
        github_api=api,
        provider_factory=NoneProviderFactory(),
        emit_logs=False,
    )

    outputs = parse_github_output(output_path)
    assert result.status == "unsupported_event"
    assert sync_result is None
    assert outputs == {
        "effective-provider": "none",
        "changed-notebooks": "0",
        "total-cells-changed": "0",
        "fallback-reason": "",
    }


def test_run_action_from_env_writes_outputs_for_fallback_case(tmp_path: Path) -> None:
    api = InMemoryGitHubApiClient(
        pr_files=_modified_notebook_files(),
        contents_by_ref=_contents_for_modified_notebook(),
    )
    output_path = tmp_path / "github-output.txt"
    env = {
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_REPOSITORY": "acme/notebooklens-fixture",
        "GITHUB_OUTPUT": str(output_path),
        "INPUT_AI_PROVIDER": "claude",
        "INPUT_AI_API_KEY": "dummy-key",
    }
    event_payload = {
        "action": "opened",
        "number": 42,
        "pull_request": {
            "base": {"sha": "base-sha"},
            "head": {"sha": "head-sha", "repo": {"fork": False, "full_name": "acme/notebooklens-fixture"}},
        },
    }

    result, sync_result = run_action_from_env(
        env=env,
        event_payload=event_payload,
        github_api=api,
        provider_factory=FailingProviderFactory(),
        emit_logs=False,
    )

    outputs = parse_github_output(output_path)
    assert result.status == "review_ready"
    assert sync_result is not None
    assert outputs["effective-provider"] == "none"
    assert outputs["changed-notebooks"] == "1"
    assert outputs["total-cells-changed"] == str(result.notebook_diff.total_cells_changed)
    assert "simulated provider failure" in outputs["fallback-reason"]


def test_run_action_from_env_unsupported_event_does_not_sync_comments() -> None:
    existing = _seed_comment(
        comment_id=30,
        body=f"{NOTEBOOKLENS_COMMENT_MARKER}\nexisting comment body",
        author_login="github-actions[bot]",
        author_type="Bot",
        updated_at="2026-04-10T00:00:30Z",
    )
    api = InMemoryGitHubApiClient(
        pr_files=_modified_notebook_files(),
        contents_by_ref=_contents_for_modified_notebook(),
        comments=[existing],
    )
    env = {
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_REPOSITORY": "acme/notebooklens-fixture",
        "INPUT_AI_PROVIDER": "none",
    }
    event_payload = {
        "action": "closed",
        "number": 42,
        "pull_request": {
            "base": {"sha": "base-sha"},
            "head": {"sha": "head-sha", "repo": {"fork": False, "full_name": "acme/notebooklens-fixture"}},
        },
    }

    result, sync_result = run_action_from_env(
        env=env,
        event_payload=event_payload,
        github_api=api,
        provider_factory=NoneProviderFactory(),
        emit_logs=False,
    )

    assert result.status == "unsupported_event"
    assert sync_result is None
    assert api.comments == [existing]
    assert api.created_bodies == []
    assert api.updated_bodies == {}
    assert api.deleted_comment_ids == []


def test_main_uses_env_runtime_wiring_and_deletes_stale_marker_when_no_notebooks(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    stale_comment = _seed_comment(
        comment_id=40,
        body=f"{NOTEBOOKLENS_COMMENT_MARKER}\nstale review",
        author_login="github-actions[bot]",
        author_type="Bot",
        updated_at="2026-04-10T00:00:40Z",
    )
    api = InMemoryGitHubApiClient(
        pr_files=_readme_only_files(),
        contents_by_ref={},
        comments=[stale_comment],
    )

    payload_path = tmp_path / "event.json"
    payload = {
        "action": "synchronize",
        "number": 42,
        "pull_request": {
            "base": {"sha": "base-sha"},
            "head": {"sha": "head-sha", "repo": {"fork": False, "full_name": "acme/notebooklens-fixture"}},
        },
    }
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/notebooklens-fixture")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(payload_path))
    monkeypatch.setenv("INPUT_AI_PROVIDER", "none")
    monkeypatch.setattr("src.github_action.GitHubApiClient.from_env", lambda env=None: api)

    exit_code = main()

    assert exit_code == 0
    assert api.deleted_comment_ids == [40]
    assert api.comments == []
