"""FastAPI entrypoint for the managed NotebookLens API."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src import __display_version__

from .config import ApiConfigurationError
from .managed_github import ManagedGitHubClientError
from .orchestration import ManagedWebhookPayloadError
from .routes.assets import router as assets_router
from .routes.auth import router as auth_router
from .routes.github import router as github_router
from .routes.health import router as health_router
from .routes.reviews import router as reviews_router
from .routes.settings import router as settings_router
from .webhooks import GitHubWebhookVerificationError


def create_app() -> FastAPI:
    """Create the managed NotebookLens FastAPI application."""
    app = FastAPI(title="NotebookLens Managed API", version=__display_version__)
    app.include_router(health_router)
    app.include_router(github_router)
    app.include_router(auth_router)
    app.include_router(reviews_router)
    app.include_router(assets_router)
    app.include_router(settings_router)

    @app.exception_handler(ApiConfigurationError)
    async def handle_configuration_error(_: Request, exc: ApiConfigurationError) -> JSONResponse:
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    @app.exception_handler(GitHubWebhookVerificationError)
    async def handle_webhook_error(_: Request, exc: GitHubWebhookVerificationError) -> JSONResponse:
        return JSONResponse(status_code=401, content={"detail": str(exc)})

    @app.exception_handler(ManagedWebhookPayloadError)
    async def handle_webhook_payload_error(_: Request, exc: ManagedWebhookPayloadError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(ManagedGitHubClientError)
    async def handle_managed_github_error(_: Request, exc: ManagedGitHubClientError) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    return app


app = create_app()
