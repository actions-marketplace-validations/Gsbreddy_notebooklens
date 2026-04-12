"""GitHub OAuth and session primitives for the managed API skeleton."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
import json
import secrets
from typing import Any, Iterable
from urllib.parse import quote, urlencode
import uuid

from cryptography.fernet import Fernet, InvalidToken
import requests
from sqlalchemy.orm import Session

from .models import UserSession


DEFAULT_GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
DEFAULT_GITHUB_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
DEFAULT_GITHUB_API_URL = "https://api.github.com"
DEFAULT_GITHUB_OAUTH_SCOPES = ("read:user", "user:email", "repo")
DEFAULT_SESSION_TTL = timedelta(hours=8)
SESSION_COOKIE_NAME = "notebooklens_session"
STATE_COOKIE_NAME = "notebooklens_oauth_state"


class OAuthStateError(ValueError):
    """Raised when OAuth state tokens are invalid or expired."""


class SessionCipherError(ValueError):
    """Raised when encrypted session tokens cannot be decrypted."""


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _normalize_next_path(next_path: str | None) -> str:
    if not next_path:
        return "/"
    return next_path if next_path.startswith("/") else f"/{next_path}"


class OAuthStateSigner:
    """Issue and verify stateless signed OAuth state tokens."""

    def __init__(self, secret: str) -> None:
        self.secret = secret.encode("utf-8")

    def issue_state(
        self,
        *,
        next_path: str | None = None,
        now: datetime | None = None,
        ttl: timedelta = timedelta(minutes=10),
    ) -> str:
        issued_at = now or datetime.now(timezone.utc)
        payload = {
            "nonce": secrets.token_urlsafe(16),
            "next_path": _normalize_next_path(next_path),
            "issued_at": issued_at.isoformat(),
            "expires_at": (issued_at + ttl).isoformat(),
        }
        payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        payload_encoded = _b64encode(payload_bytes)
        signature = _b64encode(hmac.new(self.secret, payload_encoded.encode("ascii"), hashlib.sha256).digest())
        return f"{payload_encoded}.{signature}"

    def verify_state(self, token: str, *, now: datetime | None = None) -> dict[str, str]:
        try:
            payload_encoded, signature = token.split(".", 1)
        except ValueError as exc:
            raise OAuthStateError("Malformed OAuth state token") from exc
        expected_signature = _b64encode(
            hmac.new(self.secret, payload_encoded.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(expected_signature, signature):
            raise OAuthStateError("Invalid OAuth state signature")
        payload = json.loads(_b64decode(payload_encoded).decode("utf-8"))
        current_time = now or datetime.now(timezone.utc)
        expires_at = datetime.fromisoformat(payload["expires_at"])
        if expires_at < current_time:
            raise OAuthStateError("Expired OAuth state token")
        return payload


class SessionTokenCipher:
    """Encrypt and decrypt stored GitHub OAuth access tokens."""

    def __init__(self, secret: str) -> None:
        digest = hashlib.sha256(secret.encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def encrypt(self, access_token: str) -> str:
        return self._fernet.encrypt(access_token.encode("utf-8")).decode("utf-8")

    def decrypt(self, encrypted_token: str) -> str:
        try:
            return self._fernet.decrypt(encrypted_token.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise SessionCipherError("Unable to decrypt session token") from exc


@dataclass(frozen=True)
class GitHubOAuthUser:
    id: int
    login: str
    email: str | None = None


@dataclass(frozen=True)
class GitHubOAuthToken:
    access_token: str
    scope: str
    token_type: str
    expires_at: datetime


class GitHubOAuthClient:
    """Minimal GitHub OAuth client used by the managed API auth flow."""

    def __init__(
        self,
        *,
        authorize_url: str = DEFAULT_GITHUB_AUTHORIZE_URL,
        access_token_url: str = DEFAULT_GITHUB_ACCESS_TOKEN_URL,
        api_base_url: str = DEFAULT_GITHUB_API_URL,
        session: requests.Session | None = None,
    ) -> None:
        self.authorize_url = authorize_url
        self.access_token_url = access_token_url
        self.api_base_url = api_base_url.rstrip("/")
        self.session = session or requests.Session()

    def build_authorize_url(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        state: str,
        scopes: Iterable[str] = DEFAULT_GITHUB_OAUTH_SCOPES,
    ) -> str:
        query = urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": " ".join(scopes),
                "state": state,
            }
        )
        return f"{self.authorize_url}?{query}"

    def exchange_code(
        self,
        *,
        client_id: str,
        client_secret: str,
        code: str,
        redirect_uri: str,
        now: datetime | None = None,
    ) -> GitHubOAuthToken:
        response = self.session.post(
            self.access_token_url,
            headers={"Accept": "application/json"},
            json={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
            timeout=30,
        )
        if response.status_code >= 400:
            raise OAuthStateError(f"GitHub OAuth token exchange failed with status {response.status_code}")
        payload: dict[str, Any] = response.json()
        access_token = payload.get("access_token")
        if not access_token:
            raise OAuthStateError("GitHub OAuth token response was incomplete")
        return GitHubOAuthToken(
            access_token=access_token,
            scope=payload.get("scope", ""),
            token_type=payload.get("token_type", "bearer"),
            expires_at=(now or datetime.now(timezone.utc)) + DEFAULT_SESSION_TTL,
        )

    def fetch_user(self, access_token: str) -> GitHubOAuthUser:
        response = self.session.get(
            f"{self.api_base_url}/user",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {access_token}",
            },
            timeout=30,
        )
        if response.status_code >= 400:
            raise OAuthStateError(f"GitHub user lookup failed with status {response.status_code}")
        payload: dict[str, Any] = response.json()
        return GitHubOAuthUser(
            id=int(payload["id"]),
            login=payload["login"],
            email=payload.get("email"),
        )

    def can_access_repository(self, access_token: str, *, owner: str, repo: str) -> bool:
        response = self.session.get(
            f"{self.api_base_url}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {access_token}",
            },
            timeout=30,
        )
        if response.status_code in {200, 301, 302}:
            return True
        if response.status_code in {401, 403, 404}:
            return False
        raise OAuthStateError(
            f"GitHub repository access lookup failed with status {response.status_code}"
        )


class OAuthSessionStore:
    """Persist and manage encrypted NotebookLens GitHub OAuth sessions."""

    def __init__(self, cipher: SessionTokenCipher) -> None:
        self.cipher = cipher

    def create_session(
        self,
        db_session: Session,
        *,
        github_user: GitHubOAuthUser,
        access_token: str,
        expires_at: datetime,
    ) -> UserSession:
        record = UserSession(
            id=uuid.uuid4(),
            github_user_id=github_user.id,
            github_login=github_user.login,
            access_token_encrypted=self.cipher.encrypt(access_token),
            expires_at=expires_at,
        )
        db_session.add(record)
        db_session.flush()
        return record

    def get_session(self, db_session: Session, session_id: str | uuid.UUID) -> UserSession | None:
        try:
            session_uuid = uuid.UUID(str(session_id))
        except ValueError:
            return None
        return db_session.get(UserSession, session_uuid)

    def delete_session(self, db_session: Session, session_id: str | uuid.UUID) -> bool:
        record = self.get_session(db_session, session_id)
        if record is None:
            return False
        db_session.delete(record)
        db_session.flush()
        return True
