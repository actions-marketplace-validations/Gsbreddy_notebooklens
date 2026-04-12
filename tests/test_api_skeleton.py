from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
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
from apps.api.managed_github import ManagedCheckRun
from apps.api.models import (
    Base,
    GitHubInstallation,
    InstallationAccountType,
    InstallationRepository,
    ManagedReview,
    ManagedReviewStatus,
    ReviewSnapshot,
    ReviewSnapshotStatus,
    SnapshotBuildJob,
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
from apps.api.orchestration import run_snapshot_build_worker_once
from apps.api.routes.auth import get_oauth_client
from apps.api.routes.github import get_managed_github_client
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
FIXTURES_DIR = Path(__file__).parent / "fixtures"


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


class FakeManagedGitHubClient:
    def __init__(
        self,
        *,
        files: list[dict[str, Any]] | None = None,
        contents: dict[tuple[str, str], str | None] | None = None,
        failing_content_keys: set[tuple[str, str]] | None = None,
    ) -> None:
        self.files = list(files or [])
        self.contents = dict(contents or {})
        self.failing_content_keys = set(failing_content_keys or set())
        self.check_run_calls: list[dict[str, Any]] = []
        self._next_check_run_id = 9000

    def list_pull_request_files(
        self,
        *,
        settings: ApiSettings,
        installation_id: int,
        repository: str,
        pull_number: int,
    ) -> list[dict[str, Any]]:
        del settings, installation_id, repository, pull_number
        return list(self.files)

    def get_file_content(
        self,
        *,
        settings: ApiSettings,
        installation_id: int,
        repository: str,
        path: str,
        ref: str,
    ) -> str | None:
        del settings, installation_id, repository
        key = (path, ref)
        if key in self.failing_content_keys:
            raise RuntimeError(f"boom while fetching {path}@{ref}")
        return self.contents.get(key)

    def create_or_update_check_run(self, **kwargs: Any) -> ManagedCheckRun:
        self.check_run_calls.append(dict(kwargs))
        check_run_id = kwargs.get("check_run_id")
        if check_run_id is None:
            self._next_check_run_id += 1
            check_run_id = self._next_check_run_id
        return ManagedCheckRun(
            check_run_id=check_run_id,
            html_url=f"https://github.example/check-runs/{check_run_id}",
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


def fixture_text(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def pull_request_payload(
    *,
    action: str = "opened",
    pull_number: int = 7,
    base_sha: str = "base-sha",
    head_sha: str = "head-sha",
) -> dict[str, Any]:
    return {
        "action": action,
        "number": pull_number,
        "installation": {
            "id": 11,
            "account": {
                "login": "octo-org",
                "type": "Organization",
            },
        },
        "repository": {
            "name": "notebooklens",
            "full_name": "octo-org/notebooklens",
            "private": True,
            "owner": {
                "login": "octo-org",
                "type": "Organization",
            },
        },
        "pull_request": {
            "number": pull_number,
            "base": {
                "ref": "main",
                "sha": base_sha,
            },
            "head": {
                "sha": head_sha,
            },
        },
    }


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


def test_webhook_endpoint_queues_managed_snapshot_build(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = get_engine(settings.database_url)
    create_all_tables(engine)
    app = create_app()
    fake_github = FakeManagedGitHubClient()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_managed_github_client] = lambda: fake_github
    client = TestClient(app)

    payload = pull_request_payload()
    body = json.dumps(payload).encode("utf-8")
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
    assert response.json()["status"] == "accepted"
    assert response.json()["job_id"] is not None
    assert response.json()["check_run_id"] == 9001
    assert fake_github.check_run_calls[0]["status"] == "in_progress"
    assert fake_github.check_run_calls[0]["head_sha"] == "head-sha"

    with session_scope(settings) as db_session:
        review = db_session.scalars(select(ManagedReview)).one()
        job = db_session.scalars(select(SnapshotBuildJob)).one()
        assert review.status == ManagedReviewStatus.PENDING
        assert review.latest_check_run_id == 9001
        assert job.status == SnapshotBuildJobStatus.QUEUED

    assert client.get("/api/reviews/octo/notebooklens/pulls/7").status_code == 501
    engine.dispose()


def test_snapshot_worker_builds_ready_snapshot_and_updates_check_run(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = get_engine(settings.database_url)
    create_all_tables(engine)
    app = create_app()
    fake_github = FakeManagedGitHubClient(
        files=[
            {
                "filename": "analysis/notebook.ipynb",
                "status": "modified",
                "size": 2048,
            }
        ],
        contents={
            ("analysis/notebook.ipynb", "base-sha"): fixture_text("simple_base.ipynb"),
            ("analysis/notebook.ipynb", "head-sha"): fixture_text("simple_head.ipynb"),
            (
                ".github/notebooklens.yml",
                "head-sha",
            ): (
                "version: 1\n"
                "reviewer_guidance:\n"
                "  playbooks:\n"
                "    - name: Training notebooks\n"
                "      paths:\n"
                "        - \"analysis/*.ipynb\"\n"
                "      prompts:\n"
                "        - \"Verify the dataset change is intentional.\"\n"
            ),
        },
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_managed_github_client] = lambda: fake_github
    client = TestClient(app)

    payload = pull_request_payload()
    body = json.dumps(payload).encode("utf-8")
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

    with session_scope(settings) as db_session:
        result = run_snapshot_build_worker_once(
            settings=settings,
            db_session=db_session,
            github_client=fake_github,
        )
        assert result.status == "succeeded"

    with session_scope(settings) as db_session:
        review = db_session.scalars(select(ManagedReview)).one()
        snapshot = db_session.scalars(select(ReviewSnapshot)).one()
        assert review.status == ManagedReviewStatus.READY
        assert review.latest_snapshot_id == snapshot.id
        assert snapshot.status == ReviewSnapshotStatus.READY
        assert snapshot.schema_version == 1
        assert snapshot.notebook_count == 1
        assert snapshot.changed_cell_count > 0
        assert snapshot.snapshot_payload_json["review"]["notebooks"][0]["path"] == "analysis/notebook.ipynb"
        assert any(
            item["source"] == "playbook" and item["label"] == "Training notebooks"
            for item in snapshot.reviewer_guidance_json
        )
        assert any(
            item["code"] == "cell_material_metadata_changed"
            for item in snapshot.flagged_findings_json
        )

    assert len(fake_github.check_run_calls) == 2
    ready_call = fake_github.check_run_calls[-1]
    assert ready_call["status"] == "completed"
    assert ready_call["conclusion"] == "neutral"
    assert "/reviews/octo-org/notebooklens/pulls/7/snapshots/1" in ready_call["details_url"]
    assert "Latest snapshot status: `ready`" in ready_call["summary"]
    engine.dispose()


def test_snapshot_worker_marks_failed_and_preserves_last_ready_snapshot(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = get_engine(settings.database_url)
    create_all_tables(engine)
    app = create_app()
    fake_github = FakeManagedGitHubClient(
        files=[
            {
                "filename": "analysis/notebook.ipynb",
                "status": "modified",
                "size": 2048,
            }
        ],
        contents={
            ("analysis/notebook.ipynb", "base-sha"): fixture_text("simple_base.ipynb"),
            ("analysis/notebook.ipynb", "head-sha-1"): fixture_text("simple_head.ipynb"),
        },
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_managed_github_client] = lambda: fake_github
    client = TestClient(app)

    first_payload = pull_request_payload(head_sha="head-sha-1")
    first_body = json.dumps(first_payload).encode("utf-8")
    first_response = client.post(
        "/api/github/webhooks",
        content=first_body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-1",
            "X-Hub-Signature-256": sign_github_webhook("webhook-secret", first_body),
            "Content-Type": "application/json",
        },
    )
    assert first_response.status_code == 202

    with session_scope(settings) as db_session:
        first_result = run_snapshot_build_worker_once(
            settings=settings,
            db_session=db_session,
            github_client=fake_github,
        )
        assert first_result.status == "succeeded"
        first_snapshot_id = first_result.snapshot_id

    fake_github.failing_content_keys.add(("analysis/notebook.ipynb", "head-sha-2"))
    second_payload = pull_request_payload(action="synchronize", head_sha="head-sha-2")
    second_body = json.dumps(second_payload).encode("utf-8")
    second_response = client.post(
        "/api/github/webhooks",
        content=second_body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-2",
            "X-Hub-Signature-256": sign_github_webhook("webhook-secret", second_body),
            "Content-Type": "application/json",
        },
    )
    assert second_response.status_code == 202

    with session_scope(settings) as db_session:
        failed_result = run_snapshot_build_worker_once(
            settings=settings,
            db_session=db_session,
            github_client=fake_github,
        )
        assert failed_result.status == "failed"

    with session_scope(settings) as db_session:
        review = db_session.scalars(select(ManagedReview)).one()
        snapshots = db_session.scalars(
            select(ReviewSnapshot).order_by(ReviewSnapshot.snapshot_index.asc())
        ).all()
        assert review.status == ManagedReviewStatus.FAILED
        assert review.latest_snapshot_id == first_snapshot_id
        assert len(snapshots) == 2
        assert snapshots[0].status == ReviewSnapshotStatus.READY
        assert snapshots[1].status == ReviewSnapshotStatus.FAILED
        assert snapshots[1].failure_reason is not None
        assert "boom while fetching analysis/notebook.ipynb@head-sha-2" in snapshots[1].failure_reason

    failed_call = fake_github.check_run_calls[-1]
    assert failed_call["status"] == "completed"
    assert failed_call["conclusion"] == "action_required"
    assert "Latest snapshot status: `failed`" in failed_call["summary"]
    assert "Failure: boom while fetching analysis/notebook.ipynb@head-sha-2" in failed_call["summary"]
    engine.dispose()
