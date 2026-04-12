"""Managed snapshot orchestration for PR webhook ingestion and worker execution."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Mapping
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from src.claude_integration import NoneProvider
from src.diff_engine import DiffLimits, NotebookInput
from src.review_core import REVIEW_SNAPSHOT_SCHEMA_VERSION, ReviewCoreRequest, build_review_artifacts

from .config import ApiSettings
from .job_runner import (
    claim_next_snapshot_build_job,
    enqueue_snapshot_build_job,
    mark_snapshot_build_job_failed,
    mark_snapshot_build_job_succeeded,
)
from .managed_github import MANAGED_REVIEW_CHECK_RUN_NAME, ManagedGitHubClient
from .models import (
    GitHubInstallation,
    InstallationAccountType,
    InstallationRepository,
    ManagedReview,
    ManagedReviewStatus,
    ReviewSnapshot,
    ReviewSnapshotStatus,
    SnapshotBuildJob,
    SnapshotBuildJobStatus,
)
from .review_workspace import ThreadCounts, carry_forward_open_threads
from .reviewer_guidance import (
    NotebookLensConfigError,
    ReviewerPlaybook,
    build_reviewer_guidance,
    parse_reviewer_playbooks,
)


SUPPORTED_PULL_REQUEST_ACTIONS = {"opened", "reopened", "synchronize"}
_CONFIG_PATH = ".github/notebooklens.yml"


class ManagedWebhookPayloadError(ValueError):
    """Raised when a GitHub webhook payload is missing required PR fields."""


@dataclass(frozen=True)
class PullRequestWebhook:
    """Normalized PR webhook payload used for managed review orchestration."""

    action: str
    installation_id: int
    account_login: str
    account_type: InstallationAccountType
    owner: str
    repo: str
    full_name: str
    private: bool
    pull_number: int
    pull_author_github_user_id: int | None
    pull_author_login: str | None
    base_branch: str
    base_sha: str
    head_sha: str


@dataclass(frozen=True)
class WebhookIngestionResult:
    """Structured webhook-ingestion result for route responses and tests."""

    accepted: bool
    reason: str
    action: str | None
    managed_review_id: uuid.UUID | None
    job_id: uuid.UUID | None
    check_run_id: int | None


@dataclass(frozen=True)
class SnapshotBuildResult:
    """Structured worker result for one claimed snapshot build job."""

    status: str
    job_id: uuid.UUID | None
    managed_review_id: uuid.UUID | None
    snapshot_id: uuid.UUID | None
    snapshot_index: int | None
    check_run_id: int | None
    reused_snapshot: bool = False


def ingest_pull_request_webhook(
    *,
    db_session: Session,
    settings: ApiSettings,
    github_client: ManagedGitHubClient,
    github_event: str | None,
    payload: Mapping[str, Any],
) -> WebhookIngestionResult:
    """Ingest a supported PR webhook into managed review + snapshot job state."""
    webhook = parse_pull_request_webhook(github_event=github_event, payload=payload)
    if webhook is None:
        return WebhookIngestionResult(
            accepted=False,
            reason="Ignored non-managed GitHub event",
            action=None,
            managed_review_id=None,
            job_id=None,
            check_run_id=None,
        )

    installation, repository, review, reusable_check_run_id = _upsert_review_state(
        db_session=db_session,
        webhook=webhook,
    )

    current_ready_snapshot = db_session.execute(
        select(ReviewSnapshot)
        .where(
            ReviewSnapshot.managed_review_id == review.id,
            ReviewSnapshot.base_sha == webhook.base_sha,
            ReviewSnapshot.head_sha == webhook.head_sha,
            ReviewSnapshot.status == ReviewSnapshotStatus.READY,
        )
        .order_by(ReviewSnapshot.snapshot_index.desc())
        .limit(1)
    ).scalar_one_or_none()

    details_url = _build_review_url(
        settings=settings,
        review=review,
        snapshot_index=current_ready_snapshot.snapshot_index if current_ready_snapshot else None,
    )

    job = _find_active_snapshot_job(
        db_session=db_session,
        managed_review_id=review.id,
        base_sha=webhook.base_sha,
        head_sha=webhook.head_sha,
    )

    if current_ready_snapshot is not None and job is None:
        review.status = ManagedReviewStatus.READY
        review.latest_snapshot_id = current_ready_snapshot.id
        check_run = github_client.create_or_update_check_run(
            settings=settings,
            installation_id=installation.github_installation_id,
            repository=repository.full_name,
            head_sha=webhook.head_sha,
            status="completed",
            conclusion="neutral",
            details_url=details_url,
            external_id=str(review.id),
            summary=_render_check_run_summary(
                review_url=details_url,
                snapshot_status="ready",
                activity=(
                    f"Existing snapshot v{current_ready_snapshot.snapshot_index} already matches "
                    "the latest push."
                ),
                thread_counts=_thread_counts(),
            ),
            check_run_id=reusable_check_run_id,
        )
        review.latest_check_run_id = check_run.check_run_id
        db_session.flush()
        return WebhookIngestionResult(
            accepted=True,
            reason="Reused existing ready snapshot for the latest push",
            action=webhook.action,
            managed_review_id=review.id,
            job_id=None,
            check_run_id=check_run.check_run_id,
        )

    check_run = github_client.create_or_update_check_run(
        settings=settings,
        installation_id=installation.github_installation_id,
        repository=repository.full_name,
        head_sha=webhook.head_sha,
        status="in_progress",
        details_url=details_url,
        external_id=str(review.id),
        summary=_render_check_run_summary(
            review_url=details_url,
            snapshot_status="pending",
            activity="Snapshot queued for the latest push.",
            thread_counts=_thread_counts(),
        ),
        check_run_id=reusable_check_run_id,
    )
    review.latest_check_run_id = check_run.check_run_id
    review.status = ManagedReviewStatus.PENDING

    if job is None:
        job = enqueue_snapshot_build_job(
            db_session,
            managed_review_id=review.id,
            base_sha=webhook.base_sha,
            head_sha=webhook.head_sha,
        )

    db_session.flush()
    return WebhookIngestionResult(
        accepted=True,
        reason="Queued managed snapshot build",
        action=webhook.action,
        managed_review_id=review.id,
        job_id=job.id,
        check_run_id=check_run.check_run_id,
    )


def run_snapshot_build_worker_once(
    *,
    settings: ApiSettings,
    db_session: Session,
    github_client: ManagedGitHubClient,
    limits: DiffLimits = DiffLimits(),
    now: datetime | None = None,
) -> SnapshotBuildResult:
    """Claim and process a single managed snapshot build job."""
    job = claim_next_snapshot_build_job(db_session, now=now)
    if job is None:
        return SnapshotBuildResult(
            status="idle",
            job_id=None,
            managed_review_id=None,
            snapshot_id=None,
            snapshot_index=None,
            check_run_id=None,
        )

    review = db_session.execute(
        select(ManagedReview)
        .options(
            selectinload(ManagedReview.installation_repository).selectinload(
                InstallationRepository.installation
            )
        )
        .where(ManagedReview.id == job.managed_review_id)
    ).scalar_one()
    repository = review.installation_repository
    installation = repository.installation

    existing_ready_snapshot = db_session.execute(
        select(ReviewSnapshot)
        .where(
            ReviewSnapshot.managed_review_id == review.id,
            ReviewSnapshot.base_sha == job.base_sha,
            ReviewSnapshot.head_sha == job.head_sha,
            ReviewSnapshot.status == ReviewSnapshotStatus.READY,
        )
        .order_by(ReviewSnapshot.snapshot_index.desc())
        .limit(1)
    ).scalar_one_or_none()
    if existing_ready_snapshot is not None:
        if _job_matches_latest_review(review=review, job=job):
            review.status = ManagedReviewStatus.READY
            review.latest_snapshot_id = existing_ready_snapshot.id
            check_run_id = _update_check_run_for_snapshot(
                settings=settings,
                github_client=github_client,
                review=review,
                repository=repository,
                installation_id=installation.github_installation_id,
                snapshot=existing_ready_snapshot,
                activity=(
                    f"Existing snapshot v{existing_ready_snapshot.snapshot_index} already matches "
                    f"head `{_short_sha(job.head_sha)}`."
                ),
            )
        else:
            check_run_id = review.latest_check_run_id
        mark_snapshot_build_job_succeeded(db_session, job)
        db_session.flush()
        return SnapshotBuildResult(
            status="reused",
            job_id=job.id,
            managed_review_id=review.id,
            snapshot_id=existing_ready_snapshot.id,
            snapshot_index=existing_ready_snapshot.snapshot_index,
            check_run_id=check_run_id,
            reused_snapshot=True,
        )

    snapshot = _create_pending_snapshot(db_session=db_session, review=review, job=job)
    try:
        notebook_inputs = _build_notebook_inputs_for_review(
            settings=settings,
            github_client=github_client,
            installation_id=installation.github_installation_id,
            repository_full_name=repository.full_name,
            pull_number=review.pull_number,
            base_sha=job.base_sha,
            head_sha=job.head_sha,
            limits=limits,
        )
        config_text = github_client.get_file_content(
            settings=settings,
            installation_id=installation.github_installation_id,
            repository=repository.full_name,
            path=_CONFIG_PATH,
            ref=job.head_sha,
        )
        playbooks, guidance_notices = _load_reviewer_playbooks(config_text)
        review_artifacts = build_review_artifacts(
            ReviewCoreRequest(
                notebook_inputs=notebook_inputs,
                reviewer=NoneProvider(),
                limits=limits,
            )
        )
        snapshot_payload = review_artifacts.snapshot_payload
        if guidance_notices:
            snapshot_payload = {
                **snapshot_payload,
                "review": {
                    **snapshot_payload["review"],
                    "notices": [
                        *snapshot_payload["review"].get("notices", []),
                        *guidance_notices,
                    ],
                },
            }
        reviewer_guidance = build_reviewer_guidance(
            review_artifacts.notebook_diff,
            playbooks=playbooks,
        )

        snapshot.status = ReviewSnapshotStatus.READY
        snapshot.schema_version = int(snapshot_payload["schema_version"])
        snapshot.summary_text = review_artifacts.review_result.summary
        snapshot.flagged_findings_json = [
            asdict(issue) for issue in review_artifacts.review_result.flagged_issues
        ]
        snapshot.reviewer_guidance_json = reviewer_guidance
        snapshot.snapshot_payload_json = snapshot_payload
        snapshot.notebook_count = review_artifacts.notebook_diff.total_notebooks_changed
        snapshot.changed_cell_count = review_artifacts.notebook_diff.total_cells_changed
        snapshot.failure_reason = None

        if _job_matches_latest_review(review=review, job=job):
            carry_forward_open_threads(
                db_session=db_session,
                review=review,
                snapshot=snapshot,
            )
            review.status = ManagedReviewStatus.READY
            review.latest_snapshot_id = snapshot.id
            details_url = _build_review_url(
                settings=settings,
                review=review,
                snapshot_index=snapshot.snapshot_index,
            )
            review.latest_check_run_id = github_client.create_or_update_check_run(
                settings=settings,
                installation_id=installation.github_installation_id,
                repository=repository.full_name,
                head_sha=job.head_sha,
                status="completed",
                conclusion="neutral",
                details_url=details_url,
                external_id=str(review.id),
                summary=_render_check_run_summary(
                    review_url=details_url,
                    snapshot_status="ready",
                    activity=(
                        f"Snapshot v{snapshot.snapshot_index} built for head "
                        f"`{_short_sha(job.head_sha)}`."
                    ),
                    thread_counts=_thread_counts(),
                ),
                check_run_id=review.latest_check_run_id,
            ).check_run_id
        mark_snapshot_build_job_succeeded(db_session, job)
        db_session.flush()
        return SnapshotBuildResult(
            status="succeeded",
            job_id=job.id,
            managed_review_id=review.id,
            snapshot_id=snapshot.id,
            snapshot_index=snapshot.snapshot_index,
            check_run_id=review.latest_check_run_id,
        )
    except Exception as exc:
        failure_reason = _truncate_error(exc)
        snapshot.status = ReviewSnapshotStatus.FAILED
        snapshot.failure_reason = failure_reason
        snapshot.summary_text = None
        snapshot.flagged_findings_json = []
        snapshot.reviewer_guidance_json = []
        snapshot.snapshot_payload_json = {
            "schema_version": REVIEW_SNAPSHOT_SCHEMA_VERSION,
            "review": {
                "notices": [],
                "notebooks": [],
            },
        }
        snapshot.notebook_count = 0
        snapshot.changed_cell_count = 0
        if _job_matches_latest_review(review=review, job=job):
            review.status = ManagedReviewStatus.FAILED
            details_url = _build_review_url(settings=settings, review=review)
            review.latest_check_run_id = github_client.create_or_update_check_run(
                settings=settings,
                installation_id=installation.github_installation_id,
                repository=repository.full_name,
                head_sha=job.head_sha,
                status="completed",
                conclusion="action_required",
                details_url=details_url,
                external_id=str(review.id),
                summary=_render_check_run_summary(
                    review_url=details_url,
                    snapshot_status="failed",
                    activity=(
                        f"Snapshot build failed for head `{_short_sha(job.head_sha)}`."
                    ),
                    thread_counts=_thread_counts(),
                    failure_reason=failure_reason,
                ),
                check_run_id=review.latest_check_run_id,
            ).check_run_id
        mark_snapshot_build_job_failed(
            db_session,
            job,
            error_message=failure_reason,
        )
        db_session.flush()
        return SnapshotBuildResult(
            status="failed",
            job_id=job.id,
            managed_review_id=review.id,
            snapshot_id=snapshot.id,
            snapshot_index=snapshot.snapshot_index,
            check_run_id=review.latest_check_run_id,
        )


def parse_pull_request_webhook(
    *,
    github_event: str | None,
    payload: Mapping[str, Any],
) -> PullRequestWebhook | None:
    """Parse a supported GitHub pull_request webhook payload, or ignore it."""
    event_name = (github_event or "").strip()
    action = str(payload.get("action", "")).strip()
    if event_name != "pull_request" or action not in SUPPORTED_PULL_REQUEST_ACTIONS:
        return None

    installation = _require_mapping(payload, "installation")
    repository = _require_mapping(payload, "repository")
    pull_request = _require_mapping(payload, "pull_request")
    base = _require_mapping(pull_request, "base")
    head = _require_mapping(pull_request, "head")

    installation_id = _require_int(installation, "id")
    account = installation.get("account") or repository.get("owner")
    account_login = _require_str(account, "login")
    account_type_raw = _require_str(account, "type").lower()
    if account_type_raw == "organization":
        account_type = InstallationAccountType.ORGANIZATION
    elif account_type_raw == "user":
        account_type = InstallationAccountType.USER
    else:
        raise ManagedWebhookPayloadError(f"Unsupported GitHub installation account type: {account_type_raw}")

    owner = _require_str(repository.get("owner"), "login")
    repo = _require_str(repository, "name")
    full_name = _require_str(repository, "full_name")

    return PullRequestWebhook(
        action=action,
        installation_id=installation_id,
        account_login=account_login,
        account_type=account_type,
        owner=owner,
        repo=repo,
        full_name=full_name,
        private=bool(repository.get("private", False)),
        pull_number=_require_int(payload, "number"),
        pull_author_github_user_id=_optional_int(pull_request.get("user"), "id"),
        pull_author_login=_optional_str(pull_request.get("user"), "login"),
        base_branch=_require_str(base, "ref"),
        base_sha=_require_str(base, "sha"),
        head_sha=_require_str(head, "sha"),
    )


def _upsert_review_state(
    *,
    db_session: Session,
    webhook: PullRequestWebhook,
) -> tuple[GitHubInstallation, InstallationRepository, ManagedReview, int | None]:
    installation = db_session.execute(
        select(GitHubInstallation).where(
            GitHubInstallation.github_installation_id == webhook.installation_id
        )
    ).scalar_one_or_none()
    if installation is None:
        installation = GitHubInstallation(
            github_installation_id=webhook.installation_id,
            account_login=webhook.account_login,
            account_type=webhook.account_type,
        )
        db_session.add(installation)
        db_session.flush()
    else:
        installation.account_login = webhook.account_login
        installation.account_type = webhook.account_type

    repository = db_session.execute(
        select(InstallationRepository).where(
            InstallationRepository.installation_id == installation.id,
            InstallationRepository.full_name == webhook.full_name,
        )
    ).scalar_one_or_none()
    if repository is None:
        repository = InstallationRepository(
            installation_id=installation.id,
            owner=webhook.owner,
            name=webhook.repo,
            full_name=webhook.full_name,
            private=webhook.private,
            active=True,
        )
        db_session.add(repository)
        db_session.flush()
    else:
        repository.owner = webhook.owner
        repository.name = webhook.repo
        repository.private = webhook.private
        repository.active = True

    review = db_session.execute(
        select(ManagedReview).where(
            ManagedReview.installation_repository_id == repository.id,
            ManagedReview.pull_number == webhook.pull_number,
        )
    ).scalar_one_or_none()
    reusable_check_run_id: int | None = None
    if review is None:
        review = ManagedReview(
            installation_repository_id=repository.id,
            owner=webhook.owner,
            repo=webhook.repo,
            pull_number=webhook.pull_number,
            pull_author_github_user_id=webhook.pull_author_github_user_id,
            pull_author_login=webhook.pull_author_login,
            base_branch=webhook.base_branch,
            latest_base_sha=webhook.base_sha,
            latest_head_sha=webhook.head_sha,
            status=ManagedReviewStatus.PENDING,
        )
        db_session.add(review)
        db_session.flush()
    else:
        reusable_check_run_id = (
            review.latest_check_run_id if review.latest_head_sha == webhook.head_sha else None
        )
        review.owner = webhook.owner
        review.repo = webhook.repo
        review.base_branch = webhook.base_branch
        review.pull_author_github_user_id = webhook.pull_author_github_user_id
        review.pull_author_login = webhook.pull_author_login
        review.latest_base_sha = webhook.base_sha
        review.latest_head_sha = webhook.head_sha
        review.status = ManagedReviewStatus.PENDING

    return installation, repository, review, reusable_check_run_id


def _find_active_snapshot_job(
    *,
    db_session: Session,
    managed_review_id: uuid.UUID,
    base_sha: str,
    head_sha: str,
) -> SnapshotBuildJob | None:
    return db_session.execute(
        select(SnapshotBuildJob)
        .where(
            SnapshotBuildJob.managed_review_id == managed_review_id,
            SnapshotBuildJob.base_sha == base_sha,
            SnapshotBuildJob.head_sha == head_sha,
            SnapshotBuildJob.status.in_(
                (
                    SnapshotBuildJobStatus.QUEUED,
                    SnapshotBuildJobStatus.RUNNING,
                    SnapshotBuildJobStatus.RETRYABLE_FAILED,
                )
            ),
        )
        .order_by(SnapshotBuildJob.scheduled_at.asc())
        .limit(1)
    ).scalar_one_or_none()


def _build_notebook_inputs_for_review(
    *,
    settings: ApiSettings,
    github_client: ManagedGitHubClient,
    installation_id: int,
    repository_full_name: str,
    pull_number: int,
    base_sha: str,
    head_sha: str,
    limits: DiffLimits,
) -> list[NotebookInput]:
    raw_files = github_client.list_pull_request_files(
        settings=settings,
        installation_id=installation_id,
        repository=repository_full_name,
        pull_number=pull_number,
    )
    inputs: list[NotebookInput] = []
    notebook_count = 0

    for raw_file in raw_files:
        selection = _coerce_notebook_file(raw_file)
        if selection is None:
            continue
        should_fetch_content = notebook_count < limits.max_notebooks_per_pr
        notebook_count += 1

        skip_for_size = (
            selection["head_size_bytes"] is not None
            and selection["head_size_bytes"] > limits.max_notebook_bytes
        )
        base_content = None
        head_content = None

        if should_fetch_content and not skip_for_size and selection["base_path"] is not None:
            base_content = github_client.get_file_content(
                settings=settings,
                installation_id=installation_id,
                repository=repository_full_name,
                path=str(selection["base_path"]),
                ref=base_sha,
            )
        if should_fetch_content and not skip_for_size and selection["head_path"] is not None:
            head_content = github_client.get_file_content(
                settings=settings,
                installation_id=installation_id,
                repository=repository_full_name,
                path=str(selection["head_path"]),
                ref=head_sha,
            )

        inputs.append(
            NotebookInput(
                path=str(selection["path"]),
                change_type=str(selection["change_type"]),
                base_content=base_content,
                head_content=head_content,
                head_size_bytes=selection["head_size_bytes"],
            )
        )

    return inputs


def _coerce_notebook_file(raw_file: Any) -> dict[str, Any] | None:
    if isinstance(raw_file, Mapping):
        payload = raw_file
    else:
        payload = {
            "filename": getattr(raw_file, "filename", getattr(raw_file, "path", None)),
            "status": getattr(raw_file, "status", None),
            "previous_filename": getattr(
                raw_file,
                "previous_filename",
                getattr(raw_file, "previous_path", None),
            ),
            "size": getattr(raw_file, "size", getattr(raw_file, "size_bytes", None)),
        }

    filename = payload.get("filename", payload.get("path"))
    status = str(payload.get("status", "")).strip().lower()
    previous_filename = payload.get("previous_filename", payload.get("previous_path"))
    size = payload.get("size", payload.get("size_bytes"))
    size_bytes = size if isinstance(size, int) else None
    current_is_notebook = isinstance(filename, str) and filename.lower().endswith(".ipynb")
    previous_is_notebook = isinstance(previous_filename, str) and previous_filename.lower().endswith(
        ".ipynb"
    )

    if not current_is_notebook and not previous_is_notebook:
        return None
    if status in {"removed", "deleted"}:
        if not current_is_notebook:
            return None
        return {
            "path": filename,
            "change_type": "deleted",
            "base_path": filename,
            "head_path": None,
            "head_size_bytes": size_bytes,
        }
    if status == "added":
        if not current_is_notebook:
            return None
        return {
            "path": filename,
            "change_type": "added",
            "base_path": None,
            "head_path": filename,
            "head_size_bytes": size_bytes,
        }
    if status == "renamed":
        if current_is_notebook and previous_is_notebook:
            return {
                "path": filename,
                "change_type": "modified",
                "base_path": previous_filename,
                "head_path": filename,
                "head_size_bytes": size_bytes,
            }
        if previous_is_notebook and not current_is_notebook:
            return {
                "path": previous_filename,
                "change_type": "deleted",
                "base_path": previous_filename,
                "head_path": None,
                "head_size_bytes": size_bytes,
            }
        if current_is_notebook:
            return {
                "path": filename,
                "change_type": "added",
                "base_path": None,
                "head_path": filename,
                "head_size_bytes": size_bytes,
            }
        return None
    if not current_is_notebook:
        return None
    return {
        "path": filename,
        "change_type": "modified",
        "base_path": filename,
        "head_path": filename,
        "head_size_bytes": size_bytes,
    }


def _create_pending_snapshot(
    *,
    db_session: Session,
    review: ManagedReview,
    job: SnapshotBuildJob,
) -> ReviewSnapshot:
    next_index = (
        db_session.execute(
            select(func.max(ReviewSnapshot.snapshot_index)).where(
                ReviewSnapshot.managed_review_id == review.id
            )
        ).scalar_one()
        or 0
    ) + 1
    snapshot = ReviewSnapshot(
        managed_review_id=review.id,
        base_sha=job.base_sha,
        head_sha=job.head_sha,
        snapshot_index=next_index,
        status=ReviewSnapshotStatus.PENDING,
        schema_version=REVIEW_SNAPSHOT_SCHEMA_VERSION,
        summary_text=None,
        flagged_findings_json=[],
        reviewer_guidance_json=[],
        snapshot_payload_json={
            "schema_version": REVIEW_SNAPSHOT_SCHEMA_VERSION,
            "review": {"notices": [], "notebooks": []},
        },
        notebook_count=0,
        changed_cell_count=0,
        failure_reason=None,
    )
    db_session.add(snapshot)
    db_session.flush()
    return snapshot


def _update_check_run_for_snapshot(
    *,
    settings: ApiSettings,
    github_client: ManagedGitHubClient,
    review: ManagedReview,
    repository: InstallationRepository,
    installation_id: int,
    snapshot: ReviewSnapshot,
    activity: str,
) -> int:
    details_url = _build_review_url(
        settings=settings,
        review=review,
        snapshot_index=snapshot.snapshot_index,
    )
    return github_client.create_or_update_check_run(
        settings=settings,
        installation_id=installation_id,
        repository=repository.full_name,
        head_sha=snapshot.head_sha,
        status="completed",
        conclusion="neutral",
        details_url=details_url,
        external_id=str(review.id),
        summary=_render_check_run_summary(
            review_url=details_url,
            snapshot_status="ready",
            activity=activity,
            thread_counts=_thread_counts(),
        ),
        check_run_id=review.latest_check_run_id,
    ).check_run_id


def _load_reviewer_playbooks(config_text: str | None) -> tuple[tuple[ReviewerPlaybook, ...], list[str]]:
    if config_text is None:
        return (), []
    try:
        return parse_reviewer_playbooks(config_text), []
    except NotebookLensConfigError as exc:
        return (), [f"{_CONFIG_PATH}: invalid config ignored ({exc})"]


def _render_check_run_summary(
    *,
    review_url: str,
    snapshot_status: str,
    activity: str,
    thread_counts: ThreadCounts,
    failure_reason: str | None = None,
) -> str:
    lines = [
        f"[Open in NotebookLens]({review_url})",
        f"Latest snapshot status: `{snapshot_status}`",
        (
            "Threads: "
            f"{thread_counts.unresolved} unresolved, "
            f"{thread_counts.resolved} resolved, "
            f"{thread_counts.outdated} outdated"
        ),
        f"Activity: {activity}",
    ]
    if failure_reason:
        lines.append(f"Failure: {failure_reason}")
    return "\n".join(lines)


def _build_review_url(
    *,
    settings: ApiSettings,
    review: ManagedReview,
    snapshot_index: int | None = None,
) -> str:
    base = (
        f"{settings.app_base_url}/reviews/{review.owner}/{review.repo}/pulls/{review.pull_number}"
    )
    if snapshot_index is None:
        return base
    return f"{base}/snapshots/{snapshot_index}"


def _job_matches_latest_review(*, review: ManagedReview, job: SnapshotBuildJob) -> bool:
    return review.latest_base_sha == job.base_sha and review.latest_head_sha == job.head_sha


def _thread_counts() -> ThreadCounts:
    return ThreadCounts()


def _short_sha(value: str) -> str:
    return value[:7]


def _truncate_error(exc: BaseException) -> str:
    message = str(exc).strip()
    if not message:
        return exc.__class__.__name__
    return message[:300]


def _require_mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if isinstance(value, Mapping):
        return value
    raise ManagedWebhookPayloadError(f"GitHub webhook payload is missing `{key}`")


def _require_str(payload: Mapping[str, Any] | Any, key: str) -> str:
    value = payload.get(key) if isinstance(payload, Mapping) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ManagedWebhookPayloadError(f"GitHub webhook payload is missing `{key}`")


def _require_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, int):
        return value
    raise ManagedWebhookPayloadError(f"GitHub webhook payload is missing `{key}`")


def _optional_int(payload: Mapping[str, Any] | Any, key: str) -> int | None:
    value = payload.get(key) if isinstance(payload, Mapping) else None
    return value if isinstance(value, int) else None


def _optional_str(payload: Mapping[str, Any] | Any, key: str) -> str | None:
    value = payload.get(key) if isinstance(payload, Mapping) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


__all__ = [
    "MANAGED_REVIEW_CHECK_RUN_NAME",
    "ManagedWebhookPayloadError",
    "PullRequestWebhook",
    "SnapshotBuildResult",
    "SUPPORTED_PULL_REQUEST_ACTIONS",
    "WebhookIngestionResult",
    "ingest_pull_request_webhook",
    "parse_pull_request_webhook",
    "run_snapshot_build_worker_once",
]
