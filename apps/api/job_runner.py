"""PostgreSQL-backed snapshot build job primitives for the managed API skeleton."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import SnapshotBuildJob, SnapshotBuildJobStatus


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def enqueue_snapshot_build_job(
    db_session: Session,
    *,
    managed_review_id,
    base_sha: str,
    head_sha: str,
    scheduled_at: datetime | None = None,
) -> SnapshotBuildJob:
    """Create a queued snapshot build job."""
    job = SnapshotBuildJob(
        managed_review_id=managed_review_id,
        base_sha=base_sha,
        head_sha=head_sha,
        status=SnapshotBuildJobStatus.QUEUED,
        attempt_count=0,
        scheduled_at=scheduled_at or utcnow(),
    )
    db_session.add(job)
    db_session.flush()
    return job


def claim_next_snapshot_build_job(
    db_session: Session,
    *,
    now: datetime | None = None,
) -> Optional[SnapshotBuildJob]:
    """Claim the next queued or retryable snapshot build job."""
    current_time = now or utcnow()
    statement = (
        select(SnapshotBuildJob)
        .where(
            SnapshotBuildJob.status.in_(
                (SnapshotBuildJobStatus.QUEUED, SnapshotBuildJobStatus.RETRYABLE_FAILED)
            ),
            SnapshotBuildJob.scheduled_at <= current_time,
        )
        .order_by(SnapshotBuildJob.scheduled_at.asc(), SnapshotBuildJob.id.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    job = db_session.execute(statement).scalars().first()
    if job is None:
        return None
    job.status = SnapshotBuildJobStatus.RUNNING
    job.attempt_count += 1
    job.started_at = current_time
    job.finished_at = None
    job.last_error = None
    db_session.flush()
    return job


def mark_snapshot_build_job_succeeded(
    db_session: Session,
    job: SnapshotBuildJob,
    *,
    finished_at: datetime | None = None,
) -> SnapshotBuildJob:
    """Mark a claimed snapshot build job as succeeded."""
    job.status = SnapshotBuildJobStatus.SUCCEEDED
    job.finished_at = finished_at or utcnow()
    job.last_error = None
    db_session.flush()
    return job


def mark_snapshot_build_job_retryable_failed(
    db_session: Session,
    job: SnapshotBuildJob,
    *,
    error_message: str,
    retry_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> SnapshotBuildJob:
    """Mark a claimed job as retryable and reschedule it."""
    job.status = SnapshotBuildJobStatus.RETRYABLE_FAILED
    job.last_error = error_message
    job.finished_at = finished_at or utcnow()
    job.scheduled_at = retry_at or job.finished_at
    db_session.flush()
    return job


def mark_snapshot_build_job_failed(
    db_session: Session,
    job: SnapshotBuildJob,
    *,
    error_message: str,
    finished_at: datetime | None = None,
) -> SnapshotBuildJob:
    """Mark a claimed job as permanently failed."""
    job.status = SnapshotBuildJobStatus.FAILED
    job.last_error = error_message
    job.finished_at = finished_at or utcnow()
    db_session.flush()
    return job
