"""Authenticated review-asset routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..database import get_db_session
from ..models import ReviewAsset, ReviewSnapshot
from ..oauth import GitHubOAuthClient
from .auth import AuthenticatedUser, get_oauth_client, require_authenticated_user
from .repo_access import ensure_repo_access


router = APIRouter(prefix="/api", tags=["review-assets"])


@router.get("/review-assets/{asset_id}")
def get_review_asset(
    asset_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
    db_session: Session = Depends(get_db_session),
    oauth_client: GitHubOAuthClient = Depends(get_oauth_client),
) -> Response:
    asset = db_session.execute(
        select(ReviewAsset)
        .options(
            joinedload(ReviewAsset.snapshot).joinedload(ReviewSnapshot.managed_review),
        )
        .where(ReviewAsset.id == asset_id)
    ).scalar_one_or_none()
    if asset is None:
        raise HTTPException(status_code=404, detail="Review asset not found")

    review = asset.snapshot.managed_review
    ensure_repo_access(
        current_user=current_user,
        owner=review.owner,
        repo=review.repo,
        oauth_client=oauth_client,
    )
    return Response(
        content=asset.content_bytes,
        media_type=asset.mime_type,
        headers={
            "Cache-Control": "private",
            "Content-Length": str(asset.byte_size),
            "X-Content-Type-Options": "nosniff",
        },
    )
