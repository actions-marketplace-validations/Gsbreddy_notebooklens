"""Health routes for the managed API skeleton."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ..config import ApiConfigurationError, get_settings
from ..database import get_engine


router = APIRouter()


@router.get("/healthz")
def healthz() -> JSONResponse:
    """Report API readiness for Compose health checks."""
    try:
        settings = get_settings()
    except ApiConfigurationError as exc:
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "checks": {
                    "config": {"status": "error", "detail": str(exc)},
                    "database": {"status": "unknown"},
                },
            },
        )

    try:
        with get_engine(settings.database_url).connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "checks": {
                    "config": {"status": "ok"},
                    "database": {"status": "error", "detail": str(exc.__class__.__name__)},
                },
            },
        )

    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "checks": {
                "config": {"status": "ok"},
                "database": {"status": "ok"},
            },
        },
    )
