from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import jwt
from sqlalchemy import select

from apps.api.config import ApiSettings, get_settings, reset_settings_cache
from apps.api.database import create_all_tables, get_engine, reset_engine_cache, session_scope
from apps.api.github_app import GitHubAppClient, build_github_app_jwt, build_github_app_headers
from apps.api.job_runner import (
    claim_next_snapshot_build_job,
    enqueue_snapshot_build_job,
    mark_snapshot_build_job_failed,
    mark_snapshot_build_job_retryable_failed,
    mark_snapshot_build_job_succeeded,
)
from apps.api.main import create_app
from apps.api.models import (
    Base,
    GitHubInstallation,
    InstallationAccountType,
    InstallationRepository,
    ManagedReview,
    SnapshotBuildJobStatus,
    UserSession,
)
from apps.api.oauth import (
    GitHubOAuthToken,
    GitHubOAuthUser,
    OAuthSessionStore,
    OAuthStateSigner,
    SESSION_COOKIE_NAME,
    STATE_COOKIE_NAME,
    SessionTokenCipher,
)
from apps.api.routes.auth import get_oauth_client
from apps.api.webhooks import GitHubWebhookVerificationError, sign_github_webhook, verify_github_webhook_signature


def _generate_private_key() -> str:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode("utf-8")


TEST_PRIVATE_KEY = _generate_private_key()


class FakeOAuthClient:
    def __init__(self) -> None:
        self.exchange_calls: list[dict[str, Any]] = []

    def build_authorize_url(self, *, client_id: str, redirect_uri: str, state: str, scopes=()):
        del scopes
        return f"https://github.example/authorize?client_id={client_id}&redirect_uri={redirect_uri}&state={state}"

    def exchange_code(self, *, client_id: str, client_secret: str, code: str, redirect_uri: str):
        self.exchange_calls.append(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            }
        )
        return GitHubOAuthToken(
            access_token="gho_test_token",
            scope="repo read:user user:email",
            token_type="bearer",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=8),
        )

    def fetch_user(self, access_token: str):
        assert access_token == "gho_test_token"
        return GitHubOAuthUser(id=101, login="octocat")


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeGitHubAppSession:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, *, headers: dict[str, str], timeout: int) -> FakeResponse:
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        return FakeResponse(
            201,
            {
                "token": "ghs_test_installation_token",
                "expires_at": "2026-04-12T12:00:00Z",
                "permissions": {"contents": "read"},
            },
        )


def _env(database_url: str) -> dict[str, str]:
    return {
        "DATABASE_URL": database_url,
        "APP_BASE_URL": "https://notebooklens.test",
        "SESSION_SECRET": "test-session-secret",
        "GITHUB_APP_ID": "12345",
        "GITHUB_APP_PRIVATE_KEY": TEST_PRIVATE_KEY.replace("\n", "\\n"),
        "GITHUB_WEBHOOK_SECRET": "webhook-secret",
        "GITHUB_OAUTH_CLIENT_ID": "oauth-client-id",
        "GITHUB_OAUTH_CLIENT_SECRET": "oauth-client-secret",
        "EMAIL_PROVIDER": "resend",
        "EMAIL_API_KEY": "resend-api-key",
        "EMAIL_FROM": "noreply@notebooklens.test",
        "SNAPSHOT_RETENTION_DAYS": "90",
        "MANAGED_REVIEW_BETA_ENABLED": "true",
    }


def _settings(tmp_path: Path) -> ApiSettings:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'managed-api.sqlite3'}"
    reset_settings_cache()
    reset_engine_cache()
    return ApiSettings.from_env(_env(database_url))


def test_api_settings_load_and_normalize_private_key(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert settings.snapshot_retention_days == 90
    assert settings.managed_review_beta_enabled is True
    assert "BEGIN RSA PRIVATE KEY" in settings.github_app_private_key
    assert "\\n" not in settings.github_app_private_key


def test_build_github_app_jwt_and_headers() -> None:
    issued_at = datetime(2026, 4, 12, tzinfo=timezone.utc)
    token = build_github_app_jwt(
        app_id="12345",
        private_key_pem=TEST_PRIVATE_KEY,
        issued_at=issued_at,
    )
    decoded = jwt.decode(
        token,
        options={"verify_signature": False, "verify_exp": False},
        algorithms=["RS256"],
    )
    assert decoded["iss"] == "12345"
    assert decoded["exp"] > decoded["iat"]
    headers = build_github_app_headers(token)
    assert headers["Authorization"] == f"Bearer {token}"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"


def test_github_app_installation_token_client(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    fake_session = FakeGitHubAppSession()
    client = GitHubAppClient(session=fake_session)
    access_token = client.create_installation_access_token(
        settings=settings,
        installation_id=99,
    )
    assert access_token.token == "ghs_test_installation_token"
    assert access_token.permissions == {"contents": "read"}
    assert fake_session.calls[0]["url"].endswith("/app/installations/99/access_tokens")


def test_webhook_signature_helpers() -> None:
    body = b'{"action":"opened"}'
    signature = sign_github_webhook("webhook-secret", body)
    verify_github_webhook_signature("webhook-secret", body, signature)
    try:
        verify_github_webhook_signature("webhook-secret", body, "sha256=bad")
    except GitHubWebhookVerificationError:
        pass
    else:
        raise AssertionError("Expected webhook verification to fail")


def test_oauth_state_and_session_cipher_round_trip() -> None:
    signer = OAuthStateSigner("secret")
    state = signer.issue_state(next_path="/reviews/demo")
    payload = signer.verify_state(state)
    assert payload["next_path"] == "/reviews/demo"

    cipher = SessionTokenCipher("secret")
    encrypted = cipher.encrypt("gho_token")
    assert cipher.decrypt(encrypted) == "gho_token"


def test_snapshot_job_runner_lifecycle(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = get_engine(settings.database_url)
    create_all_tables(engine)

    with session_scope(settings) as db_session:
        installation = GitHubInstallation(
            github_installation_id=11,
            account_login="octo-org",
            account_type=InstallationAccountType.ORGANIZATION,
        )
        db_session.add(installation)
        db_session.flush()
        repository = InstallationRepository(
            installation_id=installation.id,
            owner="octo-org",
            name="notebooklens",
            full_name="octo-org/notebooklens",
            private=True,
            active=True,
        )
        db_session.add(repository)
        db_session.flush()
        review = ManagedReview(
            installation_repository_id=repository.id,
            owner="octo-org",
            repo="notebooklens",
            pull_number=7,
            base_branch="main",
            latest_base_sha="abc123",
            latest_head_sha="def456",
        )
        db_session.add(review)
        db_session.flush()
        job = enqueue_snapshot_build_job(
            db_session,
            managed_review_id=review.id,
            base_sha="abc123",
            head_sha="def456",
        )
        assert job.status == SnapshotBuildJobStatus.QUEUED

    with session_scope(settings) as db_session:
        claimed = claim_next_snapshot_build_job(db_session)
        assert claimed is not None
        assert claimed.status == SnapshotBuildJobStatus.RUNNING
        retryable = mark_snapshot_build_job_retryable_failed(
            db_session,
            claimed,
            error_message="temporary failure",
        )
        assert retryable.status == SnapshotBuildJobStatus.RETRYABLE_FAILED

    with session_scope(settings) as db_session:
        claimed_again = claim_next_snapshot_build_job(db_session)
        assert claimed_again is not None
        assert claimed_again.attempt_count == 2
        succeeded = mark_snapshot_build_job_succeeded(db_session, claimed_again)
        assert succeeded.status == SnapshotBuildJobStatus.SUCCEEDED

    with session_scope(settings) as db_session:
        another = enqueue_snapshot_build_job(
            db_session,
            managed_review_id=review.id,
            base_sha="abc123",
            head_sha="ghi789",
        )
        claimed_third = claim_next_snapshot_build_job(db_session)
        assert claimed_third is not None
        failed = mark_snapshot_build_job_failed(
            db_session,
            claimed_third,
            error_message="permanent failure",
        )
        assert failed.status == SnapshotBuildJobStatus.FAILED
        assert another.id == failed.id
    engine.dispose()


def test_managed_api_metadata_and_migration_scaffold_exist() -> None:
    expected_tables = {
        "github_installations",
        "installation_repositories",
        "managed_reviews",
        "snapshot_build_jobs",
        "review_snapshots",
        "user_sessions",
    }
    assert expected_tables.issubset(set(Base.metadata.tables))
    migration_path = Path(
        "apps/api/alembic/versions/20260412_0001_create_managed_api_core_tables.py"
    )
    assert migration_path.exists()


def test_fastapi_login_callback_logout_flow(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = get_engine(settings.database_url)
    create_all_tables(engine)
    app = create_app()
    fake_oauth = FakeOAuthClient()
    app.dependency_overrides[get_oauth_client] = lambda: fake_oauth
    app.dependency_overrides[get_settings] = lambda: settings

    client = TestClient(app)
    login_response = client.get("/api/auth/github/login", params={"next_path": "/reviews/demo"}, follow_redirects=False)
    assert login_response.status_code == 302
    state_cookie = login_response.cookies.get(STATE_COOKIE_NAME)
    assert state_cookie is not None
    parsed = urlparse(login_response.headers["location"])
    assert parsed.netloc == "github.example"
    state = parse_qs(parsed.query)["state"][0]
    assert state == state_cookie

    client.cookies.set(STATE_COOKIE_NAME, state_cookie)
    callback_response = client.get(
        "/api/auth/github/callback",
        params={"code": "oauth-code", "state": state},
        follow_redirects=False,
    )
    assert callback_response.status_code == 302
    assert callback_response.headers["location"] == "https://notebooklens.test/reviews/demo"
    session_cookie = callback_response.cookies.get(SESSION_COOKIE_NAME)
    assert session_cookie is not None

    with session_scope(settings) as db_session:
        sessions = db_session.scalars(select(UserSession)).all()
        assert len(sessions) == 1
        store = OAuthSessionStore(SessionTokenCipher(settings.session_secret))
        assert store.cipher.decrypt(sessions[0].access_token_encrypted) == "gho_test_token"

    client.cookies.set(SESSION_COOKIE_NAME, session_cookie)
    logout_response = client.post("/api/auth/logout")
    assert logout_response.status_code == 204
    with session_scope(settings) as db_session:
        assert db_session.scalars(select(UserSession)).all() == []
    engine.dispose()


def test_webhook_endpoint_and_review_placeholders(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app)

    body = b'{"action":"opened"}'
    response = client.post(
        "/api/github/webhooks",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-1",
            "X-Hub-Signature-256": sign_github_webhook("webhook-secret", body),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 202
    assert response.json()["event"] == "pull_request"
    assert client.get("/api/reviews/octo/notebooklens/pulls/7").status_code == 501
