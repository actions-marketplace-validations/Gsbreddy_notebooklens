"""Managed review workspace routes for snapshots and inline threads."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db_session
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
from .auth import AuthenticatedUser, get_oauth_client, get_session_store, require_authenticated_user


router = APIRouter(prefix="/api", tags=["reviews"])
_ACCESS_CACHE_TTL = timedelta(minutes=5)
_REPO_ACCESS_CACHE: dict[tuple[int, str], tuple[datetime, bool]] = {}


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
    _ensure_repo_access(
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
    _ensure_repo_access(
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
    oauth_client: GitHubOAuthClient = Depends(get_oauth_client),
    session_store: OAuthSessionStore = Depends(get_session_store),
) -> dict[str, Any]:
    try:
        review = load_review_by_id(db_session=db_session, review_id=review_id)
        _ensure_repo_access(
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
        db_session.commit()
        return {"thread": serialize_thread(thread)}
    except ReviewWorkspaceNotFoundError as exc:
        db_session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ReviewWorkspaceValidationError as exc:
        db_session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/threads/{thread_id}/messages", status_code=status.HTTP_201_CREATED)
def create_thread_message(
    thread_id: str,
    request: ThreadMessageRequest,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
    db_session: Session = Depends(get_db_session),
    oauth_client: GitHubOAuthClient = Depends(get_oauth_client),
    session_store: OAuthSessionStore = Depends(get_session_store),
) -> dict[str, Any]:
    try:
        thread = load_thread_by_id(db_session=db_session, thread_id=thread_id)
        review = thread.managed_review
        _ensure_repo_access(
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
    oauth_client: GitHubOAuthClient = Depends(get_oauth_client),
    session_store: OAuthSessionStore = Depends(get_session_store),
) -> dict[str, Any]:
    try:
        thread = load_thread_by_id(db_session=db_session, thread_id=thread_id)
        review = thread.managed_review
        _ensure_repo_access(
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
    oauth_client: GitHubOAuthClient = Depends(get_oauth_client),
    session_store: OAuthSessionStore = Depends(get_session_store),
) -> dict[str, Any]:
    try:
        thread = load_thread_by_id(db_session=db_session, thread_id=thread_id)
        review = thread.managed_review
        _ensure_repo_access(
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
        db_session.commit()
        return {"thread": serialize_thread(updated_thread)}
    except ReviewWorkspaceNotFoundError as exc:
        db_session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _ensure_repo_access(
    *,
    current_user: AuthenticatedUser,
    owner: str,
    repo: str,
    oauth_client: GitHubOAuthClient,
) -> None:
    cache_key = (current_user.github_user_id, f"{owner}/{repo}")
    now = datetime.now(timezone.utc)
    cached = _REPO_ACCESS_CACHE.get(cache_key)
    if cached is not None and cached[0] >= now:
        allowed = cached[1]
    else:
        allowed = oauth_client.can_access_repository(
            current_user.access_token,
            owner=owner,
            repo=repo,
        )
        _REPO_ACCESS_CACHE[cache_key] = (now + _ACCESS_CACHE_TTL, allowed)
    if not allowed:
        raise HTTPException(status_code=403, detail="Repository access denied")
