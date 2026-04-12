"""GitHub-facing routes for the managed API skeleton."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse

from ..config import ApiSettings, get_settings
from ..webhooks import verify_github_webhook_signature


router = APIRouter(prefix="/api/github", tags=["github"])


@router.post("/webhooks")
async def receive_github_webhook(
    request: Request,
    settings: ApiSettings = Depends(get_settings),
    github_event: str | None = Header(default=None, alias="X-GitHub-Event"),
    delivery_id: str | None = Header(default=None, alias="X-GitHub-Delivery"),
    signature_header: str | None = Header(default=None, alias="X-Hub-Signature-256"),
) -> JSONResponse:
    """Verify GitHub webhook signatures and acknowledge receipt."""
    body = await request.body()
    verify_github_webhook_signature(settings.github_webhook_secret, body, signature_header)
    payload = json.loads(body.decode("utf-8") or "{}")
    return JSONResponse(
        status_code=202,
        content={
            "status": "accepted",
            "event": github_event,
            "delivery_id": delivery_id,
            "action": payload.get("action"),
        },
    )
