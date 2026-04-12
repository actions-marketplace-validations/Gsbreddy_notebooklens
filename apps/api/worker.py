"""Managed snapshot worker entrypoints."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .config import ApiSettings, get_settings
from .database import session_scope
from .managed_github import ManagedGitHubClient
from .models import ManagedReview
from .notification_delivery import (
    NotificationDeliveryResult,
    ResendEmailClient,
    build_notification_email_client,
    deliver_pending_notifications,
)
from .orchestration import (
    LiteLLMGatewayClient,
    SnapshotBuildResult,
    run_snapshot_build_worker_once,
)


@dataclass(frozen=True)
class RetentionCleanupResult:
    purged_reviews: int


def purge_expired_managed_review_data(
    *,
    settings: ApiSettings,
    db_session,
    now: datetime | None = None,
) -> RetentionCleanupResult:
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(
        days=settings.snapshot_retention_days
    )
    expired_reviews = db_session.execute(
        select(ManagedReview).where(ManagedReview.updated_at < cutoff)
    ).scalars().all()
    for review in expired_reviews:
        db_session.delete(review)
    db_session.flush()
    return RetentionCleanupResult(purged_reviews=len(expired_reviews))


def process_retention_cleanup_once(
    *,
    settings: ApiSettings | None = None,
    now: datetime | None = None,
) -> RetentionCleanupResult:
    resolved_settings = settings or get_settings()
    with session_scope(resolved_settings) as db_session:
        return purge_expired_managed_review_data(
            settings=resolved_settings,
            db_session=db_session,
            now=now,
        )


def process_snapshot_build_job_once(
    *,
    settings: ApiSettings | None = None,
    github_client: ManagedGitHubClient | None = None,
    litellm_client: LiteLLMGatewayClient | None = None,
) -> SnapshotBuildResult:
    """Claim and process one managed snapshot build job."""
    resolved_settings = settings or get_settings()
    resolved_github_client = github_client or ManagedGitHubClient()
    with session_scope(resolved_settings) as db_session:
        purge_expired_managed_review_data(
            settings=resolved_settings,
            db_session=db_session,
        )
        return run_snapshot_build_worker_once(
            settings=resolved_settings,
            db_session=db_session,
            github_client=resolved_github_client,
            litellm_client=litellm_client,
        )


def process_notification_delivery_once(
    *,
    settings: ApiSettings | None = None,
    email_client: ResendEmailClient | None = None,
    limit: int = 25,
) -> NotificationDeliveryResult:
    """Process one batch of pending outbox notifications."""
    resolved_settings = settings or get_settings()
    resolved_email_client = email_client or build_notification_email_client(
        settings=resolved_settings
    )
    with session_scope(resolved_settings) as db_session:
        purge_expired_managed_review_data(
            settings=resolved_settings,
            db_session=db_session,
        )
        return deliver_pending_notifications(
            settings=resolved_settings,
            db_session=db_session,
            email_client=resolved_email_client,
            limit=limit,
        )


__all__ = [
    "RetentionCleanupResult",
    "process_notification_delivery_once",
    "process_retention_cleanup_once",
    "process_snapshot_build_job_once",
    "purge_expired_managed_review_data",
]
