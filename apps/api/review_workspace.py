"""Managed review workspace persistence and thread-state services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping
import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from src.review_core import SnapshotBlockKind

from .models import (
    ManagedReview,
    InstallationRepository,
    NotificationDeliveryState,
    NotificationEventType,
    NotificationOutbox,
    ReviewSnapshot,
    ReviewSnapshotStatus,
    ReviewThread,
    ReviewThreadStatus,
    ThreadMessage,
    UserSession,
)
from .oauth import GitHubOAuthClient, OAuthSessionStore


VALID_THREAD_BLOCK_KINDS: tuple[SnapshotBlockKind, ...] = ("source", "outputs", "metadata")


class ReviewWorkspaceError(ValueError):
    """Base error for review workspace operations."""


class ReviewWorkspaceNotFoundError(ReviewWorkspaceError):
    """Raised when a review-scoped record does not exist."""


class ReviewWorkspaceValidationError(ReviewWorkspaceError):
    """Raised when request data is invalid for the current snapshot/thread state."""


@dataclass(frozen=True)
class ThreadCounts:
    """Aggregated thread counts for check-run and workspace summaries."""

    unresolved: int = 0
    resolved: int = 0
    outdated: int = 0


def _touch_review(review: ManagedReview) -> None:
    review.updated_at = datetime.now(timezone.utc)


def count_review_threads(*, db_session: Session, managed_review_id: uuid.UUID) -> ThreadCounts:
    db_session.flush()
    rows = db_session.execute(
        select(ReviewThread.status, func.count(ReviewThread.id))
        .where(ReviewThread.managed_review_id == managed_review_id)
        .group_by(ReviewThread.status)
    ).all()
    counts = {status: count for status, count in rows}
    return ThreadCounts(
        unresolved=int(counts.get(ReviewThreadStatus.OPEN, 0)),
        resolved=int(counts.get(ReviewThreadStatus.RESOLVED, 0)),
        outdated=int(counts.get(ReviewThreadStatus.OUTDATED, 0)),
    )


def load_review_by_route(
    *,
    db_session: Session,
    owner: str,
    repo: str,
    pull_number: int,
) -> ManagedReview:
    review = db_session.execute(
        select(ManagedReview)
        .options(
            selectinload(ManagedReview.review_snapshots),
            selectinload(ManagedReview.review_threads).selectinload(ReviewThread.messages),
        )
        .where(
            ManagedReview.owner == owner,
            ManagedReview.repo == repo,
            ManagedReview.pull_number == pull_number,
        )
    ).scalar_one_or_none()
    if review is None:
        raise ReviewWorkspaceNotFoundError("Managed review not found")
    return review


def load_review_by_id(*, db_session: Session, review_id: str | uuid.UUID) -> ManagedReview:
    review = db_session.execute(
        select(ManagedReview)
        .options(
            selectinload(ManagedReview.installation_repository).selectinload(
                InstallationRepository.installation
            ),
            selectinload(ManagedReview.review_snapshots),
        )
        .where(ManagedReview.id == uuid.UUID(str(review_id)))
    ).scalar_one_or_none()
    if review is None:
        raise ReviewWorkspaceNotFoundError("Managed review not found")
    return review


def get_workspace_payload(
    *,
    db_session: Session,
    review: ManagedReview,
    snapshot_index: int | None = None,
) -> dict[str, Any]:
    snapshots = db_session.execute(
        select(ReviewSnapshot)
        .where(ReviewSnapshot.managed_review_id == review.id)
        .order_by(ReviewSnapshot.snapshot_index.asc())
    ).scalars().all()
    selected_snapshot = _select_snapshot(review=review, snapshots=snapshots, snapshot_index=snapshot_index)
    visible_threads = (
        list_visible_threads_for_snapshot(db_session=db_session, snapshot_id=selected_snapshot.id)
        if selected_snapshot is not None
        else []
    )
    counts = count_review_threads(db_session=db_session, managed_review_id=review.id)
    latest_snapshot_index = max((snapshot.snapshot_index for snapshot in snapshots), default=None)

    return {
        "review": {
            "id": str(review.id),
            "owner": review.owner,
            "repo": review.repo,
            "pull_number": review.pull_number,
            "base_branch": review.base_branch,
            "status": review.status.value,
            "latest_snapshot_id": str(review.latest_snapshot_id) if review.latest_snapshot_id else None,
            "latest_snapshot_index": latest_snapshot_index,
            "selected_snapshot_index": selected_snapshot.snapshot_index if selected_snapshot else None,
            "thread_counts": {
                "unresolved": counts.unresolved,
                "resolved": counts.resolved,
                "outdated": counts.outdated,
            },
            "snapshot_history": [
                {
                    "id": str(snapshot.id),
                    "snapshot_index": snapshot.snapshot_index,
                    "status": snapshot.status.value,
                    "base_sha": snapshot.base_sha,
                    "head_sha": snapshot.head_sha,
                    "created_at": snapshot.created_at.isoformat(),
                    "is_latest": review.latest_snapshot_id == snapshot.id,
                }
                for snapshot in snapshots
            ],
        },
        "snapshot": _serialize_snapshot(selected_snapshot) if selected_snapshot else None,
        "threads": [
            _serialize_thread(thread, snapshot_id=selected_snapshot.id if selected_snapshot else None)
            for thread in visible_threads
        ],
    }


def create_thread(
    *,
    db_session: Session,
    review: ManagedReview,
    snapshot_id: str | uuid.UUID,
    anchor: Mapping[str, Any],
    body_markdown: str,
    actor_github_user_id: int,
    actor_login: str,
    oauth_client: GitHubOAuthClient,
    session_store: OAuthSessionStore,
) -> ReviewThread:
    snapshot = _load_snapshot_for_review(
        db_session=db_session,
        review=review,
        snapshot_id=snapshot_id,
    )
    normalized_anchor = normalize_thread_anchor(anchor)
    if review.latest_snapshot_id != snapshot.id:
        raise ReviewWorkspaceValidationError("Threads can only be created on the latest ready snapshot")
    if snapshot.status != ReviewSnapshotStatus.READY:
        raise ReviewWorkspaceValidationError("Threads can only be created on ready snapshots")
    if not snapshot_contains_anchor(snapshot.snapshot_payload_json, normalized_anchor):
        raise ReviewWorkspaceValidationError("Thread anchor does not exist on the selected snapshot")
    if not snapshot_allows_thread_creation(snapshot.snapshot_payload_json, normalized_anchor):
        raise ReviewWorkspaceValidationError("Threads can only be created on changed blocks")

    body = _normalize_markdown(body_markdown)
    thread = ReviewThread(
        managed_review_id=review.id,
        origin_snapshot_id=snapshot.id,
        current_snapshot_id=snapshot.id,
        origin_anchor_json=normalized_anchor,
        anchor_json=normalized_anchor,
        status=ReviewThreadStatus.OPEN,
        carried_forward=False,
        created_by_github_user_id=actor_github_user_id,
    )
    db_session.add(thread)
    db_session.flush()

    message = ThreadMessage(
        thread_id=thread.id,
        author_github_user_id=actor_github_user_id,
        author_login=actor_login,
        body_markdown=body,
    )
    db_session.add(message)
    db_session.flush()
    db_session.refresh(thread)
    _touch_review(review)
    _enqueue_notifications(
        db_session=db_session,
        review=review,
        thread=thread,
        actor_github_user_id=actor_github_user_id,
        actor_login=actor_login,
        event_type=NotificationEventType.THREAD_CREATED,
        message_id=message.id,
        message_body_markdown=message.body_markdown,
        oauth_client=oauth_client,
        session_store=session_store,
    )
    db_session.flush()
    return _load_thread(db_session=db_session, thread_id=thread.id)


def add_thread_message(
    *,
    db_session: Session,
    thread_id: str | uuid.UUID,
    actor_github_user_id: int,
    actor_login: str,
    body_markdown: str,
    oauth_client: GitHubOAuthClient,
    session_store: OAuthSessionStore,
) -> ReviewThread:
    thread = _load_thread(db_session=db_session, thread_id=thread_id)
    message = ThreadMessage(
        thread_id=thread.id,
        author_github_user_id=actor_github_user_id,
        author_login=actor_login,
        body_markdown=_normalize_markdown(body_markdown),
    )
    db_session.add(message)
    thread.updated_at = datetime.now(timezone.utc)
    _touch_review(thread.managed_review)
    db_session.flush()
    db_session.expire(thread, ["messages"])
    _enqueue_notifications(
        db_session=db_session,
        review=thread.managed_review,
        thread=thread,
        actor_github_user_id=actor_github_user_id,
        actor_login=actor_login,
        event_type=NotificationEventType.REPLY_ADDED,
        message_id=message.id,
        message_body_markdown=message.body_markdown,
        oauth_client=oauth_client,
        session_store=session_store,
    )
    db_session.flush()
    return _load_thread(db_session=db_session, thread_id=thread.id)


def resolve_thread(
    *,
    db_session: Session,
    thread_id: str | uuid.UUID,
    actor_github_user_id: int,
    actor_login: str,
    oauth_client: GitHubOAuthClient,
    session_store: OAuthSessionStore,
) -> ReviewThread:
    thread = _load_thread(db_session=db_session, thread_id=thread_id)
    if thread.status == ReviewThreadStatus.RESOLVED:
        return thread
    thread.status = ReviewThreadStatus.RESOLVED
    thread.resolved_at = datetime.now(timezone.utc)
    thread.resolved_by_github_user_id = actor_github_user_id
    _touch_review(thread.managed_review)
    db_session.flush()
    _enqueue_notifications(
        db_session=db_session,
        review=thread.managed_review,
        thread=thread,
        actor_github_user_id=actor_github_user_id,
        actor_login=actor_login,
        event_type=NotificationEventType.THREAD_RESOLVED,
        message_id=None,
        message_body_markdown=None,
        oauth_client=oauth_client,
        session_store=session_store,
    )
    db_session.flush()
    return _load_thread(db_session=db_session, thread_id=thread.id)


def reopen_thread(
    *,
    db_session: Session,
    thread_id: str | uuid.UUID,
    actor_github_user_id: int,
    actor_login: str,
    oauth_client: GitHubOAuthClient,
    session_store: OAuthSessionStore,
) -> ReviewThread:
    thread = _load_thread(db_session=db_session, thread_id=thread_id)
    if thread.status != ReviewThreadStatus.RESOLVED:
        return thread
    latest_snapshot_id = thread.managed_review.latest_snapshot_id
    thread.status = (
        ReviewThreadStatus.OPEN
        if latest_snapshot_id is not None and thread.current_snapshot_id == latest_snapshot_id
        else ReviewThreadStatus.OUTDATED
    )
    thread.resolved_at = None
    thread.resolved_by_github_user_id = None
    _touch_review(thread.managed_review)
    db_session.flush()
    _enqueue_notifications(
        db_session=db_session,
        review=thread.managed_review,
        thread=thread,
        actor_github_user_id=actor_github_user_id,
        actor_login=actor_login,
        event_type=NotificationEventType.THREAD_REOPENED,
        message_id=None,
        message_body_markdown=None,
        oauth_client=oauth_client,
        session_store=session_store,
    )
    db_session.flush()
    return _load_thread(db_session=db_session, thread_id=thread.id)


def list_visible_threads_for_snapshot(
    *,
    db_session: Session,
    snapshot_id: uuid.UUID,
) -> list[ReviewThread]:
    return db_session.execute(
        select(ReviewThread)
        .options(selectinload(ReviewThread.messages))
        .where(
            or_(
                ReviewThread.origin_snapshot_id == snapshot_id,
                ReviewThread.current_snapshot_id == snapshot_id,
            )
        )
        .order_by(ReviewThread.created_at.asc())
    ).scalars().all()


def carry_forward_open_threads(
    *,
    db_session: Session,
    review: ManagedReview,
    snapshot: ReviewSnapshot,
) -> None:
    open_threads = db_session.execute(
        select(ReviewThread)
        .where(
            ReviewThread.managed_review_id == review.id,
            ReviewThread.status == ReviewThreadStatus.OPEN,
        )
        .order_by(ReviewThread.created_at.asc())
    ).scalars().all()
    if not open_threads:
        return
    candidate_anchors = list(iter_snapshot_anchors(snapshot.snapshot_payload_json))
    for thread in open_threads:
        if thread.current_snapshot_id == snapshot.id:
            continue
        matched_anchor = next(
            (
                candidate
                for candidate in candidate_anchors
                if anchors_match_for_carry_forward(thread.anchor_json, candidate)
            ),
            None,
        )
        if matched_anchor is None:
            thread.status = ReviewThreadStatus.OUTDATED
            continue
        thread.anchor_json = matched_anchor
        thread.current_snapshot_id = snapshot.id
        thread.carried_forward = True


def normalize_thread_anchor(anchor: Mapping[str, Any]) -> dict[str, Any]:
    notebook_path = anchor.get("notebook_path")
    block_kind = anchor.get("block_kind")
    source_fingerprint = anchor.get("source_fingerprint")
    cell_type = anchor.get("cell_type")
    raw_locator = anchor.get("cell_locator")

    if not isinstance(notebook_path, str) or not notebook_path.strip():
        raise ReviewWorkspaceValidationError("Thread anchor requires notebook_path")
    if block_kind not in VALID_THREAD_BLOCK_KINDS:
        raise ReviewWorkspaceValidationError("Thread anchor requires a valid block_kind")
    if not isinstance(source_fingerprint, str) or not source_fingerprint.strip():
        raise ReviewWorkspaceValidationError("Thread anchor requires source_fingerprint")
    if cell_type not in {"code", "markdown", "raw"}:
        raise ReviewWorkspaceValidationError("Thread anchor requires a valid cell_type")
    if not isinstance(raw_locator, Mapping):
        raise ReviewWorkspaceValidationError("Thread anchor requires cell_locator")

    return {
        "notebook_path": notebook_path.strip(),
        "block_kind": block_kind,
        "source_fingerprint": source_fingerprint.strip(),
        "cell_type": cell_type,
        "cell_locator": {
            "cell_id": _coerce_str_or_none(raw_locator.get("cell_id")),
            "base_index": _coerce_int_or_none(raw_locator.get("base_index")),
            "head_index": _coerce_int_or_none(raw_locator.get("head_index")),
            "display_index": _coerce_int_or_none(raw_locator.get("display_index")),
        },
    }


def snapshot_contains_anchor(snapshot_payload: Mapping[str, Any], anchor: Mapping[str, Any]) -> bool:
    return any(candidate == dict(anchor) for candidate in iter_snapshot_anchors(snapshot_payload))


def snapshot_allows_thread_creation(
    snapshot_payload: Mapping[str, Any],
    anchor: Mapping[str, Any],
) -> bool:
    normalized_anchor = normalize_thread_anchor(anchor)
    for row in iter_snapshot_render_rows(snapshot_payload):
        thread_anchors = row.get("thread_anchors")
        if not isinstance(thread_anchors, Mapping):
            continue
        candidate = thread_anchors.get(normalized_anchor["block_kind"])
        if not isinstance(candidate, Mapping):
            continue
        if normalize_thread_anchor(candidate) != normalized_anchor:
            continue
        return _row_block_changed(row, normalized_anchor["block_kind"])
    return False


def iter_snapshot_anchors(snapshot_payload: Mapping[str, Any]) -> Iterable[dict[str, Any]]:
    anchors: list[dict[str, Any]] = []
    for row in iter_snapshot_render_rows(snapshot_payload):
        thread_anchors = row.get("thread_anchors")
        if not isinstance(thread_anchors, Mapping):
            continue
        for candidate in thread_anchors.values():
            if isinstance(candidate, Mapping):
                anchors.append(normalize_thread_anchor(candidate))
    return tuple(anchors)


def iter_snapshot_render_rows(snapshot_payload: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    review = snapshot_payload.get("review")
    notebooks = review.get("notebooks") if isinstance(review, Mapping) else None
    if not isinstance(notebooks, list):
        return ()
    rows: list[Mapping[str, Any]] = []
    for notebook in notebooks:
        if not isinstance(notebook, Mapping):
            continue
        render_rows = notebook.get("render_rows")
        if not isinstance(render_rows, list):
            continue
        for row in render_rows:
            if isinstance(row, Mapping):
                rows.append(row)
    return tuple(rows)


def anchors_match_for_carry_forward(
    previous_anchor: Mapping[str, Any],
    candidate_anchor: Mapping[str, Any],
) -> bool:
    old_anchor = normalize_thread_anchor(previous_anchor)
    new_anchor = normalize_thread_anchor(candidate_anchor)
    if old_anchor["notebook_path"] != new_anchor["notebook_path"]:
        return False
    if old_anchor["block_kind"] != new_anchor["block_kind"]:
        return False
    if old_anchor["source_fingerprint"] != new_anchor["source_fingerprint"]:
        return False
    if old_anchor["cell_type"] != new_anchor["cell_type"]:
        return False
    return _stable_locator_matches(old_anchor["cell_locator"], new_anchor["cell_locator"])


def _stable_locator_matches(
    previous_locator: Mapping[str, Any],
    candidate_locator: Mapping[str, Any],
) -> bool:
    previous_cell_id = _coerce_str_or_none(previous_locator.get("cell_id"))
    candidate_cell_id = _coerce_str_or_none(candidate_locator.get("cell_id"))
    if previous_cell_id and candidate_cell_id:
        return previous_cell_id == candidate_cell_id
    if previous_cell_id or candidate_cell_id:
        return False

    previous_head_index = _coerce_int_or_none(previous_locator.get("head_index"))
    candidate_base_index = _coerce_int_or_none(candidate_locator.get("base_index"))
    if previous_head_index is not None and candidate_base_index is not None:
        return previous_head_index == candidate_base_index

    previous_display_index = _coerce_int_or_none(previous_locator.get("display_index"))
    candidate_display_index = _coerce_int_or_none(candidate_locator.get("display_index"))
    if previous_display_index is not None and candidate_display_index is not None:
        return previous_display_index == candidate_display_index

    previous_base_index = _coerce_int_or_none(previous_locator.get("base_index"))
    if previous_base_index is not None and candidate_base_index is not None:
        return previous_base_index == candidate_base_index
    return False


def _load_snapshot_for_review(
    *,
    db_session: Session,
    review: ManagedReview,
    snapshot_id: str | uuid.UUID,
) -> ReviewSnapshot:
    snapshot = db_session.execute(
        select(ReviewSnapshot).where(
            ReviewSnapshot.id == uuid.UUID(str(snapshot_id)),
            ReviewSnapshot.managed_review_id == review.id,
        )
    ).scalar_one_or_none()
    if snapshot is None:
        raise ReviewWorkspaceNotFoundError("Review snapshot not found")
    return snapshot


def _load_thread(*, db_session: Session, thread_id: str | uuid.UUID) -> ReviewThread:
    thread = db_session.execute(
        select(ReviewThread)
        .execution_options(populate_existing=True)
        .options(
            selectinload(ReviewThread.messages),
            selectinload(ReviewThread.managed_review).selectinload(
                ManagedReview.installation_repository
            ),
        )
        .where(ReviewThread.id == uuid.UUID(str(thread_id)))
    ).scalar_one_or_none()
    if thread is None:
        raise ReviewWorkspaceNotFoundError("Review thread not found")
    return thread


def load_thread_by_id(*, db_session: Session, thread_id: str | uuid.UUID) -> ReviewThread:
    return _load_thread(db_session=db_session, thread_id=thread_id)


def _enqueue_notifications(
    *,
    db_session: Session,
    review: ManagedReview,
    thread: ReviewThread,
    actor_github_user_id: int,
    actor_login: str,
    event_type: NotificationEventType,
    message_id: uuid.UUID | None,
    message_body_markdown: str | None,
    oauth_client: GitHubOAuthClient,
    session_store: OAuthSessionStore,
) -> None:
    recipients = _notification_recipients(
        db_session=db_session,
        review=review,
        thread=thread,
        actor_github_user_id=actor_github_user_id,
        event_type=event_type,
        oauth_client=oauth_client,
        session_store=session_store,
    )
    payload = {
        "thread_id": str(thread.id),
        "review_id": str(review.id),
        "owner": review.owner,
        "repo": review.repo,
        "pull_number": review.pull_number,
        "actor_github_user_id": actor_github_user_id,
        "actor_login": actor_login,
        "event_type": event_type.value,
    }
    if message_id is not None:
        payload["message_id"] = str(message_id)
    if isinstance(message_body_markdown, str) and message_body_markdown.strip():
        payload["message_body_markdown"] = message_body_markdown.strip()
    for recipient_id, recipient_email in recipients:
        db_session.add(
            NotificationOutbox(
                thread_id=thread.id,
                event_type=event_type,
                recipient_github_user_id=recipient_id,
                recipient_email=recipient_email,
                payload_json=payload,
                delivery_state=NotificationDeliveryState.PENDING,
                attempt_count=0,
                last_error=None,
            )
        )


def _notification_recipients(
    *,
    db_session: Session,
    review: ManagedReview,
    thread: ReviewThread,
    actor_github_user_id: int,
    event_type: NotificationEventType,
    oauth_client: GitHubOAuthClient,
    session_store: OAuthSessionStore,
) -> list[tuple[int, str]]:
    if event_type == NotificationEventType.THREAD_CREATED:
        candidate_ids = [
            user_id
            for user_id in [review.pull_author_github_user_id]
            if user_id is not None and user_id != actor_github_user_id
        ]
    else:
        participant_ids = {
            message.author_github_user_id
            for message in thread.messages
            if message.author_github_user_id != actor_github_user_id
        }
        if review.pull_author_github_user_id is not None:
            participant_ids.add(review.pull_author_github_user_id)
        candidate_ids = sorted(user_id for user_id in participant_ids if user_id != actor_github_user_id)

    recipients: list[tuple[int, str]] = []
    for candidate_id in candidate_ids:
        email = _resolve_known_email(
            db_session=db_session,
            github_user_id=candidate_id,
            oauth_client=oauth_client,
            session_store=session_store,
        )
        if email:
            recipients.append((candidate_id, email))
    return recipients


def _resolve_known_email(
    *,
    db_session: Session,
    github_user_id: int,
    oauth_client: GitHubOAuthClient,
    session_store: OAuthSessionStore,
) -> str | None:
    now = datetime.now(timezone.utc)
    session_candidates = db_session.execute(
        select(UserSession)
        .where(UserSession.github_user_id == github_user_id)
        .order_by(UserSession.created_at.desc())
    ).scalars().all()
    session_record = next(
        (
            item
            for item in session_candidates
            if _ensure_utc(item.expires_at) >= now
        ),
        None,
    )
    if session_record is None:
        return None
    access_token = session_store.cipher.decrypt(session_record.access_token_encrypted)
    email = oauth_client.fetch_user(access_token).email
    if not isinstance(email, str) or not email.strip():
        return None
    return email.strip()


def _select_snapshot(
    *,
    review: ManagedReview,
    snapshots: list[ReviewSnapshot],
    snapshot_index: int | None,
) -> ReviewSnapshot | None:
    if snapshot_index is not None:
        for snapshot in snapshots:
            if snapshot.snapshot_index == snapshot_index:
                return snapshot
        raise ReviewWorkspaceNotFoundError("Review snapshot not found")
    if review.latest_snapshot_id is not None:
        for snapshot in snapshots:
            if snapshot.id == review.latest_snapshot_id:
                return snapshot
    return snapshots[-1] if snapshots else None


def _row_block_changed(row: Mapping[str, Any], block_kind: SnapshotBlockKind) -> bool:
    if block_kind == "source":
        source = row.get("source")
        return isinstance(source, Mapping) and bool(source.get("changed"))
    if block_kind == "outputs":
        outputs = row.get("outputs")
        return isinstance(outputs, Mapping) and bool(outputs.get("changed"))
    metadata = row.get("metadata")
    return isinstance(metadata, Mapping) and bool(metadata.get("changed"))


def _serialize_snapshot(snapshot: ReviewSnapshot) -> dict[str, Any]:
    return {
        "id": str(snapshot.id),
        "snapshot_index": snapshot.snapshot_index,
        "status": snapshot.status.value,
        "base_sha": snapshot.base_sha,
        "head_sha": snapshot.head_sha,
        "schema_version": snapshot.schema_version,
        "summary_text": snapshot.summary_text,
        "flagged_findings": snapshot.flagged_findings_json,
        "reviewer_guidance": snapshot.reviewer_guidance_json,
        "payload": snapshot.snapshot_payload_json,
        "notebook_count": snapshot.notebook_count,
        "changed_cell_count": snapshot.changed_cell_count,
        "failure_reason": snapshot.failure_reason,
        "created_at": snapshot.created_at.isoformat(),
    }


def _effective_thread_anchor(
    thread: ReviewThread,
    *,
    snapshot_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    raw_anchor: Any
    if snapshot_id is not None and snapshot_id == thread.origin_snapshot_id:
        raw_anchor = thread.origin_anchor_json
    else:
        raw_anchor = thread.anchor_json
    return dict(raw_anchor) if isinstance(raw_anchor, Mapping) else {}


def _serialize_thread(
    thread: ReviewThread,
    *,
    snapshot_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    return {
        "id": str(thread.id),
        "managed_review_id": str(thread.managed_review_id),
        "origin_snapshot_id": str(thread.origin_snapshot_id),
        "current_snapshot_id": str(thread.current_snapshot_id),
        "anchor": _effective_thread_anchor(thread, snapshot_id=snapshot_id),
        "status": thread.status.value,
        "carried_forward": thread.carried_forward,
        "created_by_github_user_id": thread.created_by_github_user_id,
        "created_at": thread.created_at.isoformat(),
        "updated_at": thread.updated_at.isoformat(),
        "resolved_at": thread.resolved_at.isoformat() if thread.resolved_at else None,
        "resolved_by_github_user_id": thread.resolved_by_github_user_id,
        "messages": [
            {
                "id": str(message.id),
                "author_github_user_id": message.author_github_user_id,
                "author_login": message.author_login,
                "body_markdown": message.body_markdown,
                "created_at": message.created_at.isoformat(),
            }
            for message in thread.messages
        ],
    }


def serialize_thread(thread: ReviewThread) -> dict[str, Any]:
    return _serialize_thread(thread)


def _normalize_markdown(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewWorkspaceValidationError("Markdown body must be non-empty")
    return value.strip()


def _coerce_int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _coerce_str_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _ensure_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


__all__ = [
    "ReviewWorkspaceError",
    "ReviewWorkspaceNotFoundError",
    "ReviewWorkspaceValidationError",
    "ThreadCounts",
    "add_thread_message",
    "anchors_match_for_carry_forward",
    "carry_forward_open_threads",
    "count_review_threads",
    "create_thread",
    "get_workspace_payload",
    "list_visible_threads_for_snapshot",
    "load_review_by_id",
    "load_review_by_route",
    "load_thread_by_id",
    "normalize_thread_anchor",
    "reopen_thread",
    "resolve_thread",
    "serialize_thread",
    "snapshot_contains_anchor",
]
