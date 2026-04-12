"""Managed snapshot worker entrypoints."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import difflib
import json
import re
import uuid

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .check_runs import build_review_url
from .config import ApiSettings, get_settings
from .database import session_scope
from .managed_github import ManagedComment, ManagedGitHubClient, ManagedGitHubClientError
from .models import (
    GitHubMirrorAction,
    GitHubMirrorJob,
    GitHubMirrorState,
    InstallationRepository,
    ManagedReview,
    ReviewSnapshot,
    ReviewThread,
    ThreadMessage,
)
from .notification_delivery import (
    NotificationDeliveryResult,
    ResendEmailClient,
    build_notification_email_client,
    deliver_pending_notifications,
)
from .oauth import OAuthSessionStore, SessionTokenCipher
from .orchestration import (
    LiteLLMGatewayClient,
    SnapshotBuildResult,
    run_snapshot_build_worker_once,
)
from .review_workspace import (
    claim_next_github_mirror_job,
    mark_github_mirror_job_failed,
    mark_github_mirror_job_sent,
    resolve_mirror_auth_context,
)


_UNIFIED_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_WORKSPACE_COMMENT_MARKER = "<!-- notebooklens-managed-workspace -->"


@dataclass(frozen=True)
class RetentionCleanupResult:
    purged_reviews: int


@dataclass(frozen=True)
class GitHubMirrorResult:
    status: str
    job_id: uuid.UUID | None
    managed_review_id: uuid.UUID | None
    thread_id: uuid.UUID | None
    action: str | None


@dataclass(frozen=True)
class ReviewCommentAnchor:
    commit_id: str
    path: str
    line: int
    side: str = "RIGHT"


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


def process_github_mirror_job_once(
    *,
    settings: ApiSettings | None = None,
    github_client: ManagedGitHubClient | None = None,
) -> GitHubMirrorResult:
    """Process one pending GitHub mirror job for hosted thread sync."""
    resolved_settings = settings or get_settings()
    resolved_github_client = github_client or ManagedGitHubClient()
    session_store = OAuthSessionStore(SessionTokenCipher(resolved_settings.session_secret))
    with session_scope(resolved_settings) as db_session:
        purge_expired_managed_review_data(
            settings=resolved_settings,
            db_session=db_session,
        )
        claimed_job = claim_next_github_mirror_job(db_session=db_session)
        if claimed_job is None:
            return GitHubMirrorResult(
                status="idle",
                job_id=None,
                managed_review_id=None,
                thread_id=None,
                action=None,
            )

        job = _load_github_mirror_job(db_session=db_session, job_id=claimed_job.id)
        try:
            _process_github_mirror_job(
                settings=resolved_settings,
                db_session=db_session,
                github_client=resolved_github_client,
                session_store=session_store,
                job=job,
            )
            mark_github_mirror_job_sent(db_session=db_session, job=job)
            return GitHubMirrorResult(
                status="sent",
                job_id=job.id,
                managed_review_id=job.managed_review_id,
                thread_id=job.thread_id,
                action=job.action.value,
            )
        except Exception as exc:
            error_message = _truncate_error(exc)
            if job.thread is not None:
                _record_thread_mirror_state(
                    job.thread,
                    state=GitHubMirrorState.FAILED,
                    mode="app",
                    fallback_reason=None,
                    target="github_review_comment",
                    action=job.action.value,
                    last_error=error_message,
                    mirrored_at=None,
                )
                try:
                    _sync_workspace_comment(
                        settings=resolved_settings,
                        db_session=db_session,
                        github_client=resolved_github_client,
                        review=job.managed_review,
                    )
                except Exception:
                    pass
            mark_github_mirror_job_failed(
                db_session=db_session,
                job=job,
                error_message=error_message,
            )
            return GitHubMirrorResult(
                status="failed",
                job_id=job.id,
                managed_review_id=job.managed_review_id,
                thread_id=job.thread_id,
                action=job.action.value,
            )


def _load_github_mirror_job(*, db_session, job_id: uuid.UUID) -> GitHubMirrorJob:
    return db_session.execute(
        select(GitHubMirrorJob)
        .options(
            selectinload(GitHubMirrorJob.managed_review)
            .selectinload(ManagedReview.installation_repository)
            .selectinload(InstallationRepository.installation),
            selectinload(GitHubMirrorJob.managed_review).selectinload(ManagedReview.review_snapshots),
            selectinload(GitHubMirrorJob.managed_review)
            .selectinload(ManagedReview.review_threads)
            .selectinload(ReviewThread.messages),
            selectinload(GitHubMirrorJob.thread).selectinload(ReviewThread.origin_snapshot),
            selectinload(GitHubMirrorJob.thread).selectinload(ReviewThread.current_snapshot),
            selectinload(GitHubMirrorJob.thread).selectinload(ReviewThread.messages),
            selectinload(GitHubMirrorJob.thread_message),
        )
        .where(GitHubMirrorJob.id == job_id)
    ).scalar_one()


def _process_github_mirror_job(
    *,
    settings: ApiSettings,
    db_session,
    github_client: ManagedGitHubClient,
    session_store: OAuthSessionStore,
    job: GitHubMirrorJob,
) -> None:
    if job.action == GitHubMirrorAction.UPSERT_WORKSPACE_COMMENT:
        _sync_workspace_comment(
            settings=settings,
            db_session=db_session,
            github_client=github_client,
            review=job.managed_review,
        )
        return

    if job.thread is None:
        raise ValueError("GitHub mirror job is missing its thread")

    if job.action == GitHubMirrorAction.CREATE_THREAD:
        _mirror_thread_create(
            settings=settings,
            db_session=db_session,
            github_client=github_client,
            session_store=session_store,
            review=job.managed_review,
            thread=job.thread,
            message=job.thread_message,
        )
        return
    if job.action == GitHubMirrorAction.REPLY:
        _mirror_thread_reply(
            settings=settings,
            db_session=db_session,
            github_client=github_client,
            session_store=session_store,
            review=job.managed_review,
            thread=job.thread,
            message=job.thread_message,
        )
        return
    if job.action == GitHubMirrorAction.RESOLVE:
        _mirror_thread_state_reply(
            settings=settings,
            db_session=db_session,
            github_client=github_client,
            review=job.managed_review,
            thread=job.thread,
            action="resolved",
        )
        return
    if job.action == GitHubMirrorAction.REOPEN:
        _mirror_thread_state_reply(
            settings=settings,
            db_session=db_session,
            github_client=github_client,
            review=job.managed_review,
            thread=job.thread,
            action="reopened",
        )
        return
    raise ValueError(f"Unsupported GitHub mirror action: {job.action.value}")


def _mirror_thread_create(
    *,
    settings: ApiSettings,
    db_session,
    github_client: ManagedGitHubClient,
    session_store: OAuthSessionStore,
    review: ManagedReview,
    thread: ReviewThread,
    message: ThreadMessage | None,
) -> None:
    if message is None:
        raise ValueError("Create-thread mirror job is missing the root message")

    anchor = _resolve_review_comment_anchor(
        settings=settings,
        github_client=github_client,
        review=review,
        thread=thread,
    )
    if anchor is None:
        _record_thread_mirror_state(
            thread,
            state=GitHubMirrorState.SKIPPED,
            mode="app",
            fallback_reason="unmappable_anchor",
            target="workspace_fallback",
            action="create_thread",
            last_error=None,
            mirrored_at=datetime.now(timezone.utc),
        )
        _sync_workspace_comment(
            settings=settings,
            db_session=db_session,
            github_client=github_client,
            review=review,
        )
        return

    auth_context = resolve_mirror_auth_context(
        db_session=db_session,
        github_user_id=message.author_github_user_id,
        session_store=session_store,
    )
    comment_body = _render_thread_message_comment(
        settings=settings,
        review=review,
        thread=thread,
        body_markdown=message.body_markdown,
    )
    comment, mode, fallback_reason = _write_with_auth_fallback(
        auth_context=auth_context,
        write_with_token=lambda token: github_client.upsert_review_comment(
            settings=settings,
            installation_id=review.installation_repository.installation.github_installation_id,
            repository=review.installation_repository.full_name,
            pull_number=review.pull_number,
            commit_id=anchor.commit_id,
            path=anchor.path,
            line=anchor.line,
            side=anchor.side,
            body=comment_body,
            comment_id=thread.github_root_comment_id,
            access_token=token,
        ),
    )
    thread.github_root_comment_id = comment.comment_id
    thread.github_root_comment_url = comment.html_url or _review_comment_url(review=review, comment_id=comment.comment_id)
    _record_thread_mirror_state(
        thread,
        state=GitHubMirrorState.MIRRORED,
        mode=mode,
        fallback_reason=fallback_reason,
        target="github_review_comment",
        action="create_thread",
        last_error=None,
        mirrored_at=datetime.now(timezone.utc),
    )
    _sync_workspace_comment(
        settings=settings,
        db_session=db_session,
        github_client=github_client,
        review=review,
    )


def _mirror_thread_reply(
    *,
    settings: ApiSettings,
    db_session,
    github_client: ManagedGitHubClient,
    session_store: OAuthSessionStore,
    review: ManagedReview,
    thread: ReviewThread,
    message: ThreadMessage | None,
) -> None:
    if message is None:
        raise ValueError("Reply mirror job is missing the reply message")

    if thread.github_root_comment_id is None:
        _record_thread_mirror_state(
            thread,
            state=GitHubMirrorState.SKIPPED,
            mode="app",
            fallback_reason="unmappable_anchor",
            target="workspace_fallback",
            action="reply",
            last_error=None,
            mirrored_at=datetime.now(timezone.utc),
        )
        _sync_workspace_comment(
            settings=settings,
            db_session=db_session,
            github_client=github_client,
            review=review,
        )
        return

    auth_context = resolve_mirror_auth_context(
        db_session=db_session,
        github_user_id=message.author_github_user_id,
        session_store=session_store,
    )
    comment_body = _render_thread_message_comment(
        settings=settings,
        review=review,
        thread=thread,
        body_markdown=message.body_markdown,
    )
    comment, mode, fallback_reason = _write_with_auth_fallback(
        auth_context=auth_context,
        write_with_token=lambda token: _upsert_reply_comment(
            settings=settings,
            github_client=github_client,
            review=review,
            root_comment_id=thread.github_root_comment_id,
            reply_comment_id=message.github_reply_comment_id,
            body=comment_body,
            access_token=token,
        ),
    )
    message.github_reply_comment_id = comment.comment_id
    message.github_reply_comment_url = comment.html_url or _review_comment_url(review=review, comment_id=comment.comment_id)
    _record_thread_mirror_state(
        thread,
        state=GitHubMirrorState.MIRRORED,
        mode=mode,
        fallback_reason=fallback_reason,
        target="github_review_comment",
        action="reply",
        last_error=None,
        mirrored_at=datetime.now(timezone.utc),
    )
    _sync_workspace_comment(
        settings=settings,
        db_session=db_session,
        github_client=github_client,
        review=review,
    )


def _mirror_thread_state_reply(
    *,
    settings: ApiSettings,
    db_session,
    github_client: ManagedGitHubClient,
    review: ManagedReview,
    thread: ReviewThread,
    action: str,
) -> None:
    if thread.github_root_comment_id is None:
        _record_thread_mirror_state(
            thread,
            state=GitHubMirrorState.SKIPPED,
            mode="app",
            fallback_reason="unmappable_anchor",
            target="workspace_fallback",
            action=action,
            last_error=None,
            mirrored_at=datetime.now(timezone.utc),
        )
        _sync_workspace_comment(
            settings=settings,
            db_session=db_session,
            github_client=github_client,
            review=review,
        )
        return

    github_client.create_review_comment_reply(
        settings=settings,
        installation_id=review.installation_repository.installation.github_installation_id,
        repository=review.installation_repository.full_name,
        pull_number=review.pull_number,
        comment_id=thread.github_root_comment_id,
        body=_render_thread_state_comment(settings=settings, review=review, thread=thread, action=action),
    )
    _record_thread_mirror_state(
        thread,
        state=GitHubMirrorState.MIRRORED,
        mode="app",
        fallback_reason=None,
        target="github_review_comment",
        action=action,
        last_error=None,
        mirrored_at=datetime.now(timezone.utc),
    )
    _sync_workspace_comment(
        settings=settings,
        db_session=db_session,
        github_client=github_client,
        review=review,
    )


def _upsert_reply_comment(
    *,
    settings: ApiSettings,
    github_client: ManagedGitHubClient,
    review: ManagedReview,
    root_comment_id: int,
    reply_comment_id: int | None,
    body: str,
    access_token: str | None,
) -> ManagedComment:
    if reply_comment_id is not None:
        try:
            return github_client.update_review_comment(
                settings=settings,
                installation_id=review.installation_repository.installation.github_installation_id,
                repository=review.installation_repository.full_name,
                comment_id=reply_comment_id,
                body=body,
                access_token=access_token,
            )
        except ManagedGitHubClientError as exc:
            if exc.status_code != 404:
                raise
    return github_client.create_review_comment_reply(
        settings=settings,
        installation_id=review.installation_repository.installation.github_installation_id,
        repository=review.installation_repository.full_name,
        pull_number=review.pull_number,
        comment_id=root_comment_id,
        body=body,
        access_token=access_token,
    )


def _write_with_auth_fallback(
    *,
    auth_context,
    write_with_token,
) -> tuple[ManagedComment, str, str | None]:
    if auth_context.mode == "user" and auth_context.access_token is not None:
        try:
            comment = write_with_token(auth_context.access_token)
            return comment, "user", None
        except ManagedGitHubClientError as exc:
            if exc.status_code not in {401, 403}:
                raise
            comment = write_with_token(None)
            return comment, "app", "user_token_write_failed"

    comment = write_with_token(None)
    return comment, "app", auth_context.fallback_reason


def _sync_workspace_comment(
    *,
    settings: ApiSettings,
    db_session,
    github_client: ManagedGitHubClient,
    review: ManagedReview,
) -> None:
    hydrated_review = db_session.execute(
        select(ManagedReview)
        .options(
            selectinload(ManagedReview.installation_repository).selectinload(
                InstallationRepository.installation
            ),
            selectinload(ManagedReview.review_threads).selectinload(ReviewThread.messages),
            selectinload(ManagedReview.review_snapshots),
        )
        .where(ManagedReview.id == review.id)
    ).scalar_one()
    comment = github_client.upsert_issue_comment(
        settings=settings,
        installation_id=hydrated_review.installation_repository.installation.github_installation_id,
        repository=hydrated_review.installation_repository.full_name,
        pull_number=hydrated_review.pull_number,
        body=_render_workspace_comment(settings=settings, review=hydrated_review),
        comment_id=hydrated_review.github_workspace_comment_id,
    )
    hydrated_review.github_workspace_comment_id = comment.comment_id
    hydrated_review.github_workspace_comment_url = comment.html_url or _issue_comment_url(
        review=hydrated_review,
        comment_id=comment.comment_id,
    )
    db_session.flush()


def _render_workspace_comment(*, settings: ApiSettings, review: ManagedReview) -> str:
    review_url = build_review_url(settings=settings, review=review)
    latest_snapshot_index = max(
        (snapshot.snapshot_index for snapshot in review.review_snapshots),
        default=None,
    )
    mirror_counts = Counter(thread.github_mirror_state.value for thread in review.review_threads)
    lines = [
        _WORKSPACE_COMMENT_MARKER,
        "## NotebookLens Review Workspace",
        "",
        f"[Open in NotebookLens]({review_url})",
        "",
        (
            f"Latest snapshot state: `{review.status.value}`"
            + (
                f" (v{latest_snapshot_index})"
                if latest_snapshot_index is not None
                else ""
            )
        ),
        (
            "Thread sync: "
            f"{mirror_counts.get('mirrored', 0)} mirrored, "
            f"{mirror_counts.get('skipped', 0)} fallback, "
            f"{mirror_counts.get('pending', 0)} pending, "
            f"{mirror_counts.get('failed', 0)} failed"
        ),
        (
            "Inline discussion is mirrored to GitHub when stable `.ipynb` diff anchors exist, "
            "but NotebookLens remains the editable source of truth."
        ),
        "",
        "### Fallback Threads",
        "",
    ]
    fallback_threads = [
        thread
        for thread in sorted(review.review_threads, key=lambda item: item.created_at)
        if thread.github_mirror_state == GitHubMirrorState.SKIPPED and thread.github_root_comment_id is None
    ]
    if not fallback_threads:
        lines.append("None.")
        return "\n".join(lines)

    for thread in fallback_threads:
        thread_url = _thread_url(settings=settings, review=review, thread=thread)
        lines.extend(
            [
                f"#### `{_thread_notebook_path(thread)}` · {_thread_block_kind(thread)} · {thread.status.value}",
                f"Hosted thread: [Open thread]({thread_url})",
                f"Context: {_thread_anchor_context(thread)}",
                "",
            ]
        )
        for message in thread.messages:
            lines.append(f"**{message.author_login}**")
            lines.append("")
            lines.append(message.body_markdown)
            lines.append("")
    return "\n".join(lines).rstrip()


def _render_thread_message_comment(
    *,
    settings: ApiSettings,
    review: ManagedReview,
    thread: ReviewThread,
    body_markdown: str,
) -> str:
    return (
        f"{body_markdown.strip()}\n\n"
        f"_Mirrored from NotebookLens. Continue editing in "
        f"[NotebookLens]({_thread_url(settings=settings, review=review, thread=thread)})._"
    )


def _render_thread_state_comment(
    *,
    settings: ApiSettings,
    review: ManagedReview,
    thread: ReviewThread,
    action: str,
) -> str:
    return (
        f"NotebookLens {action} this thread in the hosted workspace. "
        f"[Open thread]({_thread_url(settings=settings, review=review, thread=thread)})."
    )


def _thread_url(*, settings: ApiSettings, review: ManagedReview, thread: ReviewThread) -> str:
    return f"{build_review_url(settings=settings, review=review)}#thread-{thread.id}"


def _thread_notebook_path(thread: ReviewThread) -> str:
    anchor = thread.anchor_json if isinstance(thread.anchor_json, dict) else {}
    notebook_path = anchor.get("notebook_path")
    if isinstance(notebook_path, str) and notebook_path.strip():
        return notebook_path.strip()
    return "unknown-notebook.ipynb"


def _thread_block_kind(thread: ReviewThread) -> str:
    anchor = thread.anchor_json if isinstance(thread.anchor_json, dict) else {}
    block_kind = anchor.get("block_kind")
    if isinstance(block_kind, str) and block_kind.strip():
        return block_kind.strip()
    return "unknown"


def _thread_anchor_context(thread: ReviewThread) -> str:
    anchor = thread.anchor_json if isinstance(thread.anchor_json, dict) else {}
    locator = anchor.get("cell_locator") if isinstance(anchor.get("cell_locator"), dict) else {}
    cell_id = locator.get("cell_id")
    display_index = locator.get("display_index")
    if isinstance(cell_id, str) and cell_id.strip():
        return f"cell `{cell_id.strip()}`"
    if isinstance(display_index, int):
        return f"cell index {display_index}"
    return "cell context unavailable"


def _issue_comment_url(*, review: ManagedReview, comment_id: int) -> str:
    return f"{review.github_web_base_url}/{review.owner}/{review.repo}/pull/{review.pull_number}#issuecomment-{comment_id}"


def _review_comment_url(*, review: ManagedReview, comment_id: int) -> str:
    return f"{review.github_web_base_url}/{review.owner}/{review.repo}/pull/{review.pull_number}#discussion_r{comment_id}"


def _record_thread_mirror_state(
    thread: ReviewThread,
    *,
    state: GitHubMirrorState,
    mode: str,
    fallback_reason: str | None,
    target: str,
    action: str,
    last_error: str | None,
    mirrored_at: datetime | None,
) -> None:
    metadata = dict(thread.github_mirror_metadata_json) if isinstance(thread.github_mirror_metadata_json, dict) else {}
    metadata["mode"] = mode
    metadata["fallback_reason"] = fallback_reason
    metadata["target"] = target
    metadata["last_action"] = action
    if last_error:
        metadata["last_error"] = last_error
    else:
        metadata.pop("last_error", None)
    thread.github_mirror_metadata_json = metadata
    thread.github_mirror_state = state
    if mirrored_at is not None:
        thread.github_last_mirrored_at = mirrored_at


def _resolve_review_comment_anchor(
    *,
    settings: ApiSettings,
    github_client: ManagedGitHubClient,
    review: ManagedReview,
    thread: ReviewThread,
) -> ReviewCommentAnchor | None:
    anchor = thread.origin_anchor_json if isinstance(thread.origin_anchor_json, dict) else {}
    if anchor.get("block_kind") == "metadata":
        return None

    snapshot = thread.origin_snapshot
    notebook_path = anchor.get("notebook_path")
    if not isinstance(notebook_path, str) or not notebook_path.strip():
        return None

    installation_id = review.installation_repository.installation.github_installation_id
    repository = review.installation_repository.full_name
    base_content = github_client.get_file_content(
        settings=settings,
        installation_id=installation_id,
        repository=repository,
        path=notebook_path,
        ref=snapshot.base_sha,
    )
    head_content = github_client.get_file_content(
        settings=settings,
        installation_id=installation_id,
        repository=repository,
        path=notebook_path,
        ref=snapshot.head_sha,
    )
    if not isinstance(head_content, str) or not head_content.strip():
        return None

    cell = _find_anchor_cell(head_content=head_content, anchor=anchor)
    if cell is None:
        return None

    candidate_fragments = _candidate_fragments_for_anchor(cell=cell, block_kind=str(anchor.get("block_kind")))
    if not candidate_fragments:
        return None

    added_lines = _collect_added_head_lines(base_content=base_content or "", head_content=head_content)
    if not added_lines:
        return None
    cell_line = _find_cell_id_line(head_content=head_content, anchor=anchor)

    matches: list[tuple[int, int]] = []
    for line_number, content in added_lines:
        if any(fragment in content for fragment in candidate_fragments):
            score = abs(line_number - cell_line) if cell_line is not None else 0
            matches.append((score, line_number))
    if not matches:
        return None
    matches.sort()
    return ReviewCommentAnchor(
        commit_id=snapshot.head_sha,
        path=notebook_path.strip(),
        line=matches[0][1],
    )


def _find_anchor_cell(*, head_content: str, anchor: dict) -> dict | None:
    try:
        notebook = json.loads(head_content)
    except json.JSONDecodeError:
        return None
    cells = notebook.get("cells")
    if not isinstance(cells, list):
        return None
    locator = anchor.get("cell_locator") if isinstance(anchor.get("cell_locator"), dict) else {}
    cell_id = locator.get("cell_id")
    if isinstance(cell_id, str):
        for cell in cells:
            if isinstance(cell, dict) and cell.get("id") == cell_id:
                return cell
    for key in ("head_index", "display_index", "base_index"):
        index = locator.get(key)
        if isinstance(index, int) and 0 <= index < len(cells):
            candidate = cells[index]
            if isinstance(candidate, dict):
                return candidate
    return None


def _candidate_fragments_for_anchor(*, cell: dict, block_kind: str) -> tuple[str, ...]:
    if block_kind == "source":
        return _flatten_json_fragments(cell.get("source"))
    if block_kind == "outputs":
        return _flatten_json_fragments(cell.get("outputs"))
    return ()


def _flatten_json_fragments(payload) -> tuple[str, ...]:
    fragments: list[str] = []

    def visit(value) -> None:
        if isinstance(value, str):
            fragments.append(json.dumps(value))
            return
        if value is None or isinstance(value, (bool, int, float)):
            fragments.append(json.dumps(value))
            return
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if isinstance(value, dict):
            for nested_value in value.values():
                visit(nested_value)

    visit(payload)
    deduped: list[str] = []
    seen: set[str] = set()
    for fragment in fragments:
        normalized = str(fragment).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return tuple(deduped)


def _collect_added_head_lines(*, base_content: str, head_content: str) -> list[tuple[int, str]]:
    base_lines = base_content.splitlines(keepends=True)
    head_lines = head_content.splitlines(keepends=True)
    head_line_number = 0
    added_lines: list[tuple[int, str]] = []
    for line in difflib.unified_diff(base_lines, head_lines, lineterm=""):
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("@@"):
            match = _UNIFIED_HUNK_RE.match(line)
            if match is None:
                continue
            head_line_number = int(match.group(1))
            continue
        if line.startswith("+"):
            added_lines.append((head_line_number, line[1:]))
            head_line_number += 1
            continue
        if line.startswith(" "):
            head_line_number += 1
    return added_lines


def _find_cell_id_line(*, head_content: str, anchor: dict) -> int | None:
    locator = anchor.get("cell_locator") if isinstance(anchor.get("cell_locator"), dict) else {}
    cell_id = locator.get("cell_id")
    if not isinstance(cell_id, str) or not cell_id.strip():
        return None
    needle = f'"id": {json.dumps(cell_id.strip())}'
    for line_number, line in enumerate(head_content.splitlines(), start=1):
        if needle in line:
            return line_number
    return None


def _truncate_error(exc: BaseException) -> str:
    message = str(exc).strip()
    return message[:300] if message else exc.__class__.__name__


__all__ = [
    "GitHubMirrorResult",
    "RetentionCleanupResult",
    "process_github_mirror_job_once",
    "process_notification_delivery_once",
    "process_retention_cleanup_once",
    "process_snapshot_build_job_once",
    "purge_expired_managed_review_data",
]
