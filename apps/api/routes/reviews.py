"""Managed review workspace routes for snapshots and inline threads."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..check_runs import sync_review_workspace_check_run
from ..config import ApiSettings, get_settings
from ..database import get_db_session
from ..job_runner import enqueue_snapshot_build_job
from ..managed_github import ManagedGitHubClient
from ..models import ManagedReviewStatus, ReviewThreadStatus
from ..oauth import GitHubOAuthClient, OAuthSessionStore
from ..review_workspace import (
    ReviewWorkspaceNotFoundError,
    ReviewWorkspaceValidationError,
    add_thread_message,
    create_thread,
    get_workspace_payload,
    load_review_by_id,
    load_review_by_route,
    load_thread_by_id,
    reopen_thread,
    resolve_thread,
    serialize_thread,
)
from .auth import (
    AuthenticatedUser,
    ensure_installation_admin,
    get_oauth_client,
    get_session_store,
    require_authenticated_user,
)
from .github import get_managed_github_client
from .repo_access import ensure_repo_access


router = APIRouter(prefix="/api", tags=["reviews"])


class CreateThreadRequest(BaseModel):
    snapshot_id: str
    anchor: dict[str, Any]
    body_markdown: str


class ThreadMessageRequest(BaseModel):
    body_markdown: str


@router.get("/reviews/{owner}/{repo}/pulls/{pull_number}")
def get_review(
    owner: str,
    repo: str,
    pull_number: int,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
    db_session: Session = Depends(get_db_session),
    oauth_client: GitHubOAuthClient = Depends(get_oauth_client),
) -> dict[str, Any]:
    ensure_repo_access(
        current_user=current_user,
        owner=owner,
        repo=repo,
        oauth_client=oauth_client,
    )
    try:
        review = load_review_by_route(
            db_session=db_session,
            owner=owner,
            repo=repo,
            pull_number=pull_number,
        )
    except ReviewWorkspaceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return get_workspace_payload(db_session=db_session, review=review)


@router.get("/reviews/{owner}/{repo}/pulls/{pull_number}/snapshots/{snapshot_index}")
def get_review_snapshot(
    owner: str,
    repo: str,
    pull_number: int,
    snapshot_index: int,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
    db_session: Session = Depends(get_db_session),
    oauth_client: GitHubOAuthClient = Depends(get_oauth_client),
) -> dict[str, Any]:
    ensure_repo_access(
        current_user=current_user,
        owner=owner,
        repo=repo,
        oauth_client=oauth_client,
    )
    try:
        review = load_review_by_route(
            db_session=db_session,
            owner=owner,
            repo=repo,
            pull_number=pull_number,
        )
        return get_workspace_payload(
            db_session=db_session,
            review=review,
            snapshot_index=snapshot_index,
        )
    except ReviewWorkspaceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/reviews/{review_id}/threads", status_code=status.HTTP_201_CREATED)
def create_review_thread(
    review_id: str,
    request: CreateThreadRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
    db_session: Session = Depends(get_db_session),
    settings: ApiSettings = Depends(get_settings),
    github_client: ManagedGitHubClient = Depends(get_managed_github_client),
    oauth_client: GitHubOAuthClient = Depends(get_oauth_client),
    session_store: OAuthSessionStore = Depends(get_session_store),
) -> dict[str, Any]:
    try:
        review = load_review_by_id(db_session=db_session, review_id=review_id)
        ensure_repo_access(
            current_user=current_user,
            owner=review.owner,
            repo=review.repo,
            oauth_client=oauth_client,
        )
        thread = create_thread(
            db_session=db_session,
            review=review,
            snapshot_id=request.snapshot_id,
            anchor=request.anchor,
            body_markdown=request.body_markdown,
            actor_github_user_id=current_user.github_user_id,
            actor_login=current_user.github_login,
            oauth_client=oauth_client,
            session_store=session_store,
        )
        sync_review_workspace_check_run(
            settings=settings,
            db_session=db_session,
            github_client=github_client,
            review=review,
            activity=_thread_activity(
                actor_login=current_user.github_login,
                notebook_path=_thread_notebook_path(thread),
                action="created a thread on",
            ),
        )
        db_session.commit()
        return {"thread": serialize_thread(thread)}
    except ReviewWorkspaceNotFoundError as exc:
        db_session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ReviewWorkspaceValidationError as exc:
        db_session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/reviews/{review_id}/rebuild-latest", status_code=status.HTTP_202_ACCEPTED)
def rebuild_latest_review_snapshot(
    review_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
    db_session: Session = Depends(get_db_session),
    settings: ApiSettings = Depends(get_settings),
    github_client: ManagedGitHubClient = Depends(get_managed_github_client),
    oauth_client: GitHubOAuthClient = Depends(get_oauth_client),
) -> dict[str, Any]:
    try:
        review = load_review_by_id(db_session=db_session, review_id=review_id)
        ensure_repo_access(
            current_user=current_user,
            owner=review.owner,
            repo=review.repo,
            oauth_client=oauth_client,
        )
        ensure_installation_admin(
            current_user=current_user,
            installation=review.installation_repository.installation,
            oauth_client=oauth_client,
        )
        job = enqueue_snapshot_build_job(
            db_session,
            managed_review_id=review.id,
            base_sha=review.latest_base_sha,
            head_sha=review.latest_head_sha,
            force_rebuild=True,
        )
        review.status = ManagedReviewStatus.PENDING
        review.latest_check_run_id = sync_review_workspace_check_run(
            settings=settings,
            db_session=db_session,
            github_client=github_client,
            review=review,
            activity="Snapshot rebuild queued for the latest push.",
        )
        db_session.commit()
        return {
            "status": "accepted",
            "review_id": str(review.id),
            "job_id": str(job.id),
            "force_rebuild": True,
            "check_run_id": review.latest_check_run_id,
        }
    except ReviewWorkspaceNotFoundError as exc:
        db_session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/threads/{thread_id}/messages", status_code=status.HTTP_201_CREATED)
def create_thread_message(
    thread_id: str,
    request: ThreadMessageRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
    db_session: Session = Depends(get_db_session),
    settings: ApiSettings = Depends(get_settings),
    github_client: ManagedGitHubClient = Depends(get_managed_github_client),
    oauth_client: GitHubOAuthClient = Depends(get_oauth_client),
    session_store: OAuthSessionStore = Depends(get_session_store),
) -> dict[str, Any]:
    try:
        thread = load_thread_by_id(db_session=db_session, thread_id=thread_id)
        review = thread.managed_review
        previous_status = thread.status
        ensure_repo_access(
            current_user=current_user,
            owner=review.owner,
            repo=review.repo,
            oauth_client=oauth_client,
        )
        updated_thread = add_thread_message(
            db_session=db_session,
            thread_id=thread_id,
            actor_github_user_id=current_user.github_user_id,
            actor_login=current_user.github_login,
            body_markdown=request.body_markdown,
            oauth_client=oauth_client,
            session_store=session_store,
        )
        sync_review_workspace_check_run(
            settings=settings,
            db_session=db_session,
            github_client=github_client,
            review=review,
            activity=_thread_activity(
                actor_login=current_user.github_login,
                notebook_path=_thread_notebook_path(updated_thread),
                action="replied to a thread on",
            ),
        )
        db_session.commit()
        return {"thread": serialize_thread(updated_thread)}
    except ReviewWorkspaceNotFoundError as exc:
        db_session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ReviewWorkspaceValidationError as exc:
        db_session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/threads/{thread_id}/resolve")
def resolve_thread_route(
    thread_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
    db_session: Session = Depends(get_db_session),
    settings: ApiSettings = Depends(get_settings),
    github_client: ManagedGitHubClient = Depends(get_managed_github_client),
    oauth_client: GitHubOAuthClient = Depends(get_oauth_client),
    session_store: OAuthSessionStore = Depends(get_session_store),
) -> dict[str, Any]:
    try:
        thread = load_thread_by_id(db_session=db_session, thread_id=thread_id)
        review = thread.managed_review
        previous_status = thread.status
        ensure_repo_access(
            current_user=current_user,
            owner=review.owner,
            repo=review.repo,
            oauth_client=oauth_client,
        )
        updated_thread = resolve_thread(
            db_session=db_session,
            thread_id=thread_id,
            actor_github_user_id=current_user.github_user_id,
            actor_login=current_user.github_login,
            oauth_client=oauth_client,
            session_store=session_store,
        )
        if previous_status != ReviewThreadStatus.RESOLVED:
            sync_review_workspace_check_run(
                settings=settings,
                db_session=db_session,
                github_client=github_client,
                review=review,
                activity=_thread_activity(
                    actor_login=current_user.github_login,
                    notebook_path=_thread_notebook_path(updated_thread),
                    action="resolved the thread on",
                ),
            )
        db_session.commit()
        return {"thread": serialize_thread(updated_thread)}
    except ReviewWorkspaceNotFoundError as exc:
        db_session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/threads/{thread_id}/reopen")
def reopen_thread_route(
    thread_id: str,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
    db_session: Session = Depends(get_db_session),
    settings: ApiSettings = Depends(get_settings),
    github_client: ManagedGitHubClient = Depends(get_managed_github_client),
    oauth_client: GitHubOAuthClient = Depends(get_oauth_client),
    session_store: OAuthSessionStore = Depends(get_session_store),
) -> dict[str, Any]:
    try:
        thread = load_thread_by_id(db_session=db_session, thread_id=thread_id)
        review = thread.managed_review
        previous_status = thread.status
        ensure_repo_access(
            current_user=current_user,
            owner=review.owner,
            repo=review.repo,
            oauth_client=oauth_client,
        )
        updated_thread = reopen_thread(
            db_session=db_session,
            thread_id=thread_id,
            actor_github_user_id=current_user.github_user_id,
            actor_login=current_user.github_login,
            oauth_client=oauth_client,
            session_store=session_store,
        )
        if previous_status == ReviewThreadStatus.RESOLVED:
            sync_review_workspace_check_run(
                settings=settings,
                db_session=db_session,
                github_client=github_client,
                review=review,
                activity=_thread_activity(
                    actor_login=current_user.github_login,
                    notebook_path=_thread_notebook_path(updated_thread),
                    action="reopened the thread on",
                ),
            )
        db_session.commit()
        return {"thread": serialize_thread(updated_thread)}
    except ReviewWorkspaceNotFoundError as exc:
        db_session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _thread_notebook_path(thread: Any) -> str:
    anchor = thread.anchor_json if isinstance(thread.anchor_json, dict) else {}
    value = anchor.get("notebook_path")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "the notebook diff"


def _thread_activity(*, actor_login: str, notebook_path: str, action: str) -> str:
    return f"{actor_login} {action} `{notebook_path}`."
