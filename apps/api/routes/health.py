"""Health routes for the managed API skeleton."""

from fastapi import APIRouter


router = APIRouter()


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Return a simple API health response."""
    return {"status": "ok"}
