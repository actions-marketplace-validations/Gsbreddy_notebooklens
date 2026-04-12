"""GitHub-facing routes for managed webhook ingestion."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..config import ApiSettings, get_settings
from ..database import get_db_session
from ..managed_github import ManagedGitHubClient
from ..orchestration import ingest_pull_request_webhook
from ..webhooks import verify_github_webhook_signature


router = APIRouter(prefix="/api/github", tags=["github"])


def get_managed_github_client() -> ManagedGitHubClient:
    return ManagedGitHubClient()


@router.post("/webhooks")
async def receive_github_webhook(
    request: Request,
    settings: ApiSettings = Depends(get_settings),
    db_session: Session = Depends(get_db_session),
    github_client: ManagedGitHubClient = Depends(get_managed_github_client),
    github_event: str | None = Header(default=None, alias="X-GitHub-Event"),
    delivery_id: str | None = Header(default=None, alias="X-GitHub-Delivery"),
    signature_header: str | None = Header(default=None, alias="X-Hub-Signature-256"),
) -> JSONResponse:
    """Verify the webhook signature and queue managed snapshot work when applicable."""
    body = await request.body()
    verify_github_webhook_signature(settings.github_webhook_secret, body, signature_header)
    payload = json.loads(body.decode("utf-8") or "{}")
    if not settings.managed_review_beta_enabled:
        return JSONResponse(
            status_code=202,
            content={
                "status": "ignored",
                "reason": "managed review beta disabled",
                "event": github_event,
                "delivery_id": delivery_id,
                "action": payload.get("action"),
            },
        )

    result = ingest_pull_request_webhook(
        db_session=db_session,
        settings=settings,
        github_client=github_client,
        github_event=github_event,
        payload=payload,
    )
    db_session.commit()
    return JSONResponse(
        status_code=202,
        content={
            "status": "accepted" if result.accepted else "ignored",
            "reason": result.reason,
            "event": github_event,
            "delivery_id": delivery_id,
            "action": result.action or payload.get("action"),
            "managed_review_id": str(result.managed_review_id) if result.managed_review_id else None,
            "job_id": str(result.job_id) if result.job_id else None,
            "check_run_id": result.check_run_id,
        },
    )
