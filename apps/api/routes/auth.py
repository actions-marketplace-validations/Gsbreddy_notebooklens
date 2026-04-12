"""OAuth routes for the managed API skeleton."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..config import ApiSettings, get_settings
from ..database import get_db_session
from ..oauth import (
    GitHubOAuthClient,
    OAuthSessionStore,
    OAuthStateError,
    OAuthStateSigner,
    SESSION_COOKIE_NAME,
    STATE_COOKIE_NAME,
    SessionTokenCipher,
)


router = APIRouter(prefix="/api/auth", tags=["auth"])


def get_oauth_client() -> GitHubOAuthClient:
    return GitHubOAuthClient()


def get_state_signer(settings: ApiSettings = Depends(get_settings)) -> OAuthStateSigner:
    return OAuthStateSigner(settings.session_secret)


def get_session_store(settings: ApiSettings = Depends(get_settings)) -> OAuthSessionStore:
    return OAuthSessionStore(SessionTokenCipher(settings.session_secret))


@router.get("/github/login")
def github_login(
    next_path: str = Query(default="/"),
    settings: ApiSettings = Depends(get_settings),
    oauth_client: GitHubOAuthClient = Depends(get_oauth_client),
    signer: OAuthStateSigner = Depends(get_state_signer),
) -> RedirectResponse:
    """Start the GitHub OAuth login flow."""
    state = signer.issue_state(next_path=next_path)
    redirect_url = oauth_client.build_authorize_url(
        client_id=settings.github_oauth_client_id,
        redirect_uri=settings.github_oauth_callback_url,
        state=state,
    )
    response = RedirectResponse(redirect_url, status_code=302)
    response.set_cookie(
        STATE_COOKIE_NAME,
        state,
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=True,
        path="/",
    )
    return response


@router.get("/github/callback")
def github_callback(
    code: Annotated[str, Query()],
    state: Annotated[str, Query()],
    settings: ApiSettings = Depends(get_settings),
    db_session: Session = Depends(get_db_session),
    oauth_client: GitHubOAuthClient = Depends(get_oauth_client),
    signer: OAuthStateSigner = Depends(get_state_signer),
    session_store: OAuthSessionStore = Depends(get_session_store),
    state_cookie: str | None = Cookie(default=None, alias=STATE_COOKIE_NAME),
) -> RedirectResponse:
    """Complete the GitHub OAuth flow and persist a user session."""
    if state_cookie is None or state_cookie != state:
        raise HTTPException(status_code=400, detail="OAuth state cookie mismatch")
    try:
        payload = signer.verify_state(state)
    except OAuthStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    token = oauth_client.exchange_code(
        client_id=settings.github_oauth_client_id,
        client_secret=settings.github_oauth_client_secret,
        code=code,
        redirect_uri=settings.github_oauth_callback_url,
    )
    user = oauth_client.fetch_user(token.access_token)
    session_record = session_store.create_session(
        db_session,
        github_user=user,
        access_token=token.access_token,
        expires_at=token.expires_at,
    )
    db_session.commit()
    redirect_response = RedirectResponse(
        f"{settings.app_base_url}{payload['next_path']}",
        status_code=302,
    )
    redirect_response.delete_cookie(STATE_COOKIE_NAME, path="/")
    redirect_response.set_cookie(
        SESSION_COOKIE_NAME,
        str(session_record.id),
        max_age=max(0, int((token.expires_at - datetime.now(timezone.utc)).total_seconds())),
        httponly=True,
        samesite="lax",
        secure=True,
        path="/",
    )
    return redirect_response


@router.post("/logout", status_code=204)
def logout(
    response: Response,
    db_session: Session = Depends(get_db_session),
    session_store: OAuthSessionStore = Depends(get_session_store),
    session_cookie: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> Response:
    """Delete the current NotebookLens user session cookie and DB record."""
    if session_cookie:
        session_store.delete_session(db_session, session_cookie)
        db_session.commit()
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    response.status_code = 204
    return response
