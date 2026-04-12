"""Review-route placeholders for the managed API skeleton."""

from fastapi import APIRouter, HTTPException


router = APIRouter(prefix="/api", tags=["reviews"])


def _not_implemented() -> None:
    raise HTTPException(status_code=501, detail="Managed review workspace endpoints are not implemented yet")


@router.get("/reviews/{owner}/{repo}/pulls/{pull_number}")
def get_review(owner: str, repo: str, pull_number: int) -> None:
    del owner, repo, pull_number
    _not_implemented()


@router.get("/reviews/{owner}/{repo}/pulls/{pull_number}/snapshots/{snapshot_index}")
def get_review_snapshot(owner: str, repo: str, pull_number: int, snapshot_index: int) -> None:
    del owner, repo, pull_number, snapshot_index
    _not_implemented()


@router.post("/reviews/{review_id}/threads")
def create_review_thread(review_id: str) -> None:
    del review_id
    _not_implemented()


@router.post("/threads/{thread_id}/messages")
def create_thread_message(thread_id: str) -> None:
    del thread_id
    _not_implemented()


@router.post("/threads/{thread_id}/resolve")
def resolve_thread(thread_id: str) -> None:
    del thread_id
    _not_implemented()


@router.post("/threads/{thread_id}/reopen")
def reopen_thread(thread_id: str) -> None:
    del thread_id
    _not_implemented()
