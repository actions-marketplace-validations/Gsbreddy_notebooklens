"""GitHub webhook verification primitives for the managed API skeleton."""

from __future__ import annotations

import hashlib
import hmac


class GitHubWebhookVerificationError(ValueError):
    """Raised when a GitHub webhook signature is missing or invalid."""


def sign_github_webhook(secret: str, body: bytes) -> str:
    """Create the GitHub sha256 webhook signature for a payload body."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_github_webhook_signature(secret: str, body: bytes, signature_header: str | None) -> None:
    """Verify the GitHub sha256 signature header against the request body."""
    if not signature_header:
        raise GitHubWebhookVerificationError("Missing X-Hub-Signature-256 header")
    expected = sign_github_webhook(secret, body)
    if not hmac.compare_digest(expected, signature_header):
        raise GitHubWebhookVerificationError("Invalid GitHub webhook signature")
