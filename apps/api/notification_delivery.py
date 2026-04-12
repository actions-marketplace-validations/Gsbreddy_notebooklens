"""Transactional email delivery for pending managed-review notifications."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from src import __display_version__

from .check_runs import build_review_url
from .config import ApiConfigurationError, ApiSettings
from .models import NotificationDeliveryState, NotificationEventType, NotificationOutbox, ReviewThread


class NotificationDeliveryError(RuntimeError):
    """Raised when a pending notification cannot be delivered."""


@dataclass(frozen=True)
class NotificationDeliveryResult:
    """Batch result for one pending outbox delivery pass."""

    processed: int
    sent: int
    failed: int


@dataclass(frozen=True)
class TransactionalEmail:
    """Rendered email content ready for transport."""

    to_email: str
    subject: str
    text_body: str
    html_body: str


class ResendEmailClient:
    """Small Resend API client used by the managed notification outbox worker."""

    def __init__(
        self,
        *,
        api_key: str,
        email_from: str,
        api_base_url: str = "https://api.resend.com",
        session: requests.Session | None = None,
    ) -> None:
        self.api_key = api_key
        self.email_from = email_from
        self.api_base_url = api_base_url.rstrip("/")
        self.session = session or requests.Session()

    def send_transactional_email(self, message: TransactionalEmail) -> None:
        response = self.session.post(
            f"{self.api_base_url}/emails",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": f"notebooklens-managed/{__display_version__}",
            },
            json={
                "from": self.email_from,
                "to": [message.to_email],
                "subject": message.subject,
                "text": message.text_body,
                "html": message.html_body,
            },
            timeout=30,
        )
        if int(response.status_code) not in {200, 202}:
            raise NotificationDeliveryError(
                f"Resend email send failed with status {response.status_code}"
            )


def build_notification_email_client(
    *,
    settings: ApiSettings,
    session: requests.Session | None = None,
) -> ResendEmailClient:
    provider = settings.email_provider.strip().lower()
    if provider != "resend":
        raise ApiConfigurationError(f"Unsupported EMAIL_PROVIDER: {settings.email_provider}")
    return ResendEmailClient(
        api_key=settings.email_api_key,
        email_from=settings.email_from,
        session=session,
    )


def deliver_pending_notifications(
    *,
    settings: ApiSettings,
    db_session: Session,
    email_client: ResendEmailClient,
    limit: int = 25,
) -> NotificationDeliveryResult:
    notifications = db_session.execute(
        select(NotificationOutbox)
        .options(
            selectinload(NotificationOutbox.thread).selectinload(ReviewThread.current_snapshot),
            selectinload(NotificationOutbox.thread).selectinload(ReviewThread.managed_review),
            selectinload(NotificationOutbox.thread).selectinload(ReviewThread.messages),
        )
        .where(NotificationOutbox.delivery_state == NotificationDeliveryState.PENDING)
        .order_by(NotificationOutbox.created_at.asc())
        .limit(limit)
    ).scalars().all()

    sent = 0
    failed = 0
    for notification in notifications:
        notification.attempt_count += 1
        try:
            email_client.send_transactional_email(
                _build_transactional_email(settings=settings, notification=notification)
            )
            notification.delivery_state = NotificationDeliveryState.SENT
            notification.sent_at = datetime.now(timezone.utc)
            notification.last_error = None
            sent += 1
        except Exception as exc:
            notification.delivery_state = NotificationDeliveryState.FAILED
            notification.last_error = _truncate_error(exc)
            failed += 1

    db_session.flush()
    return NotificationDeliveryResult(
        processed=len(notifications),
        sent=sent,
        failed=failed,
    )


def _build_transactional_email(
    *,
    settings: ApiSettings,
    notification: NotificationOutbox,
) -> TransactionalEmail:
    thread = notification.thread
    review = thread.managed_review
    actor_login = _payload_str(notification.payload_json, "actor_login") or "A reviewer"
    notebook_path = _thread_notebook_path(thread)
    snapshot_index = thread.current_snapshot.snapshot_index if thread.current_snapshot is not None else None
    review_url = build_review_url(
        settings=settings,
        review=review,
        snapshot_index=snapshot_index,
    )
    event_message = _payload_str(notification.payload_json, "message_body_markdown")
    subject = _notification_subject(notification.event_type, review.owner, review.repo, review.pull_number)

    action_line = _notification_action_line(
        event_type=notification.event_type,
        actor_login=actor_login,
        notebook_path=notebook_path,
    )
    lines = [
        f"Repository: {review.owner}/{review.repo}",
        f"Pull request: #{review.pull_number}",
        action_line,
        f"Open in NotebookLens: {review_url}",
    ]
    if event_message:
        lines.append("")
        lines.append("Latest thread message:")
        lines.append(event_message)

    escaped_action_line = escape(action_line)
    escaped_review_url = escape(review_url)
    html_parts = [
        f"<p>{escape(subject)}</p>",
        f"<p>{escaped_action_line}</p>",
        f"<p><strong>Repository:</strong> {escape(review.owner)}/{escape(review.repo)}<br>"
        f"<strong>Pull request:</strong> #{review.pull_number}</p>",
        f"<p><a href=\"{escaped_review_url}\">Open in NotebookLens</a></p>",
    ]
    if event_message:
        html_parts.append(
            f"<p><strong>Latest thread message:</strong><br>{escape(event_message)}</p>"
        )

    return TransactionalEmail(
        to_email=notification.recipient_email,
        subject=subject,
        text_body="\n".join(lines),
        html_body="".join(html_parts),
    )


def _notification_subject(
    event_type: NotificationEventType,
    owner: str,
    repo: str,
    pull_number: int,
) -> str:
    prefix = f"{owner}/{repo}#{pull_number}"
    if event_type == NotificationEventType.THREAD_CREATED:
        return f"[NotebookLens] New thread on {prefix}"
    if event_type == NotificationEventType.REPLY_ADDED:
        return f"[NotebookLens] New reply on {prefix}"
    if event_type == NotificationEventType.THREAD_RESOLVED:
        return f"[NotebookLens] Thread resolved on {prefix}"
    return f"[NotebookLens] Thread reopened on {prefix}"


def _notification_action_line(
    *,
    event_type: NotificationEventType,
    actor_login: str,
    notebook_path: str,
) -> str:
    if event_type == NotificationEventType.THREAD_CREATED:
        return f"{actor_login} created a review thread on {notebook_path}."
    if event_type == NotificationEventType.REPLY_ADDED:
        return f"{actor_login} replied to a review thread on {notebook_path}."
    if event_type == NotificationEventType.THREAD_RESOLVED:
        return f"{actor_login} resolved a review thread on {notebook_path}."
    return f"{actor_login} reopened a review thread on {notebook_path}."


def _payload_str(payload: dict, key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _thread_notebook_path(thread: ReviewThread) -> str:
    anchor = thread.anchor_json if isinstance(thread.anchor_json, dict) else {}
    value = anchor.get("notebook_path")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "the notebook diff"


def _truncate_error(exc: BaseException) -> str:
    message = str(exc).strip()
    if not message:
        return exc.__class__.__name__
    return message[:300]


__all__ = [
    "NotificationDeliveryError",
    "NotificationDeliveryResult",
    "ResendEmailClient",
    "build_notification_email_client",
    "deliver_pending_notifications",
]
