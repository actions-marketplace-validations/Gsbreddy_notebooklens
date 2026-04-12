"""Managed review check-run summary helpers."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .config import ApiSettings
from .managed_github import ManagedGitHubClient
from .models import (
    InstallationRepository,
    ManagedReview,
    ManagedReviewStatus,
    ReviewSnapshot,
    SnapshotBuildJob,
    SnapshotBuildJobStatus,
)
from .review_workspace import count_review_threads


def build_review_url(
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


def render_review_workspace_check_run_summary(
    *,
    review_url: str,
    snapshot_status: str,
    activity: str,
    unresolved_threads: int,
    resolved_threads: int,
    outdated_threads: int,
    failure_reason: str | None = None,
) -> str:
    lines = [
        f"[Open in NotebookLens]({review_url})",
        f"Latest snapshot status: `{snapshot_status}`",
        (
            "Threads: "
            f"{unresolved_threads} unresolved, "
            f"{resolved_threads} resolved, "
            f"{outdated_threads} outdated"
        ),
        f"Activity: {activity}",
    ]
    if failure_reason:
        lines.append(f"Failure: {failure_reason}")
    return "\n".join(lines)


def sync_review_workspace_check_run(
    *,
    settings: ApiSettings,
    db_session: Session,
    github_client: ManagedGitHubClient,
    review: ManagedReview,
    activity: str,
) -> int:
    db_session.flush()
    hydrated_review = db_session.execute(
        select(ManagedReview)
        .options(
            selectinload(ManagedReview.installation_repository).selectinload(
                InstallationRepository.installation
            )
        )
        .where(ManagedReview.id == review.id)
    ).scalar_one()
    repository = hydrated_review.installation_repository
    installation = repository.installation

    latest_selected_snapshot = (
        db_session.get(ReviewSnapshot, hydrated_review.latest_snapshot_id)
        if hydrated_review.latest_snapshot_id is not None
        else None
    )
    latest_timeline_snapshot = db_session.execute(
        select(ReviewSnapshot)
        .where(ReviewSnapshot.managed_review_id == hydrated_review.id)
        .order_by(ReviewSnapshot.snapshot_index.desc())
        .limit(1)
    ).scalar_one_or_none()
    thread_counts = count_review_threads(
        db_session=db_session,
        managed_review_id=hydrated_review.id,
    )

    snapshot_status = "ready"
    check_run_status = "completed"
    conclusion = "neutral"
    failure_reason = None
    details_url = build_review_url(
        settings=settings,
        review=hydrated_review,
        snapshot_index=(
            latest_selected_snapshot.snapshot_index if latest_selected_snapshot is not None else None
        ),
    )

    if hydrated_review.status == ManagedReviewStatus.PENDING:
        snapshot_status = "pending"
        active_job = db_session.execute(
            select(SnapshotBuildJob)
            .where(
                SnapshotBuildJob.managed_review_id == hydrated_review.id,
                SnapshotBuildJob.base_sha == hydrated_review.latest_base_sha,
                SnapshotBuildJob.head_sha == hydrated_review.latest_head_sha,
                SnapshotBuildJob.status.in_(
                    (
                        SnapshotBuildJobStatus.QUEUED,
                        SnapshotBuildJobStatus.RUNNING,
                        SnapshotBuildJobStatus.RETRYABLE_FAILED,
                    )
                ),
            )
            .order_by(SnapshotBuildJob.scheduled_at.asc(), SnapshotBuildJob.id.asc())
            .limit(1)
        ).scalar_one_or_none()
        if active_job is not None and active_job.status == SnapshotBuildJobStatus.RUNNING:
            check_run_status = "in_progress"
        else:
            check_run_status = "queued"
        conclusion = None
        details_url = build_review_url(settings=settings, review=hydrated_review)
    elif hydrated_review.status == ManagedReviewStatus.FAILED:
        snapshot_status = "failed"
        conclusion = "action_required"
        details_url = build_review_url(settings=settings, review=hydrated_review)
        if latest_timeline_snapshot is not None:
            failure_reason = latest_timeline_snapshot.failure_reason

    check_run = github_client.create_or_update_check_run(
        settings=settings,
        installation_id=installation.github_installation_id,
        repository=repository.full_name,
        head_sha=hydrated_review.latest_head_sha,
        status=check_run_status,
        conclusion=conclusion,
        details_url=details_url,
        external_id=str(hydrated_review.id),
        summary=render_review_workspace_check_run_summary(
            review_url=details_url,
            snapshot_status=snapshot_status,
            activity=activity,
            unresolved_threads=thread_counts.unresolved,
            resolved_threads=thread_counts.resolved,
            outdated_threads=thread_counts.outdated,
            failure_reason=failure_reason,
        ),
        check_run_id=hydrated_review.latest_check_run_id,
    )
    hydrated_review.latest_check_run_id = check_run.check_run_id
    db_session.flush()
    return check_run.check_run_id


__all__ = [
    "build_review_url",
    "render_review_workspace_check_run_summary",
    "sync_review_workspace_check_run",
]
