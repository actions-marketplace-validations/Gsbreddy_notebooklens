from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import jwt
from sqlalchemy import select
import yaml

from apps.api.config import ApiConfigurationError, ApiSettings, get_settings, reset_settings_cache
from apps.api.check_runs import sync_review_workspace_check_run
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
from apps.api.managed_github import ManagedCheckRun, ManagedComment, ManagedGitHubClientError
from apps.api.models import (
    Base,
    GitHubInstallation,
    GitHubMirrorAction,
    GitHubMirrorJob,
    GitHubMirrorState,
    GitHubHostKind,
    InstallationAccountType,
    InstallationRepository,
    ManagedAiGatewayConfig,
    ManagedAiGatewayProviderKind,
    ManagedReview,
    ManagedReviewStatus,
    NotificationDeliveryState,
    NotificationEventType,
    NotificationOutbox,
    ReviewAsset,
    ReviewThread,
    ReviewThreadStatus,
    ReviewSnapshot,
    ReviewSnapshotStatus,
    SnapshotBuildJob,
    SnapshotBuildJobStatus,
    ThreadMessage,
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
from apps.api.orchestration import LiteLLMGatewayResponse, run_snapshot_build_worker_once
from apps.api.review_workspace import (
    enqueue_github_mirror_job,
    get_workspace_payload,
    resolve_mirror_auth_context,
)
from apps.api.routes.auth import get_oauth_client
from apps.api.routes.github import get_managed_github_client
from apps.api.routes.repo_access import reset_repo_access_cache
from apps.api.routes.settings import get_litellm_connection_tester
from apps.api.worker import (
    process_github_mirror_job_once,
    process_notification_delivery_once,
    process_retention_cleanup_once,
)
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
REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = Path(__file__).parent / "fixtures"
REVIEW_WORKSPACE_THREAD_PATH = "notebooks/training/churn_model.ipynb"
SALES_FORECAST_NOTEBOOK_PATH = "notebooks/forecast/sales_forecast.ipynb"
SALES_FORECAST_BASE_FIXTURE = "sales_forecast_plot_base.ipynb"
SALES_FORECAST_HEAD_FIXTURE = "sales_forecast_plot_head.ipynb"
SMALL_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO2N1foAAAAASUVORK5CYII="
)


class FakeOAuthClient:
    def __init__(
        self,
        *,
        users_by_token: dict[str, GitHubOAuthUser] | None = None,
        repo_access: dict[tuple[str, str, str], bool] | None = None,
        org_owner_access: dict[tuple[str, str], bool] | None = None,
    ) -> None:
        self.exchange_calls: list[dict[str, Any]] = []
        self.users_by_token = dict(
            users_by_token
            or {
                "gho_test_token": GitHubOAuthUser(
                    id=101,
                    login="octocat",
                    email="octocat@example.test",
                )
            }
        )
        self.repo_access = dict(repo_access or {})
        self.org_owner_access = dict(org_owner_access or {})
        self.repo_access_checks: list[tuple[str, str, str]] = []
        self.org_owner_checks: list[tuple[str, str]] = []

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
        return self.users_by_token[access_token]

    def can_access_repository(self, access_token: str, *, owner: str, repo: str) -> bool:
        self.repo_access_checks.append((access_token, owner, repo))
        return self.repo_access.get((access_token, owner, repo), True)

    def is_org_owner(self, access_token: str, *, org: str) -> bool:
        self.org_owner_checks.append((access_token, org))
        return self.org_owner_access.get((access_token, org), False)


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
        api_base_url: str = "https://api.github.com",
    ) -> None:
        self.api_base_url = api_base_url
        self.files = list(files or [])
        self.contents = dict(contents or {})
        self.failing_content_keys = set(failing_content_keys or set())
        self.check_run_calls: list[dict[str, Any]] = []
        self.issue_comment_calls: list[dict[str, Any]] = []
        self.review_comment_calls: list[dict[str, Any]] = []
        self.issue_comments: dict[int, dict[str, Any]] = {}
        self.review_comments: dict[int, dict[str, Any]] = {}
        self._next_check_run_id = 9000
        self._next_issue_comment_id = 6000
        self._next_review_comment_id = 7000

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

    def upsert_issue_comment(self, **kwargs: Any) -> ManagedComment:
        comment_id = kwargs.get("comment_id")
        if isinstance(comment_id, int) and comment_id in self.issue_comments:
            return self.update_issue_comment(**kwargs)
        return self.create_issue_comment(**kwargs)

    def create_issue_comment(self, **kwargs: Any) -> ManagedComment:
        self._next_issue_comment_id += 1
        comment_id = self._next_issue_comment_id
        body = str(kwargs["body"])
        pull_number = int(kwargs["pull_number"])
        repository = str(kwargs["repository"])
        self.issue_comment_calls.append(
            {
                "op": "create",
                "repository": repository,
                "pull_number": pull_number,
                "body": body,
                "access_token": kwargs.get("access_token"),
            }
        )
        self.issue_comments[comment_id] = {
            "body": body,
            "repository": repository,
            "pull_number": pull_number,
        }
        return ManagedComment(
            comment_id=comment_id,
            html_url=f"https://github.example/{repository}/pull/{pull_number}#issuecomment-{comment_id}",
        )

    def update_issue_comment(self, **kwargs: Any) -> ManagedComment:
        comment_id = int(kwargs["comment_id"])
        record = self.issue_comments.get(comment_id)
        if record is None:
            raise ManagedGitHubClientError("issue comment not found", status_code=404)
        body = str(kwargs["body"])
        self.issue_comment_calls.append(
            {
                "op": "update",
                "repository": kwargs["repository"],
                "pull_number": kwargs.get("pull_number"),
                "comment_id": comment_id,
                "body": body,
                "access_token": kwargs.get("access_token"),
            }
        )
        record["body"] = body
        return ManagedComment(
            comment_id=comment_id,
            html_url=f"https://github.example/{record['repository']}/pull/{record['pull_number']}#issuecomment-{comment_id}",
        )

    def upsert_review_comment(self, **kwargs: Any) -> ManagedComment:
        comment_id = kwargs.get("comment_id")
        if isinstance(comment_id, int) and comment_id in self.review_comments:
            return self.update_review_comment(**kwargs)
        return self._create_review_comment(**kwargs)

    def _create_review_comment(self, **kwargs: Any) -> ManagedComment:
        self._next_review_comment_id += 1
        comment_id = self._next_review_comment_id
        body = str(kwargs["body"])
        repository = str(kwargs["repository"])
        pull_number = int(kwargs["pull_number"])
        self.review_comment_calls.append(
            {
                "op": "create_root",
                "repository": repository,
                "pull_number": pull_number,
                "comment_id": comment_id,
                "body": body,
                "access_token": kwargs.get("access_token"),
                "path": kwargs.get("path"),
                "line": kwargs.get("line"),
                "commit_id": kwargs.get("commit_id"),
            }
        )
        self.review_comments[comment_id] = {
            "body": body,
            "repository": repository,
            "pull_number": pull_number,
            "path": kwargs.get("path"),
            "line": kwargs.get("line"),
            "parent_comment_id": None,
        }
        return ManagedComment(
            comment_id=comment_id,
            html_url=f"https://github.example/{repository}/pull/{pull_number}#discussion_r{comment_id}",
        )

    def create_review_comment_reply(self, **kwargs: Any) -> ManagedComment:
        parent_comment_id = int(kwargs["comment_id"])
        if parent_comment_id not in self.review_comments:
            raise ManagedGitHubClientError("review comment not found", status_code=404)
        self._next_review_comment_id += 1
        comment_id = self._next_review_comment_id
        body = str(kwargs["body"])
        repository = str(kwargs["repository"])
        pull_number = int(kwargs["pull_number"])
        self.review_comment_calls.append(
            {
                "op": "create_reply",
                "repository": repository,
                "pull_number": pull_number,
                "comment_id": comment_id,
                "parent_comment_id": parent_comment_id,
                "body": body,
                "access_token": kwargs.get("access_token"),
            }
        )
        self.review_comments[comment_id] = {
            "body": body,
            "repository": repository,
            "pull_number": pull_number,
            "parent_comment_id": parent_comment_id,
        }
        return ManagedComment(
            comment_id=comment_id,
            html_url=f"https://github.example/{repository}/pull/{pull_number}#discussion_r{comment_id}",
        )

    def update_review_comment(self, **kwargs: Any) -> ManagedComment:
        comment_id = int(kwargs["comment_id"])
        record = self.review_comments.get(comment_id)
        if record is None:
            raise ManagedGitHubClientError("review comment not found", status_code=404)
        body = str(kwargs["body"])
        self.review_comment_calls.append(
            {
                "op": "update",
                "repository": kwargs["repository"],
                "comment_id": comment_id,
                "body": body,
                "access_token": kwargs.get("access_token"),
            }
        )
        record["body"] = body
        return ManagedComment(
            comment_id=comment_id,
            html_url=f"https://github.example/{record['repository']}/pull/{record['pull_number']}#discussion_r{comment_id}",
        )


class FakeEmailClient:
    def __init__(self, *, failing_recipients: set[str] | None = None) -> None:
        self.failing_recipients = set(failing_recipients or set())
        self.sent_messages: list[Any] = []

    def send_transactional_email(self, message: Any) -> None:
        if message.to_email in self.failing_recipients:
            raise RuntimeError(f"boom while emailing {message.to_email}")
        self.sent_messages.append(message)


class FakeLiteLLMConnectionTester:
    def __init__(self, *, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.calls: list[dict[str, Any]] = []

    def test_connection(self, **kwargs: Any) -> str:
        self.calls.append(dict(kwargs))
        if self.should_fail:
            raise RuntimeError("boom")
        return "/chat/completions" if not kwargs["use_responses_api"] else "/responses"


class FakeLiteLLMGatewayClient:
    def __init__(
        self,
        *,
        responses: list[str] | None = None,
        error: str | None = None,
    ) -> None:
        self.responses = list(responses or [])
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def complete(self, **kwargs: Any) -> LiteLLMGatewayResponse:
        config = kwargs["config"]
        self.calls.append(
            {
                "base_url": config.base_url,
                "model_name": config.model_name,
                "use_responses_api": config.use_responses_api,
                "api_key": kwargs["api_key"],
                "static_headers": dict(kwargs["static_headers"]),
                "prompt": kwargs["prompt"],
            }
        )
        if self.error is not None:
            raise RuntimeError(self.error)
        if not self.responses:
            raise RuntimeError("No fake LiteLLM response configured")
        return LiteLLMGatewayResponse(
            text=self.responses.pop(0),
            input_tokens=123,
            output_tokens=45,
        )


def _env(database_url: str) -> dict[str, str]:
    return {
        "DATABASE_URL": database_url,
        "APP_BASE_URL": "https://notebooklens.test",
        "SESSION_SECRET": "test-session-secret",
        "ENCRYPTION_KEY": "test-encryption-secret",
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
    reset_repo_access_cache()
    return ApiSettings.from_env(_env(database_url))


def fixture_text(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def sales_forecast_notebooks() -> tuple[str, str]:
    return (
        fixture_text(SALES_FORECAST_BASE_FIXTURE),
        fixture_text(SALES_FORECAST_HEAD_FIXTURE),
    )


def assert_compose_smoke_stack_supports_managed_review_flow() -> None:
    compose_path = REPO_ROOT / "deploy" / "docker-compose.yml"
    caddyfile_path = REPO_ROOT / "deploy" / "Caddyfile"

    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    services = compose["services"]

    assert set(services) >= {"gateway", "web", "api", "worker", "postgres"}
    assert services["gateway"]["depends_on"]["api"]["condition"] == "service_healthy"
    assert services["gateway"]["depends_on"]["web"]["condition"] == "service_healthy"
    assert services["api"]["command"] == ["run-managed-service", "api"]
    assert services["worker"]["command"] == ["run-managed-service", "worker"]
    assert services["api"]["environment"]["APP_BASE_URL"] == "${APP_BASE_URL:?APP_BASE_URL is required}"
    assert services["worker"]["environment"]["GITHUB_PR_SYNC_ENABLED"] == (
        "${GITHUB_PR_SYNC_ENABLED:?GITHUB_PR_SYNC_ENABLED is required}"
    )

    caddyfile = caddyfile_path.read_text(encoding="utf-8")
    assert "@api path /api /api/*" in caddyfile
    assert "reverse_proxy @api api:8000" in caddyfile
    assert "reverse_proxy web:3000" in caddyfile


def pull_request_payload(
    *,
    action: str = "opened",
    pull_number: int = 7,
    base_sha: str = "base-sha",
    head_sha: str = "head-sha",
    author_id: int = 202,
    author_login: str = "pr-author",
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
            "user": {
                "id": author_id,
                "login": author_login,
            },
            "base": {
                "ref": "main",
                "sha": base_sha,
            },
            "head": {
                "sha": head_sha,
            },
        },
    }


def post_pull_request_webhook(
    client: TestClient,
    *,
    payload: dict[str, Any],
    delivery_id: str,
) -> Any:
    body = json.dumps(payload).encode("utf-8")
    response = client.post(
        "/api/github/webhooks",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": delivery_id,
            "X-Hub-Signature-256": sign_github_webhook("webhook-secret", body),
            "Content-Type": "application/json",
        },
    )
    return response


def run_managed_snapshot_worker(
    *,
    settings: ApiSettings,
    github_client: FakeManagedGitHubClient,
) -> Any:
    with session_scope(settings) as db_session:
        return run_snapshot_build_worker_once(
            settings=settings,
            db_session=db_session,
            github_client=github_client,
        )


def run_github_mirror_worker_until_idle(
    *,
    settings: ApiSettings,
    github_client: FakeManagedGitHubClient,
) -> list[Any]:
    results: list[Any] = []
    while True:
        result = process_github_mirror_job_once(
            settings=settings,
            github_client=github_client,
        )
        results.append(result)
        if result.status == "idle":
            break
    return results


def review_row_for_cell(workspace: dict[str, Any], *, cell_id: str) -> dict[str, Any]:
    notebook = workspace["snapshot"]["payload"]["review"]["notebooks"][0]
    return next(item for item in notebook["render_rows"] if item["locator"]["cell_id"] == cell_id)


def create_user_session(
    settings: ApiSettings,
    *,
    github_user_id: int,
    github_login: str,
    access_token: str,
    expires_at: datetime | None = None,
) -> str:
    with session_scope(settings) as db_session:
        record = OAuthSessionStore(SessionTokenCipher(settings.session_secret)).create_session(
            db_session,
            github_user=GitHubOAuthUser(
                id=github_user_id,
                login=github_login,
                email=None,
            ),
            access_token=access_token,
            expires_at=expires_at or (datetime.now(timezone.utc) + timedelta(hours=8)),
        )
        return str(record.id)


def review_thread_notebook(metric: str, *, explanation: str = "Baseline churn training.") -> str:
    return json.dumps(
        {
            "cells": [
                {
                    "cell_type": "markdown",
                    "id": "intro-cell",
                    "source": explanation,
                    "metadata": {},
                },
                {
                    "cell_type": "code",
                    "id": "metric-cell",
                    "source": "print('accuracy')",
                    "metadata": {},
                    "outputs": [
                        {
                            "output_type": "stream",
                            "name": "stdout",
                            "text": [f"accuracy = {metric}\n"],
                        }
                    ],
                },
            ],
            "metadata": {},
            "nbformat": 4,
            "nbformat_minor": 5,
        }
    )


def review_metadata_thread_notebooks() -> tuple[str, str]:
    return (
        json.dumps(
            {
                "cells": [
                    {
                        "cell_type": "code",
                        "id": "metric-cell",
                        "source": "print('accuracy')",
                        "metadata": {"tags": ["baseline"]},
                        "outputs": [],
                    }
                ],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        ),
        json.dumps(
            {
                "cells": [
                    {
                        "cell_type": "code",
                        "id": "metric-cell",
                        "source": "print('accuracy')",
                        "metadata": {"tags": ["baseline", "review-me"]},
                        "outputs": [],
                    }
                ],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        ),
    )


def review_asset_notebook(
    *,
    png_payload: str = SMALL_PNG_BASE64,
    include_placeholders: bool = True,
) -> tuple[str, str]:
    base_cells = [
        {
            "cell_type": "code",
            "id": "plot-one",
            "source": "plot_one()",
            "metadata": {},
            "outputs": [],
        },
        {
            "cell_type": "code",
            "id": "plot-two",
            "source": "plot_two()",
            "metadata": {},
            "outputs": [],
        },
    ]
    head_cells = [
        {
            "cell_type": "code",
            "id": "plot-one",
            "source": "plot_one()",
            "metadata": {},
            "outputs": [
                {
                    "output_type": "display_data",
                    "data": {"image/png": png_payload},
                }
            ],
        },
        {
            "cell_type": "code",
            "id": "plot-two",
            "source": "plot_two()",
            "metadata": {},
            "outputs": [
                {
                    "output_type": "display_data",
                    "data": {"image/png": png_payload},
                }
            ],
        },
    ]
    if include_placeholders:
        oversized_gif_base64 = base64.b64encode(
            b"GIF89a\x01\x00\x01\x00" + (b"\x00" * 2_097_200)
        ).decode("ascii")
        base_cells.extend(
            [
                {
                    "cell_type": "code",
                    "id": "svg-plot",
                    "source": "plot_svg()",
                    "metadata": {},
                    "outputs": [],
                },
                {
                    "cell_type": "code",
                    "id": "too-large-plot",
                    "source": "plot_large()",
                    "metadata": {},
                    "outputs": [],
                },
            ]
        )
        head_cells.extend(
            [
                {
                    "cell_type": "code",
                    "id": "svg-plot",
                    "source": "plot_svg()",
                    "metadata": {},
                    "outputs": [
                        {
                            "output_type": "display_data",
                            "data": {
                                "image/svg+xml": (
                                    "<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'></svg>"
                                )
                            },
                        }
                    ],
                },
                {
                    "cell_type": "code",
                    "id": "too-large-plot",
                    "source": "plot_large()",
                    "metadata": {},
                    "outputs": [
                        {
                            "output_type": "display_data",
                            "data": {"image/gif": oversized_gif_base64},
                        }
                    ],
                },
            ]
        )
    return (
        json.dumps(
            {
                "cells": base_cells,
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        ),
        json.dumps(
            {
                "cells": head_cells,
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        ),
    )


def create_review_asset_fixture(settings: ApiSettings) -> str:
    asset_bytes = base64.b64decode(SMALL_PNG_BASE64)
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
            latest_base_sha="base-sha",
            latest_head_sha="head-sha",
            status=ManagedReviewStatus.READY,
        )
        db_session.add(review)
        db_session.flush()

        snapshot = ReviewSnapshot(
            managed_review_id=review.id,
            base_sha="base-sha",
            head_sha="head-sha",
            snapshot_index=1,
            status=ReviewSnapshotStatus.READY,
            schema_version=1,
            summary_text=None,
            flagged_findings_json=[],
            reviewer_guidance_json=[],
            snapshot_payload_json={"schema_version": 1, "review": {"notebooks": []}},
            notebook_count=1,
            changed_cell_count=1,
            failure_reason=None,
        )
        db_session.add(snapshot)
        db_session.flush()

        review.latest_snapshot_id = snapshot.id
        asset = ReviewAsset(
            snapshot_id=snapshot.id,
            sha256="6036f1a33af7cb1f50fb76fa2e7be2d0dce995c8484af18f4f5bc085ac6f6f6a",
            mime_type="image/png",
            byte_size=len(asset_bytes),
            width=1,
            height=1,
            storage_key=f"reviews/{snapshot.id}/6036f1a33af7cb1f50fb76fa2e7be2d0dce995c8484af18f4f5bc085ac6f6f6a.png",
            content_bytes=asset_bytes,
        )
        db_session.add(asset)
        db_session.flush()
        return str(asset.id)


def create_github_installation_fixture(
    settings: ApiSettings,
    *,
    github_installation_id: int = 11,
    account_login: str = "octo-org",
    account_type: InstallationAccountType = InstallationAccountType.ORGANIZATION,
) -> str:
    with session_scope(settings) as db_session:
        installation = GitHubInstallation(
            github_installation_id=github_installation_id,
            account_login=account_login,
            account_type=account_type,
        )
        db_session.add(installation)
        db_session.flush()
        return str(installation.id)


def create_managed_ai_gateway_fixture(
    settings: ApiSettings,
    *,
    installation_id: str,
    active: bool = True,
    use_responses_api: bool = False,
) -> str:
    cipher = SessionTokenCipher(settings.encryption_key)
    with session_scope(settings) as db_session:
        config = ManagedAiGatewayConfig(
            installation_id=uuid.UUID(installation_id),
            provider_kind=ManagedAiGatewayProviderKind.LITELLM,
            display_name="Internal LiteLLM",
            github_host_kind=GitHubHostKind.GITHUB_COM,
            github_api_base_url="https://api.github.com",
            github_web_base_url="https://github.com",
            base_url="https://litellm.internal.example/v1",
            model_name="gpt-4.1",
            api_key_encrypted=cipher.encrypt("Bearer managed-secret"),
            api_key_header_name="Authorization",
            static_headers_encrypted_json=cipher.encrypt(
                json.dumps({"x-tenant-token": "tenant-secret"})
            ),
            use_responses_api=use_responses_api,
            litellm_virtual_key_id="vk-123",
            active=active,
            updated_by_github_user_id=101,
        )
        db_session.add(config)
        db_session.flush()
        return str(config.id)


def create_ready_review_fixture(
    settings: ApiSettings,
    *,
    account_login: str = "octo-org",
    account_type: InstallationAccountType = InstallationAccountType.ORGANIZATION,
    owner: str = "octo-org",
    repo: str = "notebooklens",
    pull_number: int = 7,
    base_sha: str = "base-sha",
    head_sha: str = "head-sha",
) -> tuple[str, str]:
    with session_scope(settings) as db_session:
        installation = GitHubInstallation(
            github_installation_id=11,
            account_login=account_login,
            account_type=account_type,
        )
        db_session.add(installation)
        db_session.flush()

        repository = InstallationRepository(
            installation_id=installation.id,
            owner=owner,
            name=repo,
            full_name=f"{owner}/{repo}",
            private=True,
            active=True,
        )
        db_session.add(repository)
        db_session.flush()

        review = ManagedReview(
            installation_repository_id=repository.id,
            owner=owner,
            repo=repo,
            pull_number=pull_number,
            base_branch="main",
            latest_base_sha=base_sha,
            latest_head_sha=head_sha,
            status=ManagedReviewStatus.READY,
        )
        db_session.add(review)
        db_session.flush()

        snapshot = ReviewSnapshot(
            managed_review_id=review.id,
            base_sha=base_sha,
            head_sha=head_sha,
            snapshot_index=1,
            status=ReviewSnapshotStatus.READY,
            schema_version=1,
            summary_text="Existing snapshot",
            flagged_findings_json=[],
            reviewer_guidance_json=[],
            snapshot_payload_json={"schema_version": 1, "review": {"notices": [], "notebooks": []}},
            notebook_count=1,
            changed_cell_count=1,
            failure_reason=None,
        )
        db_session.add(snapshot)
        db_session.flush()

        review.latest_snapshot_id = snapshot.id
        db_session.flush()
        return str(review.id), str(installation.id)


def test_api_settings_load_and_normalize_private_key(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert settings.snapshot_retention_days == 90
    assert settings.managed_review_beta_enabled is True
    assert settings.app_base_url == "https://notebooklens.test"
    assert settings.encryption_key == "test-encryption-secret"
    assert "BEGIN RSA PRIVATE KEY" in settings.github_app_private_key
    assert "\\n" not in settings.github_app_private_key


def test_api_settings_reject_non_origin_app_base_url(tmp_path: Path) -> None:
    invalid_env = _env(f"sqlite+pysqlite:///{tmp_path / 'managed-api.sqlite3'}")
    invalid_env["APP_BASE_URL"] = "https://notebooklens.test/reviews"
    try:
        ApiSettings.from_env(invalid_env)
    except ApiConfigurationError as exc:
        assert str(exc) == "APP_BASE_URL must be an origin without a path, query, or fragment"
    else:
        raise AssertionError("Expected APP_BASE_URL validation to fail")


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


def test_healthz_reports_configuration_errors(tmp_path: Path, monkeypatch: Any) -> None:
    env = _env(f"sqlite+pysqlite:///{tmp_path / 'managed-api.sqlite3'}")
    env["APP_BASE_URL"] = "https://notebooklens.test/reviews"
    reset_settings_cache()
    reset_engine_cache()
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    client = TestClient(create_app())
    response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "error",
        "checks": {
            "config": {
                "status": "error",
                "detail": "APP_BASE_URL must be an origin without a path, query, or fragment",
            },
            "database": {"status": "unknown"},
        },
    }


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
        "managed_ai_gateway_configs",
        "managed_reviews",
        "notification_outbox",
        "review_assets",
        "snapshot_build_jobs",
        "review_threads",
        "review_snapshots",
        "thread_messages",
        "user_sessions",
    }
    assert expected_tables.issubset(set(Base.metadata.tables))
    migration_path = Path(
        "apps/api/alembic/versions/20260412_0001_create_managed_api_core_tables.py"
    )
    assert migration_path.exists()
    assert Path(
        "apps/api/alembic/versions/20260412_0002_add_review_threads_and_notifications.py"
    ).exists()
    assert Path(
        "apps/api/alembic/versions/20260412_0003_add_review_assets.py"
    ).exists()
    assert Path(
        "apps/api/alembic/versions/20260412_0004_add_managed_ai_gateway_configs.py"
    ).exists()
    assert Path(
        "apps/api/alembic/versions/20260412_0005_add_force_rebuild_to_snapshot_jobs.py"
    ).exists()


def test_workspace_payload_keeps_origin_anchor_visible_on_origin_snapshot(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = get_engine(settings.database_url)
    create_all_tables(engine)

    origin_anchor = {
        "notebook_path": REVIEW_WORKSPACE_THREAD_PATH,
        "block_kind": "outputs",
        "source_fingerprint": "metric-output-v1",
        "cell_type": "code",
        "cell_locator": {
            "cell_id": "metric-cell",
            "base_index": 3,
            "head_index": 3,
            "display_index": 3,
        },
    }
    current_anchor = {
        "notebook_path": REVIEW_WORKSPACE_THREAD_PATH,
        "block_kind": "outputs",
        "source_fingerprint": "metric-output-v2",
        "cell_type": "code",
        "cell_locator": {
            "cell_id": "metric-cell",
            "base_index": 4,
            "head_index": 4,
            "display_index": 4,
        },
    }

    with session_scope(settings) as db_session:
        installation = GitHubInstallation(
            github_installation_id=1,
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
            latest_base_sha="base-sha-2",
            latest_head_sha="head-sha-2",
            status=ManagedReviewStatus.READY,
        )
        db_session.add(review)
        db_session.flush()

        snapshot_one = ReviewSnapshot(
            managed_review_id=review.id,
            base_sha="base-sha-1",
            head_sha="head-sha-1",
            snapshot_index=1,
            status=ReviewSnapshotStatus.READY,
            schema_version=1,
            summary_text=None,
            flagged_findings_json=[],
            reviewer_guidance_json=[],
            snapshot_payload_json={"schema_version": 1, "review": {"notices": [], "notebooks": []}},
            notebook_count=1,
            changed_cell_count=1,
            failure_reason=None,
        )
        snapshot_two = ReviewSnapshot(
            managed_review_id=review.id,
            base_sha="base-sha-2",
            head_sha="head-sha-2",
            snapshot_index=2,
            status=ReviewSnapshotStatus.READY,
            schema_version=1,
            summary_text=None,
            flagged_findings_json=[],
            reviewer_guidance_json=[],
            snapshot_payload_json={"schema_version": 1, "review": {"notices": [], "notebooks": []}},
            notebook_count=1,
            changed_cell_count=1,
            failure_reason=None,
        )
        db_session.add_all([snapshot_one, snapshot_two])
        db_session.flush()

        review.latest_snapshot_id = snapshot_two.id

        thread = ReviewThread(
            managed_review_id=review.id,
            origin_snapshot_id=snapshot_one.id,
            current_snapshot_id=snapshot_two.id,
            origin_anchor_json=origin_anchor,
            anchor_json=current_anchor,
            status=ReviewThreadStatus.OPEN,
            carried_forward=True,
            created_by_github_user_id=101,
        )
        db_session.add(thread)
        db_session.flush()
        db_session.add(
            ThreadMessage(
                thread_id=thread.id,
                author_github_user_id=101,
                author_login="reviewer",
                body_markdown="Explain the regression.",
            )
        )
        db_session.flush()

        origin_payload = get_workspace_payload(
            db_session=db_session,
            review=review,
            snapshot_index=1,
        )
        latest_payload = get_workspace_payload(
            db_session=db_session,
            review=review,
            snapshot_index=2,
        )

    assert origin_payload["threads"][0]["anchor"] == origin_anchor
    assert latest_payload["threads"][0]["anchor"] == current_anchor

    engine.dispose()


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
    assert fake_github.check_run_calls[0]["status"] == "queued"
    assert fake_github.check_run_calls[0]["head_sha"] == "head-sha"
    assert "Latest snapshot status: `pending`" in fake_github.check_run_calls[0]["summary"]

    with session_scope(settings) as db_session:
        review = db_session.scalars(select(ManagedReview)).one()
        job = db_session.scalars(select(SnapshotBuildJob)).one()
        assert review.status == ManagedReviewStatus.PENDING
        assert review.latest_check_run_id == 9001
        assert job.status == SnapshotBuildJobStatus.QUEUED

    assert client.get("/api/reviews/octo/notebooklens/pulls/7").status_code == 401
    engine.dispose()


def test_retention_cleanup_worker_purges_expired_managed_review_data(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = get_engine(settings.database_url)
    create_all_tables(engine)
    now = datetime.now(timezone.utc)
    cutoff_age = timedelta(days=settings.snapshot_retention_days + 1)

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

        stale_review = ManagedReview(
            installation_repository_id=repository.id,
            owner="octo-org",
            repo="notebooklens",
            pull_number=7,
            base_branch="main",
            latest_base_sha="base-old",
            latest_head_sha="head-old",
            status=ManagedReviewStatus.READY,
            created_at=now - cutoff_age,
            updated_at=now - cutoff_age,
        )
        fresh_review = ManagedReview(
            installation_repository_id=repository.id,
            owner="octo-org",
            repo="notebooklens",
            pull_number=8,
            base_branch="main",
            latest_base_sha="base-new",
            latest_head_sha="head-new",
            status=ManagedReviewStatus.READY,
            created_at=now,
            updated_at=now,
        )
        db_session.add_all([stale_review, fresh_review])
        db_session.flush()

        stale_snapshot = ReviewSnapshot(
            managed_review_id=stale_review.id,
            base_sha="base-old",
            head_sha="head-old",
            snapshot_index=1,
            status=ReviewSnapshotStatus.READY,
            schema_version=1,
            summary_text=None,
            flagged_findings_json=[],
            reviewer_guidance_json=[],
            snapshot_payload_json={"schema_version": 1, "review": {"notices": [], "notebooks": []}},
            notebook_count=1,
            changed_cell_count=1,
            failure_reason=None,
            created_at=now - cutoff_age,
        )
        fresh_snapshot = ReviewSnapshot(
            managed_review_id=fresh_review.id,
            base_sha="base-new",
            head_sha="head-new",
            snapshot_index=1,
            status=ReviewSnapshotStatus.READY,
            schema_version=1,
            summary_text=None,
            flagged_findings_json=[],
            reviewer_guidance_json=[],
            snapshot_payload_json={"schema_version": 1, "review": {"notices": [], "notebooks": []}},
            notebook_count=1,
            changed_cell_count=1,
            failure_reason=None,
            created_at=now,
        )
        db_session.add_all([stale_snapshot, fresh_snapshot])
        db_session.flush()

        stale_review.latest_snapshot_id = stale_snapshot.id
        fresh_review.latest_snapshot_id = fresh_snapshot.id

        stale_thread = ReviewThread(
            managed_review_id=stale_review.id,
            origin_snapshot_id=stale_snapshot.id,
            current_snapshot_id=stale_snapshot.id,
            origin_anchor_json={"notebook_path": "stale.ipynb"},
            anchor_json={"notebook_path": "stale.ipynb"},
            status=ReviewThreadStatus.OPEN,
            carried_forward=False,
            created_by_github_user_id=101,
            created_at=now - cutoff_age,
            updated_at=now - cutoff_age,
        )
        db_session.add(stale_thread)
        db_session.flush()
        db_session.add(
            ThreadMessage(
                thread_id=stale_thread.id,
                author_github_user_id=101,
                author_login="reviewer",
                body_markdown="Old thread message",
                created_at=now - cutoff_age,
            )
        )
        db_session.add(
            NotificationOutbox(
                thread_id=stale_thread.id,
                event_type=NotificationEventType.THREAD_CREATED,
                recipient_github_user_id=202,
                recipient_email="author@example.test",
                payload_json={},
                delivery_state=NotificationDeliveryState.PENDING,
                attempt_count=0,
                last_error=None,
                created_at=now - cutoff_age,
            )
        )
        db_session.add(
            SnapshotBuildJob(
                managed_review_id=stale_review.id,
                base_sha="base-old",
                head_sha="head-old",
                status=SnapshotBuildJobStatus.SUCCEEDED,
                attempt_count=1,
                last_error=None,
                scheduled_at=now - cutoff_age,
                started_at=now - cutoff_age,
                finished_at=now - cutoff_age,
            )
        )
        stale_review.updated_at = now - cutoff_age
        fresh_review.updated_at = now

    cleanup_result = process_retention_cleanup_once(settings=settings, now=now)
    assert cleanup_result.purged_reviews == 1

    with session_scope(settings) as db_session:
        reviews = db_session.scalars(
            select(ManagedReview).order_by(ManagedReview.pull_number.asc())
        ).all()
        snapshots = db_session.scalars(
            select(ReviewSnapshot).order_by(ReviewSnapshot.snapshot_index.asc())
        ).all()
        assert [review.pull_number for review in reviews] == [8]
        assert [snapshot.head_sha for snapshot in snapshots] == ["head-new"]
        assert db_session.scalars(select(SnapshotBuildJob)).all() == []
        assert db_session.scalars(select(ReviewThread)).all() == []
        assert db_session.scalars(select(ThreadMessage)).all() == []
        assert db_session.scalars(select(NotificationOutbox)).all() == []

    engine.dispose()


def test_check_run_stays_queued_when_latest_push_job_has_not_been_claimed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = get_engine(settings.database_url)
    create_all_tables(engine)
    fake_github = FakeManagedGitHubClient()
    now = datetime.now(timezone.utc)

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
            latest_base_sha="base-sha-new",
            latest_head_sha="head-sha-new",
            status=ManagedReviewStatus.PENDING,
        )
        db_session.add(review)
        db_session.flush()

        db_session.add_all(
            [
                SnapshotBuildJob(
                    managed_review_id=review.id,
                    base_sha="base-sha-old",
                    head_sha="head-sha-old",
                    status=SnapshotBuildJobStatus.RUNNING,
                    attempt_count=1,
                    last_error=None,
                    scheduled_at=now - timedelta(minutes=2),
                    started_at=now - timedelta(minutes=1),
                    finished_at=None,
                ),
                SnapshotBuildJob(
                    managed_review_id=review.id,
                    base_sha="base-sha-new",
                    head_sha="head-sha-new",
                    status=SnapshotBuildJobStatus.QUEUED,
                    attempt_count=0,
                    last_error=None,
                    scheduled_at=now,
                    started_at=None,
                    finished_at=None,
                ),
            ]
        )
        db_session.flush()

        check_run_id = sync_review_workspace_check_run(
            settings=settings,
            db_session=db_session,
            github_client=fake_github,
            review=review,
            activity="Snapshot queued for the latest push.",
        )

    assert check_run_id == 9001
    assert len(fake_github.check_run_calls) == 1
    assert fake_github.check_run_calls[0]["status"] == "queued"
    assert fake_github.check_run_calls[0]["head_sha"] == "head-sha-new"
    assert "Latest snapshot status: `pending`" in fake_github.check_run_calls[0]["summary"]

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

    assert len(fake_github.check_run_calls) == 3
    running_call = fake_github.check_run_calls[-2]
    assert running_call["status"] == "in_progress"
    assert "Latest snapshot status: `pending`" in running_call["summary"]
    ready_call = fake_github.check_run_calls[-1]
    assert ready_call["status"] == "completed"
    assert ready_call["conclusion"] == "neutral"
    assert "/reviews/octo-org/notebooklens/pulls/7/snapshots/1" in ready_call["details_url"]
    assert "Latest snapshot status: `ready`" in ready_call["summary"]
    engine.dispose()


def test_snapshot_worker_persists_deduplicated_review_assets_and_rewrites_payload(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = get_engine(settings.database_url)
    create_all_tables(engine)
    base_notebook, head_notebook = review_asset_notebook()
    app = create_app()
    fake_github = FakeManagedGitHubClient(
        files=[
            {
                "filename": "analysis/plots.ipynb",
                "status": "modified",
                "size": 4096,
            }
        ],
        contents={
            ("analysis/plots.ipynb", "base-sha"): base_notebook,
            ("analysis/plots.ipynb", "head-sha"): head_notebook,
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
            "X-GitHub-Delivery": "delivery-assets",
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
        snapshot = db_session.scalars(select(ReviewSnapshot)).one()
        stored_assets = db_session.scalars(select(ReviewAsset)).all()
        assert len(stored_assets) == 1
        stored_asset = stored_assets[0]
        assert stored_asset.snapshot_id == snapshot.id
        assert stored_asset.mime_type == "image/png"
        assert stored_asset.byte_size > 0
        assert stored_asset.width == 1
        assert stored_asset.height == 1
        assert stored_asset.storage_key.endswith(f"{stored_asset.sha256}.png")

        notebook = snapshot.snapshot_payload_json["review"]["notebooks"][0]
        rows = {
            row["locator"]["cell_id"]: row
            for row in notebook["render_rows"]
        }
        plot_one_item = rows["plot-one"]["outputs"]["items"][0]
        plot_two_item = rows["plot-two"]["outputs"]["items"][0]
        assert plot_one_item == {
            "kind": "image",
            "asset_id": str(stored_asset.id),
            "mime_type": "image/png",
            "width": 1,
            "height": 1,
            "change_type": "added",
        }
        assert plot_two_item["asset_id"] == str(stored_asset.id)
        assert "asset_key" not in plot_one_item

        svg_item = rows["svg-plot"]["outputs"]["items"][0]
        assert svg_item["kind"] == "placeholder"
        assert "unsupported image format" in svg_item["summary"]

        oversized_item = rows["too-large-plot"]["outputs"]["items"][0]
        assert oversized_item["kind"] == "placeholder"
        assert "exceeds 2097152 bytes" in oversized_item["summary"]

        serialized_payload = json.dumps(snapshot.snapshot_payload_json)
        assert SMALL_PNG_BASE64 not in serialized_payload
        assert "iVBOR" not in serialized_payload

    engine.dispose()


def test_review_assets_route_serves_private_image_bytes_for_authorized_users(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = get_engine(settings.database_url)
    create_all_tables(engine)
    asset_id = create_review_asset_fixture(settings)
    app = create_app()
    fake_oauth = FakeOAuthClient()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_oauth_client] = lambda: fake_oauth
    client = TestClient(app)

    reviewer_session = create_user_session(
        settings,
        github_user_id=101,
        github_login="reviewer",
        access_token="gho_reviewer",
    )
    client.cookies.set(SESSION_COOKIE_NAME, reviewer_session)

    response = client.get(f"/api/review-assets/{asset_id}")

    assert response.status_code == 200
    assert response.content == base64.b64decode(SMALL_PNG_BASE64)
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert fake_oauth.repo_access_checks == [("gho_reviewer", "octo-org", "notebooklens")]

    engine.dispose()


def test_review_assets_route_rejects_users_without_repo_access(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = get_engine(settings.database_url)
    create_all_tables(engine)
    asset_id = create_review_asset_fixture(settings)
    app = create_app()
    fake_oauth = FakeOAuthClient(
        repo_access={("gho_denied", "octo-org", "notebooklens"): False}
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_oauth_client] = lambda: fake_oauth
    client = TestClient(app)

    reviewer_session = create_user_session(
        settings,
        github_user_id=101,
        github_login="reviewer",
        access_token="gho_denied",
    )
    client.cookies.set(SESSION_COOKIE_NAME, reviewer_session)

    response = client.get(f"/api/review-assets/{asset_id}")

    assert response.status_code == 403
    assert response.json() == {"detail": "Repository access denied"}
    assert fake_oauth.repo_access_checks == [("gho_denied", "octo-org", "notebooklens")]

    engine.dispose()


def test_ai_gateway_settings_persist_encrypted_config_and_redact_secrets(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = get_engine(settings.database_url)
    create_all_tables(engine)
    installation_id = create_github_installation_fixture(settings)
    app = create_app()
    fake_oauth = FakeOAuthClient(org_owner_access={("gho_owner", "octo-org"): True})
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_oauth_client] = lambda: fake_oauth
    client = TestClient(app)

    session_id = create_user_session(
        settings,
        github_user_id=101,
        github_login="octo-owner",
        access_token="gho_owner",
    )
    client.cookies.set(SESSION_COOKIE_NAME, session_id)
    payload = {
        "provider_kind": "litellm",
        "display_name": "Internal LiteLLM",
        "github_host_kind": "github_com",
        "github_api_base_url": "https://api.github.com",
        "github_web_base_url": "https://github.com",
        "base_url": "https://litellm.internal.example/v1",
        "model_name": "gpt-4.1",
        "api_key": "Bearer super-secret-token",
        "api_key_header_name": "Authorization",
        "static_headers": {"x-tenant-token": "tenant-secret"},
        "use_responses_api": False,
        "litellm_virtual_key_id": "vk-123",
        "active": True,
    }

    put_response = client.put(
        f"/api/settings/ai-gateway?installation_id={installation_id}",
        json=payload,
    )

    assert put_response.status_code == 200
    put_body = put_response.json()["config"]
    assert put_body["provider_kind"] == "litellm"
    assert put_body["has_api_key"] is True
    assert put_body["static_header_names"] == ["x-tenant-token"]
    assert "api_key" not in put_body
    assert "static_headers" not in put_body

    get_response = client.get(f"/api/settings/ai-gateway?installation_id={installation_id}")

    assert get_response.status_code == 200
    serialized = json.dumps(get_response.json())
    assert "super-secret-token" not in serialized
    assert "tenant-secret" not in serialized
    assert fake_oauth.org_owner_checks == [
        ("gho_owner", "octo-org"),
        ("gho_owner", "octo-org"),
    ]

    with session_scope(settings) as db_session:
        config = db_session.scalars(select(ManagedAiGatewayConfig)).one()
        assert config.provider_kind == ManagedAiGatewayProviderKind.LITELLM
        assert config.github_host_kind == GitHubHostKind.GITHUB_COM
        assert config.api_key_encrypted != "Bearer super-secret-token"
        assert config.static_headers_encrypted_json is not None
        cipher = SessionTokenCipher(settings.encryption_key)
        assert cipher.decrypt(config.api_key_encrypted) == "Bearer super-secret-token"
        assert json.loads(cipher.decrypt(config.static_headers_encrypted_json)) == {
            "x-tenant-token": "tenant-secret"
        }

    engine.dispose()


def test_ai_gateway_settings_reject_non_admin_users(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = get_engine(settings.database_url)
    create_all_tables(engine)
    installation_id = create_github_installation_fixture(settings)
    app = create_app()
    fake_oauth = FakeOAuthClient(org_owner_access={("gho_member", "octo-org"): False})
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_oauth_client] = lambda: fake_oauth
    client = TestClient(app)

    session_id = create_user_session(
        settings,
        github_user_id=202,
        github_login="octo-member",
        access_token="gho_member",
    )
    client.cookies.set(SESSION_COOKIE_NAME, session_id)

    response = client.get(f"/api/settings/ai-gateway?installation_id={installation_id}")

    assert response.status_code == 403
    assert response.json() == {"detail": "Installation admin access required"}
    assert fake_oauth.org_owner_checks == [("gho_member", "octo-org")]
    engine.dispose()


def test_ai_gateway_settings_allow_user_owned_installation_admin_by_login(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = get_engine(settings.database_url)
    create_all_tables(engine)
    installation_id = create_github_installation_fixture(
        settings,
        github_installation_id=21,
        account_login="octocat",
        account_type=InstallationAccountType.USER,
    )
    app = create_app()
    fake_oauth = FakeOAuthClient()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_oauth_client] = lambda: fake_oauth
    client = TestClient(app)

    session_id = create_user_session(
        settings,
        github_user_id=101,
        github_login="octocat",
        access_token="gho_user_admin",
    )
    client.cookies.set(SESSION_COOKIE_NAME, session_id)

    response = client.get(f"/api/settings/ai-gateway?installation_id={installation_id}")

    assert response.status_code == 200
    assert response.json()["config"]["provider_kind"] == "none"
    assert fake_oauth.org_owner_checks == []
    engine.dispose()


def test_ai_gateway_test_route_can_reuse_stored_encrypted_secret(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = get_engine(settings.database_url)
    create_all_tables(engine)
    installation_id = create_github_installation_fixture(settings)
    app = create_app()
    fake_oauth = FakeOAuthClient(org_owner_access={("gho_owner", "octo-org"): True})
    fake_tester = FakeLiteLLMConnectionTester()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_oauth_client] = lambda: fake_oauth
    app.dependency_overrides[get_litellm_connection_tester] = lambda: fake_tester
    client = TestClient(app)

    session_id = create_user_session(
        settings,
        github_user_id=101,
        github_login="octo-owner",
        access_token="gho_owner",
    )
    client.cookies.set(SESSION_COOKIE_NAME, session_id)
    put_payload = {
        "provider_kind": "litellm",
        "display_name": "Internal LiteLLM",
        "github_host_kind": "ghes",
        "github_api_base_url": "https://ghes-api.example.test",
        "github_web_base_url": "https://ghes.example.test",
        "base_url": "https://litellm.internal.example/v1",
        "model_name": "claude-sonnet",
        "api_key": "Bearer reusable-secret",
        "api_key_header_name": "Authorization",
        "static_headers": {"x-tenant-token": "tenant-secret"},
        "use_responses_api": True,
        "litellm_virtual_key_id": "vk-456",
        "active": False,
    }
    put_response = client.put(
        f"/api/settings/ai-gateway?installation_id={installation_id}",
        json=put_payload,
    )
    assert put_response.status_code == 200

    test_payload = {
        "provider_kind": "litellm",
        "display_name": "Internal LiteLLM",
        "github_host_kind": "ghes",
        "github_api_base_url": "https://ghes-api.example.test",
        "github_web_base_url": "https://ghes.example.test",
        "base_url": "https://litellm.internal.example/v1",
        "model_name": "claude-sonnet",
        "api_key_header_name": "Authorization",
        "use_responses_api": True,
        "litellm_virtual_key_id": "vk-456",
        "active": False,
    }

    response = client.post(
        f"/api/settings/ai-gateway/test?installation_id={installation_id}",
        json=test_payload,
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "provider_kind": "litellm",
        "model_name": "claude-sonnet",
        "tested_endpoint": "/responses",
    }
    assert fake_tester.calls == [
        {
            "base_url": "https://litellm.internal.example/v1",
            "model_name": "claude-sonnet",
            "api_key_header_name": "Authorization",
            "api_key": "Bearer reusable-secret",
            "static_headers": {"x-tenant-token": "tenant-secret"},
            "use_responses_api": True,
        }
    ]

    engine.dispose()


def test_snapshot_worker_uses_active_litellm_gateway_when_enabled(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = get_engine(settings.database_url)
    create_all_tables(engine)
    app = create_app()
    fake_github = FakeManagedGitHubClient(
        files=[
            {
                "filename": REVIEW_WORKSPACE_THREAD_PATH,
                "status": "modified",
                "size": 2048,
            }
        ],
        contents={
            (REVIEW_WORKSPACE_THREAD_PATH, "base-sha"): fixture_text(
                "review_workspace_thread_base.ipynb"
            ),
            (REVIEW_WORKSPACE_THREAD_PATH, "head-sha"): fixture_text(
                "review_workspace_thread_head_v1.ipynb"
            ),
        },
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_managed_github_client] = lambda: fake_github
    client = TestClient(app)

    response = post_pull_request_webhook(
        client,
        payload=pull_request_payload(head_sha="head-sha"),
        delivery_id="delivery-litellm-success",
    )
    assert response.status_code == 202

    with session_scope(settings) as db_session:
        installation_id = str(db_session.scalars(select(GitHubInstallation.id)).one())

    create_managed_ai_gateway_fixture(settings, installation_id=installation_id)
    fake_gateway = FakeLiteLLMGatewayClient(
        responses=[
            json.dumps(
                {
                    "summary": "AI summary",
                    "flagged_issues": [
                        {
                            "notebook_path": REVIEW_WORKSPACE_THREAD_PATH,
                            "locator": {
                                "cell_id": None,
                                "base_index": None,
                                "head_index": None,
                                "display_index": None,
                            },
                            "code": "ai:output_shift",
                            "category": "output",
                            "severity": "medium",
                            "confidence": "medium",
                            "message": "Explain the output shift before merge.",
                        }
                    ],
                    "reviewer_guidance": [
                        {
                            "notebook_path": REVIEW_WORKSPACE_THREAD_PATH,
                            "locator": None,
                            "code": "claude:explain_output_shift",
                            "source": "claude",
                            "label": "AI",
                            "priority": "medium",
                            "message": "Explain the output shift in surrounding markdown.",
                        }
                    ],
                }
            )
        ]
    )

    with session_scope(settings) as db_session:
        result = run_snapshot_build_worker_once(
            settings=settings,
            db_session=db_session,
            github_client=fake_github,
            litellm_client=fake_gateway,
        )

    assert result.status == "succeeded"
    assert len(fake_gateway.calls) == 1
    assert fake_gateway.calls[0]["api_key"] == "Bearer managed-secret"
    assert fake_gateway.calls[0]["static_headers"] == {"x-tenant-token": "tenant-secret"}

    with session_scope(settings) as db_session:
        snapshot = db_session.scalars(select(ReviewSnapshot)).one()
        assert snapshot.status == ReviewSnapshotStatus.READY
        assert snapshot.summary_text == "AI summary"
        assert snapshot.flagged_findings_json[0]["message"] == "Explain the output shift before merge."
        assert any(
            item["message"] == "Explain the output shift in surrounding markdown."
            for item in snapshot.reviewer_guidance_json
        )

    engine.dispose()


def test_snapshot_worker_falls_back_to_deterministic_review_when_litellm_fails(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    engine = get_engine(settings.database_url)
    create_all_tables(engine)
    app = create_app()
    fake_github = FakeManagedGitHubClient(
        files=[
            {
                "filename": REVIEW_WORKSPACE_THREAD_PATH,
                "status": "modified",
                "size": 2048,
            }
        ],
        contents={
            (REVIEW_WORKSPACE_THREAD_PATH, "base-sha"): fixture_text(
                "review_workspace_thread_base.ipynb"
            ),
            (REVIEW_WORKSPACE_THREAD_PATH, "head-sha"): fixture_text(
                "review_workspace_thread_head_v1.ipynb"
            ),
        },
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_managed_github_client] = lambda: fake_github
    client = TestClient(app)

    response = post_pull_request_webhook(
        client,
        payload=pull_request_payload(head_sha="head-sha"),
        delivery_id="delivery-litellm-fallback",
    )
    assert response.status_code == 202

    with session_scope(settings) as db_session:
        installation_id = str(db_session.scalars(select(GitHubInstallation.id)).one())

    create_managed_ai_gateway_fixture(settings, installation_id=installation_id)
    fake_gateway = FakeLiteLLMGatewayClient(error="gateway exploded")

    with session_scope(settings) as db_session:
        result = run_snapshot_build_worker_once(
            settings=settings,
            db_session=db_session,
            github_client=fake_github,
            litellm_client=fake_gateway,
        )

    assert result.status == "succeeded"
    assert len(fake_gateway.calls) == 2

    with session_scope(settings) as db_session:
        snapshot = db_session.scalars(select(ReviewSnapshot)).one()
        assert snapshot.status == ReviewSnapshotStatus.READY
        assert snapshot.summary_text is not None
        assert "Managed LiteLLM review unavailable: gateway exploded." in snapshot.summary_text
        assert (
            "Managed LiteLLM review unavailable: gateway exploded. Used deterministic local findings."
            in snapshot.snapshot_payload_json["review"]["notices"]
        )

    engine.dispose()


def test_rebuild_latest_route_requires_installation_admin(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = get_engine(settings.database_url)
    create_all_tables(engine)
    review_id, _installation_id = create_ready_review_fixture(settings)
    app = create_app()
    fake_oauth = FakeOAuthClient(org_owner_access={("gho_member", "octo-org"): False})
    fake_github = FakeManagedGitHubClient()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_oauth_client] = lambda: fake_oauth
    app.dependency_overrides[get_managed_github_client] = lambda: fake_github
    client = TestClient(app)

    session_id = create_user_session(
        settings,
        github_user_id=202,
        github_login="octo-member",
        access_token="gho_member",
    )
    client.cookies.set(SESSION_COOKIE_NAME, session_id)

    response = client.post(f"/api/reviews/{review_id}/rebuild-latest")

    assert response.status_code == 403
    assert response.json() == {"detail": "Installation admin access required"}

    with session_scope(settings) as db_session:
        assert db_session.scalars(select(SnapshotBuildJob)).all() == []

    engine.dispose()


def test_rebuild_latest_route_queues_force_rebuild_job_for_installation_admin(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    engine = get_engine(settings.database_url)
    create_all_tables(engine)
    review_id, _installation_id = create_ready_review_fixture(settings)
    app = create_app()
    fake_oauth = FakeOAuthClient(org_owner_access={("gho_owner", "octo-org"): True})
    fake_github = FakeManagedGitHubClient()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_oauth_client] = lambda: fake_oauth
    app.dependency_overrides[get_managed_github_client] = lambda: fake_github
    client = TestClient(app)

    session_id = create_user_session(
        settings,
        github_user_id=101,
        github_login="octo-owner",
        access_token="gho_owner",
    )
    client.cookies.set(SESSION_COOKIE_NAME, session_id)

    response = client.post(f"/api/reviews/{review_id}/rebuild-latest")

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    assert response.json()["force_rebuild"] is True

    with session_scope(settings) as db_session:
        review = db_session.get(ManagedReview, uuid.UUID(response.json()["review_id"]))
        jobs = db_session.scalars(select(SnapshotBuildJob)).all()
        assert review is not None
        assert review.status == ManagedReviewStatus.PENDING
        assert len(jobs) == 1
        assert jobs[0].force_rebuild is True
        assert jobs[0].base_sha == "base-sha"
        assert jobs[0].head_sha == "head-sha"

    assert fake_github.check_run_calls[-1]["status"] == "queued"
    assert "Snapshot rebuild queued for the latest push." in fake_github.check_run_calls[-1]["summary"]
    engine.dispose()


def test_force_rebuild_job_creates_new_snapshot_even_for_same_sha(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = get_engine(settings.database_url)
    create_all_tables(engine)
    review_id, _installation_id = create_ready_review_fixture(
        settings,
        head_sha="head-sha-rebuild",
    )
    fake_github = FakeManagedGitHubClient(
        files=[
            {
                "filename": REVIEW_WORKSPACE_THREAD_PATH,
                "status": "modified",
                "size": 2048,
            }
        ],
        contents={
            (REVIEW_WORKSPACE_THREAD_PATH, "base-sha"): fixture_text(
                "review_workspace_thread_base.ipynb"
            ),
            (REVIEW_WORKSPACE_THREAD_PATH, "head-sha-rebuild"): fixture_text(
                "review_workspace_thread_head_v1.ipynb"
            ),
        },
    )

    with session_scope(settings) as db_session:
        review = db_session.get(ManagedReview, uuid.UUID(review_id))
        assert review is not None
        enqueue_snapshot_build_job(
            db_session,
            managed_review_id=review.id,
            base_sha="base-sha",
            head_sha="head-sha-rebuild",
            force_rebuild=True,
        )

    with session_scope(settings) as db_session:
        result = run_snapshot_build_worker_once(
            settings=settings,
            db_session=db_session,
            github_client=fake_github,
        )

    assert result.status == "succeeded"
    assert result.reused_snapshot is False
    assert result.snapshot_index == 2

    with session_scope(settings) as db_session:
        snapshots = db_session.scalars(
            select(ReviewSnapshot).order_by(ReviewSnapshot.snapshot_index.asc())
        ).all()
        assert [snapshot.snapshot_index for snapshot in snapshots] == [1, 2]

    engine.dispose()


def test_resolve_mirror_auth_context_prefers_user_token_and_falls_back_to_app_auth(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    engine = get_engine(settings.database_url)
    create_all_tables(engine)

    valid_session_id = create_user_session(
        settings,
        github_user_id=101,
        github_login="reviewer",
        access_token="gho_reviewer",
    )
    assert valid_session_id

    create_user_session(
        settings,
        github_user_id=202,
        github_login="expired-reviewer",
        access_token="gho_expired",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )

    store = OAuthSessionStore(SessionTokenCipher(settings.session_secret))
    with session_scope(settings) as db_session:
        user_auth = resolve_mirror_auth_context(
            db_session=db_session,
            github_user_id=101,
            session_store=store,
        )
        fallback_auth = resolve_mirror_auth_context(
            db_session=db_session,
            github_user_id=202,
            session_store=store,
        )

    assert user_auth.mode == "user"
    assert user_auth.access_token == "gho_reviewer"
    assert user_auth.fallback_reason is None
    assert fallback_auth.mode == "app"
    assert fallback_auth.access_token is None
    assert fallback_auth.fallback_reason == "user_token_unavailable"

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


def test_churn_model_review_workspace_end_to_end_flow_covers_threads_notifications_and_carry_forward(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    engine = get_engine(settings.database_url)
    create_all_tables(engine)
    app = create_app()
    fake_github = FakeManagedGitHubClient(
        files=[
            {
                "filename": REVIEW_WORKSPACE_THREAD_PATH,
                "status": "modified",
                "size": 2048,
            }
        ],
        contents={
            (REVIEW_WORKSPACE_THREAD_PATH, "base-sha"): fixture_text(
                "review_workspace_thread_base.ipynb"
            ),
            (REVIEW_WORKSPACE_THREAD_PATH, "head-sha-1"): fixture_text(
                "review_workspace_thread_head_v1.ipynb"
            ),
            (REVIEW_WORKSPACE_THREAD_PATH, "head-sha-2"): fixture_text(
                "review_workspace_thread_head_v2.ipynb"
            ),
        },
    )
    fake_oauth = FakeOAuthClient(
        users_by_token={
            "reviewer-token": GitHubOAuthUser(
                id=101,
                login="reviewer",
                email="reviewer@example.test",
            ),
            "reviewer-2-token": GitHubOAuthUser(
                id=303,
                login="reviewer-2",
                email="reviewer2@example.test",
            ),
            "author-token": GitHubOAuthUser(
                id=202,
                login="pr-author",
                email="author@example.test",
            ),
        }
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_managed_github_client] = lambda: fake_github
    app.dependency_overrides[get_oauth_client] = lambda: fake_oauth
    client = TestClient(app)

    reviewer_session = create_user_session(
        settings,
        github_user_id=101,
        github_login="reviewer",
        access_token="reviewer-token",
    )
    reviewer_two_session = create_user_session(
        settings,
        github_user_id=303,
        github_login="reviewer-2",
        access_token="reviewer-2-token",
    )
    create_user_session(
        settings,
        github_user_id=202,
        github_login="pr-author",
        access_token="author-token",
    )

    first_response = post_pull_request_webhook(
        client,
        payload=pull_request_payload(head_sha="head-sha-1"),
        delivery_id="delivery-1",
    )
    assert first_response.status_code == 202
    assert first_response.json()["status"] == "accepted"
    assert first_response.json()["job_id"] is not None
    assert first_response.json()["check_run_id"] == 9001
    assert len(fake_github.check_run_calls) == 1
    first_pending_call = fake_github.check_run_calls[-1]
    assert first_pending_call["head_sha"] == "head-sha-1"
    assert first_pending_call["status"] == "queued"
    assert "Latest snapshot status: `pending`" in first_pending_call["summary"]
    assert "Threads: 0 unresolved, 0 resolved, 0 outdated" in first_pending_call["summary"]

    with session_scope(settings) as db_session:
        review = db_session.scalars(select(ManagedReview)).one()
        jobs = db_session.scalars(
            select(SnapshotBuildJob).order_by(SnapshotBuildJob.scheduled_at.asc())
        ).all()
        assert review.status == ManagedReviewStatus.PENDING
        assert len(jobs) == 1
        assert jobs[0].status == SnapshotBuildJobStatus.QUEUED

    first_result = run_managed_snapshot_worker(settings=settings, github_client=fake_github)
    assert first_result.status == "succeeded"
    assert first_result.snapshot_index == 1
    assert len(fake_github.check_run_calls) == 3
    first_running_call = fake_github.check_run_calls[-2]
    assert first_running_call["status"] == "in_progress"
    assert "Latest snapshot status: `pending`" in first_running_call["summary"]
    first_ready_call = fake_github.check_run_calls[-1]
    assert first_ready_call["status"] == "completed"
    assert first_ready_call["conclusion"] == "neutral"
    assert "/reviews/octo-org/notebooklens/pulls/7/snapshots/1" in first_ready_call["details_url"]
    assert "Latest snapshot status: `ready`" in first_ready_call["summary"]
    assert "Threads: 0 unresolved, 0 resolved, 0 outdated" in first_ready_call["summary"]

    client.cookies.set(SESSION_COOKIE_NAME, reviewer_session)
    workspace_v1 = client.get("/api/reviews/octo-org/notebooklens/pulls/7")
    assert workspace_v1.status_code == 200
    workspace_v1_json = workspace_v1.json()
    assert workspace_v1_json["review"]["selected_snapshot_index"] == 1
    assert workspace_v1_json["review"]["status"] == "ready"
    metric_row = review_row_for_cell(workspace_v1_json, cell_id="metric-cell")
    assert metric_row["outputs"]["changed"] is True
    assert metric_row["summary"] == "cell outputs changed"
    assert metric_row["source"]["changed"] is False

    invalid_create_response = client.post(
        f"/api/reviews/{workspace_v1_json['review']['id']}/threads",
        json={
            "snapshot_id": workspace_v1_json["snapshot"]["id"],
            "anchor": metric_row["thread_anchors"]["source"],
            "body_markdown": "This should be rejected because the source block did not change.",
        },
    )
    assert invalid_create_response.status_code == 400
    assert invalid_create_response.json()["detail"] == "Threads can only be created on changed blocks"

    create_response = client.post(
        f"/api/reviews/{workspace_v1_json['review']['id']}/threads",
        json={
            "snapshot_id": workspace_v1_json["snapshot"]["id"],
            "anchor": metric_row["thread_anchors"]["outputs"],
            "body_markdown": "Explain the regression and update the notebook narrative.",
        },
    )
    assert create_response.status_code == 201
    created_thread = create_response.json()["thread"]
    thread_id = created_thread["id"]
    assert created_thread["status"] == "open"
    assert len(created_thread["messages"]) == 1
    assert len(fake_github.check_run_calls) == 4
    created_call = fake_github.check_run_calls[-1]
    assert "Threads: 1 unresolved, 0 resolved, 0 outdated" in created_call["summary"]
    assert (
        f"Activity: reviewer created a thread on `{REVIEW_WORKSPACE_THREAD_PATH}`."
        in created_call["summary"]
    )

    client.cookies.set(SESSION_COOKIE_NAME, reviewer_two_session)
    reply_response = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"body_markdown": "The output still needs narrative context for the regression."},
    )
    assert reply_response.status_code == 201
    replied_thread = reply_response.json()["thread"]
    assert len(replied_thread["messages"]) == 2
    assert len(fake_github.check_run_calls) == 5
    reply_call = fake_github.check_run_calls[-1]
    assert "Threads: 1 unresolved, 0 resolved, 0 outdated" in reply_call["summary"]
    assert (
        f"Activity: reviewer-2 replied to a thread on `{REVIEW_WORKSPACE_THREAD_PATH}`."
        in reply_call["summary"]
    )

    client.cookies.set(SESSION_COOKIE_NAME, reviewer_session)
    resolve_response = client.post(f"/api/threads/{thread_id}/resolve")
    assert resolve_response.status_code == 200
    assert resolve_response.json()["thread"]["status"] == "resolved"
    assert len(fake_github.check_run_calls) == 6
    resolve_call = fake_github.check_run_calls[-1]
    assert "Threads: 0 unresolved, 1 resolved, 0 outdated" in resolve_call["summary"]
    assert (
        f"Activity: reviewer resolved the thread on `{REVIEW_WORKSPACE_THREAD_PATH}`."
        in resolve_call["summary"]
    )

    snapshot_one_after_resolve = client.get(
        "/api/reviews/octo-org/notebooklens/pulls/7/snapshots/1"
    )
    assert snapshot_one_after_resolve.status_code == 200
    assert snapshot_one_after_resolve.json()["threads"][0]["status"] == "resolved"

    reopen_response = client.post(f"/api/threads/{thread_id}/reopen")
    assert reopen_response.status_code == 200
    assert reopen_response.json()["thread"]["status"] == "open"
    assert len(fake_github.check_run_calls) == 7
    reopen_call = fake_github.check_run_calls[-1]
    assert "Threads: 1 unresolved, 0 resolved, 0 outdated" in reopen_call["summary"]
    assert (
        f"Activity: reviewer reopened the thread on `{REVIEW_WORKSPACE_THREAD_PATH}`."
        in reopen_call["summary"]
    )

    with session_scope(settings) as db_session:
        notifications = db_session.scalars(
            select(NotificationOutbox).order_by(NotificationOutbox.created_at.asc())
        ).all()
        assert [
            (item.event_type.value, item.recipient_github_user_id, item.recipient_email)
            for item in notifications
        ] == [
            ("thread_created", 202, "author@example.test"),
            ("reply_added", 101, "reviewer@example.test"),
            ("reply_added", 202, "author@example.test"),
            ("thread_resolved", 202, "author@example.test"),
            ("thread_resolved", 303, "reviewer2@example.test"),
            ("thread_reopened", 202, "author@example.test"),
            ("thread_reopened", 303, "reviewer2@example.test"),
        ]
        assert all(item.delivery_state == NotificationDeliveryState.PENDING for item in notifications)

    second_response = post_pull_request_webhook(
        client,
        payload=pull_request_payload(action="synchronize", head_sha="head-sha-2"),
        delivery_id="delivery-2",
    )
    assert second_response.status_code == 202
    assert second_response.json()["job_id"] is not None
    assert len(fake_github.check_run_calls) == 8
    second_pending_call = fake_github.check_run_calls[-1]
    assert second_pending_call["head_sha"] == "head-sha-2"
    assert second_pending_call["status"] == "queued"
    assert "Latest snapshot status: `pending`" in second_pending_call["summary"]
    assert "Threads: 1 unresolved, 0 resolved, 0 outdated" in second_pending_call["summary"]

    with session_scope(settings) as db_session:
        jobs = db_session.scalars(
            select(SnapshotBuildJob).order_by(SnapshotBuildJob.scheduled_at.asc())
        ).all()
        assert len(jobs) == 2
        assert jobs[-1].status == SnapshotBuildJobStatus.QUEUED

    second_result = run_managed_snapshot_worker(settings=settings, github_client=fake_github)
    assert second_result.status == "succeeded"
    assert second_result.snapshot_index == 2
    assert len(fake_github.check_run_calls) == 10
    second_running_call = fake_github.check_run_calls[-2]
    assert second_running_call["status"] == "in_progress"
    assert "Latest snapshot status: `pending`" in second_running_call["summary"]
    second_ready_call = fake_github.check_run_calls[-1]
    assert second_ready_call["status"] == "completed"
    assert second_ready_call["conclusion"] == "neutral"
    assert "/reviews/octo-org/notebooklens/pulls/7/snapshots/2" in second_ready_call["details_url"]
    assert "Latest snapshot status: `ready`" in second_ready_call["summary"]
    assert "Threads: 1 unresolved, 0 resolved, 0 outdated" in second_ready_call["summary"]

    latest_workspace = client.get("/api/reviews/octo-org/notebooklens/pulls/7")
    assert latest_workspace.status_code == 200
    latest_workspace_json = latest_workspace.json()
    assert latest_workspace_json["review"]["selected_snapshot_index"] == 2
    assert latest_workspace_json["review"]["latest_snapshot_index"] == 2
    assert latest_workspace_json["review"]["thread_counts"] == {
        "unresolved": 1,
        "resolved": 0,
        "outdated": 0,
    }
    assert len(latest_workspace_json["review"]["snapshot_history"]) == 2
    latest_intro_row = review_row_for_cell(latest_workspace_json, cell_id="intro-cell")
    latest_metric_row = review_row_for_cell(latest_workspace_json, cell_id="metric-cell")
    assert "train_v2.csv" in latest_intro_row["source"]["head"]
    assert latest_metric_row["summary"] == "cell outputs changed"
    assert latest_metric_row["thread_anchors"]["outputs"] == metric_row["thread_anchors"]["outputs"]
    assert len(latest_workspace_json["threads"]) == 1
    latest_thread = latest_workspace_json["threads"][0]
    assert latest_thread["id"] == thread_id
    assert latest_thread["status"] == "open"
    assert latest_thread["carried_forward"] is True
    assert latest_thread["current_snapshot_id"] == latest_workspace_json["snapshot"]["id"]
    assert len(latest_thread["messages"]) == 2
    assert latest_thread["anchor"] == latest_metric_row["thread_anchors"]["outputs"]

    origin_workspace = client.get("/api/reviews/octo-org/notebooklens/pulls/7/snapshots/1")
    assert origin_workspace.status_code == 200
    origin_thread = origin_workspace.json()["threads"][0]
    assert origin_thread["id"] == thread_id
    assert origin_thread["status"] == "open"
    assert len(origin_thread["messages"]) == 2
    assert origin_thread["anchor"] == metric_row["thread_anchors"]["outputs"]

    final_resolve_response = client.post(f"/api/threads/{thread_id}/resolve")
    assert final_resolve_response.status_code == 200
    assert final_resolve_response.json()["thread"]["status"] == "resolved"
    assert len(fake_github.check_run_calls) == 11
    final_resolve_call = fake_github.check_run_calls[-1]
    assert "Threads: 0 unresolved, 1 resolved, 0 outdated" in final_resolve_call["summary"]
    assert (
        f"Activity: reviewer resolved the thread on `{REVIEW_WORKSPACE_THREAD_PATH}`."
        in final_resolve_call["summary"]
    )

    latest_snapshot_after_resolve = client.get("/api/reviews/octo-org/notebooklens/pulls/7")
    assert latest_snapshot_after_resolve.status_code == 200
    latest_snapshot_after_resolve_json = latest_snapshot_after_resolve.json()
    assert latest_snapshot_after_resolve_json["review"]["thread_counts"] == {
        "unresolved": 0,
        "resolved": 1,
        "outdated": 0,
    }
    assert latest_snapshot_after_resolve_json["threads"][0]["status"] == "resolved"

    with session_scope(settings) as db_session:
        thread = db_session.scalars(select(ReviewThread)).one()
        latest_snapshot = db_session.scalars(
            select(ReviewSnapshot).where(ReviewSnapshot.snapshot_index == 2)
        ).one()
        notifications = db_session.scalars(
            select(NotificationOutbox).order_by(NotificationOutbox.created_at.asc())
        ).all()
        assert thread.status == ReviewThreadStatus.RESOLVED
        assert thread.carried_forward is True
        assert thread.origin_anchor_json == metric_row["thread_anchors"]["outputs"]
        assert thread.anchor_json == latest_metric_row["thread_anchors"]["outputs"]
        assert thread.current_snapshot_id == latest_snapshot.id
        assert [
            (item.event_type.value, item.recipient_github_user_id)
            for item in notifications
        ] == [
            ("thread_created", 202),
            ("reply_added", 101),
            ("reply_added", 202),
            ("thread_resolved", 202),
            ("thread_resolved", 303),
            ("thread_reopened", 202),
            ("thread_reopened", 303),
            ("thread_resolved", 202),
            ("thread_resolved", 303),
        ]

    engine.dispose()


def test_review_workspace_routes_persist_threads_notifications_and_snapshot_history(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = get_engine(settings.database_url)
    create_all_tables(engine)
    app = create_app()
    fake_github = FakeManagedGitHubClient(
        files=[
            {
                "filename": REVIEW_WORKSPACE_THREAD_PATH,
                "status": "modified",
                "size": 2048,
            }
        ],
        contents={
            (REVIEW_WORKSPACE_THREAD_PATH, "base-sha"): fixture_text(
                "review_workspace_thread_base.ipynb"
            ),
            (REVIEW_WORKSPACE_THREAD_PATH, "head-sha-1"): fixture_text(
                "review_workspace_thread_head_v1.ipynb"
            ),
        },
    )
    fake_oauth = FakeOAuthClient(
        users_by_token={
            "reviewer-token": GitHubOAuthUser(
                id=101,
                login="reviewer",
                email="reviewer@example.test",
            ),
            "author-token": GitHubOAuthUser(
                id=202,
                login="pr-author",
                email="author@example.test",
            ),
        }
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_managed_github_client] = lambda: fake_github
    app.dependency_overrides[get_oauth_client] = lambda: fake_oauth
    client = TestClient(app)

    payload = pull_request_payload(head_sha="head-sha-1")
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

    reviewer_session = create_user_session(
        settings,
        github_user_id=101,
        github_login="reviewer",
        access_token="reviewer-token",
    )
    author_session = create_user_session(
        settings,
        github_user_id=202,
        github_login="pr-author",
        access_token="author-token",
    )

    client.cookies.set(SESSION_COOKIE_NAME, reviewer_session)
    review_response = client.get("/api/reviews/octo-org/notebooklens/pulls/7")
    assert review_response.status_code == 200
    workspace = review_response.json()
    assert workspace["review"]["selected_snapshot_index"] == 1
    assert len(workspace["review"]["snapshot_history"]) == 1
    notebook = workspace["snapshot"]["payload"]["review"]["notebooks"][0]
    assert notebook["path"] == REVIEW_WORKSPACE_THREAD_PATH

    row = next(
        item
        for item in notebook["render_rows"]
        if item["locator"]["cell_id"] == "metric-cell"
    )
    assert row["outputs"]["changed"] is True
    assert row["source"]["changed"] is False
    anchor = row["thread_anchors"]["outputs"]
    thread_response = client.post(
        f"/api/reviews/{workspace['review']['id']}/threads",
        json={
            "snapshot_id": workspace["snapshot"]["id"],
            "anchor": anchor,
            "body_markdown": "Explain the regression and update the notebook narrative.",
        },
    )
    assert thread_response.status_code == 201
    thread = thread_response.json()["thread"]
    assert thread["status"] == "open"
    assert len(thread["messages"]) == 1
    assert len(fake_github.check_run_calls) == 4
    assert fake_github.check_run_calls[-1]["check_run_id"] == 9001
    assert "Threads: 1 unresolved, 0 resolved, 0 outdated" in fake_github.check_run_calls[-1][
        "summary"
    ]
    assert (
        f"Activity: reviewer created a thread on `{REVIEW_WORKSPACE_THREAD_PATH}`."
        in fake_github.check_run_calls[-1]["summary"]
    )

    with session_scope(settings) as db_session:
        stored_thread = db_session.scalars(select(ReviewThread)).one()
        notifications = db_session.scalars(
            select(NotificationOutbox).order_by(NotificationOutbox.created_at.asc())
        ).all()
        assert stored_thread.status == ReviewThreadStatus.OPEN
        assert len(notifications) == 1
        assert notifications[0].recipient_github_user_id == 202
        assert notifications[0].recipient_email == "author@example.test"

    client.cookies.set(SESSION_COOKIE_NAME, author_session)
    reply_response = client.post(
        f"/api/threads/{thread['id']}/messages",
        json={"body_markdown": "I will update the narrative in the next push."},
    )
    assert reply_response.status_code == 201
    assert len(reply_response.json()["thread"]["messages"]) == 2
    assert len(fake_github.check_run_calls) == 5
    assert "Threads: 1 unresolved, 0 resolved, 0 outdated" in fake_github.check_run_calls[-1][
        "summary"
    ]
    assert (
        f"Activity: pr-author replied to a thread on `{REVIEW_WORKSPACE_THREAD_PATH}`."
        in fake_github.check_run_calls[-1]["summary"]
    )

    client.cookies.set(SESSION_COOKIE_NAME, reviewer_session)
    resolve_response = client.post(f"/api/threads/{thread['id']}/resolve")
    assert resolve_response.status_code == 200
    assert resolve_response.json()["thread"]["status"] == "resolved"
    assert len(fake_github.check_run_calls) == 6
    assert "Threads: 0 unresolved, 1 resolved, 0 outdated" in fake_github.check_run_calls[-1][
        "summary"
    ]
    assert (
        f"Activity: reviewer resolved the thread on `{REVIEW_WORKSPACE_THREAD_PATH}`."
        in fake_github.check_run_calls[-1]["summary"]
    )
    second_resolve_response = client.post(f"/api/threads/{thread['id']}/resolve")
    assert second_resolve_response.status_code == 200
    assert len(fake_github.check_run_calls) == 6
    reopen_response = client.post(f"/api/threads/{thread['id']}/reopen")
    assert reopen_response.status_code == 200
    assert reopen_response.json()["thread"]["status"] == "open"
    assert len(fake_github.check_run_calls) == 7
    assert "Threads: 1 unresolved, 0 resolved, 0 outdated" in fake_github.check_run_calls[-1][
        "summary"
    ]
    assert (
        f"Activity: reviewer reopened the thread on `{REVIEW_WORKSPACE_THREAD_PATH}`."
        in fake_github.check_run_calls[-1]["summary"]
    )
    second_reopen_response = client.post(f"/api/threads/{thread['id']}/reopen")
    assert second_reopen_response.status_code == 200
    assert second_reopen_response.json()["thread"]["status"] == "open"
    assert len(fake_github.check_run_calls) == 7

    snapshot_response = client.get("/api/reviews/octo-org/notebooklens/pulls/7/snapshots/1")
    assert snapshot_response.status_code == 200
    assert len(snapshot_response.json()["threads"]) == 1

    with session_scope(settings) as db_session:
        messages = db_session.scalars(select(ThreadMessage).order_by(ThreadMessage.created_at.asc())).all()
        notifications = db_session.scalars(
            select(NotificationOutbox).order_by(NotificationOutbox.created_at.asc())
        ).all()
        assert len(messages) == 2
        assert len(notifications) == 4
        assert [item.event_type.value for item in notifications] == [
            "thread_created",
            "reply_added",
            "thread_resolved",
            "thread_reopened",
        ]
        assert notifications[1].recipient_github_user_id == 101
        assert notifications[2].recipient_github_user_id == 202
        assert notifications[3].recipient_github_user_id == 202

    engine.dispose()


def test_notification_delivery_worker_sends_and_marks_pending_thread_event_emails(
    tmp_path: Path,
) -> None:
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
            ("analysis/notebook.ipynb", "base-sha"): review_thread_notebook("0.81"),
            ("analysis/notebook.ipynb", "head-sha-1"): review_thread_notebook("0.73"),
        },
    )
    fake_oauth = FakeOAuthClient(
        users_by_token={
            "reviewer-token": GitHubOAuthUser(
                id=101,
                login="reviewer",
                email="reviewer@example.test",
            ),
            "author-token": GitHubOAuthUser(
                id=202,
                login="pr-author",
                email="author@example.test",
            ),
        }
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_managed_github_client] = lambda: fake_github
    app.dependency_overrides[get_oauth_client] = lambda: fake_oauth
    client = TestClient(app)

    payload = pull_request_payload(head_sha="head-sha-1")
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

    reviewer_session = create_user_session(
        settings,
        github_user_id=101,
        github_login="reviewer",
        access_token="reviewer-token",
    )
    author_session = create_user_session(
        settings,
        github_user_id=202,
        github_login="pr-author",
        access_token="author-token",
    )

    client.cookies.set(SESSION_COOKIE_NAME, reviewer_session)
    workspace = client.get("/api/reviews/octo-org/notebooklens/pulls/7").json()
    row = next(
        item
        for item in workspace["snapshot"]["payload"]["review"]["notebooks"][0]["render_rows"]
        if item["outputs"]["changed"]
    )
    anchor = row["thread_anchors"]["outputs"]
    thread_response = client.post(
        f"/api/reviews/{workspace['review']['id']}/threads",
        json={
            "snapshot_id": workspace["snapshot"]["id"],
            "anchor": anchor,
            "body_markdown": "Explain the regression and update the notebook narrative.",
        },
    )
    assert thread_response.status_code == 201
    thread_id = thread_response.json()["thread"]["id"]

    client.cookies.set(SESSION_COOKIE_NAME, author_session)
    assert (
        client.post(
            f"/api/threads/{thread_id}/messages",
            json={"body_markdown": "I will update the narrative in the next push."},
        ).status_code
        == 201
    )
    client.cookies.set(SESSION_COOKIE_NAME, reviewer_session)
    assert client.post(f"/api/threads/{thread_id}/resolve").status_code == 200
    assert client.post(f"/api/threads/{thread_id}/reopen").status_code == 200

    fake_email = FakeEmailClient()
    delivery_result = process_notification_delivery_once(
        settings=settings,
        email_client=fake_email,
        limit=10,
    )
    assert delivery_result.processed == 4
    assert delivery_result.sent == 4
    assert delivery_result.failed == 0
    assert [message.subject for message in fake_email.sent_messages] == [
        "[NotebookLens] New thread on octo-org/notebooklens#7",
        "[NotebookLens] New reply on octo-org/notebooklens#7",
        "[NotebookLens] Thread resolved on octo-org/notebooklens#7",
        "[NotebookLens] Thread reopened on octo-org/notebooklens#7",
    ]
    assert "Open in NotebookLens: https://notebooklens.test/reviews/octo-org/notebooklens/pulls/7/snapshots/1" in fake_email.sent_messages[0].text_body
    assert "Explain the regression and update the notebook narrative." in fake_email.sent_messages[0].text_body
    assert "I will update the narrative in the next push." not in fake_email.sent_messages[0].text_body
    assert "I will update the narrative in the next push." in fake_email.sent_messages[1].text_body
    assert "Latest thread message:" not in fake_email.sent_messages[2].text_body
    assert "Latest thread message:" not in fake_email.sent_messages[3].text_body

    with session_scope(settings) as db_session:
        notifications = db_session.scalars(
            select(NotificationOutbox).order_by(NotificationOutbox.created_at.asc())
        ).all()
        assert [item.delivery_state for item in notifications] == [
            NotificationDeliveryState.SENT,
            NotificationDeliveryState.SENT,
            NotificationDeliveryState.SENT,
            NotificationDeliveryState.SENT,
        ]
        assert all(item.sent_at is not None for item in notifications)
        assert all(item.attempt_count == 1 for item in notifications)

    engine.dispose()


def test_notification_delivery_worker_marks_failed_rows_when_email_send_errors(
    tmp_path: Path,
) -> None:
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
            ("analysis/notebook.ipynb", "base-sha"): review_thread_notebook("0.81"),
            ("analysis/notebook.ipynb", "head-sha-1"): review_thread_notebook("0.73"),
        },
    )
    fake_oauth = FakeOAuthClient(
        users_by_token={
            "reviewer-token": GitHubOAuthUser(
                id=101,
                login="reviewer",
                email="reviewer@example.test",
            ),
            "author-token": GitHubOAuthUser(
                id=202,
                login="pr-author",
                email="author@example.test",
            ),
        }
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_managed_github_client] = lambda: fake_github
    app.dependency_overrides[get_oauth_client] = lambda: fake_oauth
    client = TestClient(app)

    payload = pull_request_payload(head_sha="head-sha-1")
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

    reviewer_session = create_user_session(
        settings,
        github_user_id=101,
        github_login="reviewer",
        access_token="reviewer-token",
    )
    create_user_session(
        settings,
        github_user_id=202,
        github_login="pr-author",
        access_token="author-token",
    )
    client.cookies.set(SESSION_COOKIE_NAME, reviewer_session)
    workspace = client.get("/api/reviews/octo-org/notebooklens/pulls/7").json()
    row = next(
        item
        for item in workspace["snapshot"]["payload"]["review"]["notebooks"][0]["render_rows"]
        if item["outputs"]["changed"]
    )
    anchor = row["thread_anchors"]["outputs"]
    thread_response = client.post(
        f"/api/reviews/{workspace['review']['id']}/threads",
        json={
            "snapshot_id": workspace["snapshot"]["id"],
            "anchor": anchor,
            "body_markdown": "Explain the regression and update the notebook narrative.",
        },
    )
    assert thread_response.status_code == 201

    fake_email = FakeEmailClient(failing_recipients={"author@example.test"})
    delivery_result = process_notification_delivery_once(
        settings=settings,
        email_client=fake_email,
        limit=10,
    )
    assert delivery_result.processed == 1
    assert delivery_result.sent == 0
    assert delivery_result.failed == 1

    with session_scope(settings) as db_session:
        notification = db_session.scalars(select(NotificationOutbox)).one()
        assert notification.delivery_state == NotificationDeliveryState.FAILED
        assert notification.sent_at is None
        assert notification.attempt_count == 1
        assert notification.last_error == "boom while emailing author@example.test"

    engine.dispose()


def test_snapshot_worker_carries_forward_open_threads_and_marks_outdated_when_anchor_breaks(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    engine = get_engine(settings.database_url)
    create_all_tables(engine)
    app = create_app()
    fake_github = FakeManagedGitHubClient(
        files=[
            {
                "filename": REVIEW_WORKSPACE_THREAD_PATH,
                "status": "modified",
                "size": 2048,
            }
        ],
        contents={
            (REVIEW_WORKSPACE_THREAD_PATH, "base-sha"): fixture_text(
                "review_workspace_thread_base.ipynb"
            ),
            (REVIEW_WORKSPACE_THREAD_PATH, "head-sha-1"): fixture_text(
                "review_workspace_thread_head_v1.ipynb"
            ),
            (REVIEW_WORKSPACE_THREAD_PATH, "head-sha-2"): fixture_text(
                "review_workspace_thread_head_v2.ipynb"
            ),
            (
                REVIEW_WORKSPACE_THREAD_PATH,
                "head-sha-3",
            ): json.dumps(
                {
                    "cells": [
                        {
                            "cell_type": "markdown",
                            "id": "intro-cell",
                            "source": "Narrative updated for new dataset.",
                            "metadata": {},
                        },
                        {
                            "cell_type": "code",
                            "id": "metric-cell",
                            "source": "print('new accuracy')",
                            "metadata": {},
                            "outputs": [
                                {
                                    "output_type": "stream",
                                    "name": "stdout",
                                    "text": ["accuracy = 0.79\n"],
                                }
                            ],
                        },
                    ],
                    "metadata": {},
                    "nbformat": 4,
                    "nbformat_minor": 5,
                }
            ),
        },
    )
    fake_oauth = FakeOAuthClient(
        users_by_token={
            "reviewer-token": GitHubOAuthUser(
                id=101,
                login="reviewer",
                email="reviewer@example.test",
            ),
            "reviewer-2-token": GitHubOAuthUser(
                id=303,
                login="reviewer-2",
                email="reviewer2@example.test",
            ),
            "author-token": GitHubOAuthUser(
                id=202,
                login="pr-author",
                email="author@example.test",
            ),
        }
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_managed_github_client] = lambda: fake_github
    app.dependency_overrides[get_oauth_client] = lambda: fake_oauth
    client = TestClient(app)

    first_payload = pull_request_payload(head_sha="head-sha-1")
    first_body = json.dumps(first_payload).encode("utf-8")
    assert (
        client.post(
            "/api/github/webhooks",
            content=first_body,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "delivery-1",
                "X-Hub-Signature-256": sign_github_webhook("webhook-secret", first_body),
                "Content-Type": "application/json",
            },
        ).status_code
        == 202
    )

    with session_scope(settings) as db_session:
        first_result = run_snapshot_build_worker_once(
            settings=settings,
            db_session=db_session,
            github_client=fake_github,
        )
        assert first_result.status == "succeeded"

    reviewer_session = create_user_session(
        settings,
        github_user_id=101,
        github_login="reviewer",
        access_token="reviewer-token",
    )
    reviewer_two_session = create_user_session(
        settings,
        github_user_id=303,
        github_login="reviewer-2",
        access_token="reviewer-2-token",
    )
    create_user_session(
        settings,
        github_user_id=202,
        github_login="pr-author",
        access_token="author-token",
    )

    client.cookies.set(SESSION_COOKIE_NAME, reviewer_session)
    workspace = client.get("/api/reviews/octo-org/notebooklens/pulls/7").json()
    notebook = workspace["snapshot"]["payload"]["review"]["notebooks"][0]
    assert notebook["path"] == REVIEW_WORKSPACE_THREAD_PATH
    row = next(
        item
        for item in notebook["render_rows"]
        if item["locator"]["cell_id"] == "metric-cell"
    )
    assert row["outputs"]["changed"] is True
    assert row["source"]["changed"] is False
    create_response = client.post(
        f"/api/reviews/{workspace['review']['id']}/threads",
        json={
            "snapshot_id": workspace["snapshot"]["id"],
            "anchor": row["thread_anchors"]["outputs"],
            "body_markdown": "Explain the regression.",
        },
    )
    assert create_response.status_code == 201
    thread_id = create_response.json()["thread"]["id"]
    client.cookies.set(SESSION_COOKIE_NAME, reviewer_two_session)
    reply_response = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"body_markdown": "The output still needs narrative context for the regression."},
    )
    assert reply_response.status_code == 201
    assert len(reply_response.json()["thread"]["messages"]) == 2

    second_payload = pull_request_payload(action="synchronize", head_sha="head-sha-2")
    second_body = json.dumps(second_payload).encode("utf-8")
    assert (
        client.post(
            "/api/github/webhooks",
            content=second_body,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "delivery-2",
                "X-Hub-Signature-256": sign_github_webhook("webhook-secret", second_body),
                "Content-Type": "application/json",
            },
        ).status_code
        == 202
    )
    with session_scope(settings) as db_session:
        second_result = run_snapshot_build_worker_once(
            settings=settings,
            db_session=db_session,
            github_client=fake_github,
        )
        assert second_result.status == "succeeded"
        thread = db_session.scalars(select(ReviewThread)).one()
        latest_snapshot = db_session.scalars(
            select(ReviewSnapshot).where(ReviewSnapshot.snapshot_index == 2)
        ).one()
        assert thread.status == ReviewThreadStatus.OPEN
        assert thread.carried_forward is True
        assert thread.current_snapshot_id == latest_snapshot.id

    latest_workspace = client.get("/api/reviews/octo-org/notebooklens/pulls/7").json()
    assert latest_workspace["review"]["selected_snapshot_index"] == 2
    assert len(latest_workspace["threads"]) == 1
    latest_notebook = latest_workspace["snapshot"]["payload"]["review"]["notebooks"][0]
    latest_intro = next(
        item
        for item in latest_notebook["render_rows"]
        if item["locator"]["cell_id"] == "intro-cell"
    )
    assert "train_v2.csv" in latest_intro["source"]["head"]
    origin_workspace = client.get("/api/reviews/octo-org/notebooklens/pulls/7/snapshots/1").json()
    assert len(origin_workspace["threads"]) == 1
    assert len(origin_workspace["threads"][0]["messages"]) == 2

    third_payload = pull_request_payload(action="synchronize", head_sha="head-sha-3")
    third_body = json.dumps(third_payload).encode("utf-8")
    assert (
        client.post(
            "/api/github/webhooks",
            content=third_body,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "delivery-3",
                "X-Hub-Signature-256": sign_github_webhook("webhook-secret", third_body),
                "Content-Type": "application/json",
            },
        ).status_code
        == 202
    )
    with session_scope(settings) as db_session:
        third_result = run_snapshot_build_worker_once(
            settings=settings,
            db_session=db_session,
            github_client=fake_github,
        )
        assert third_result.status == "succeeded"
        thread = db_session.scalars(select(ReviewThread)).one()
        latest_snapshot = db_session.scalars(
            select(ReviewSnapshot).where(ReviewSnapshot.snapshot_index == 3)
        ).one()
        assert thread.id.hex
        assert thread.status == ReviewThreadStatus.OUTDATED
        assert thread.current_snapshot_id != latest_snapshot.id

    latest_workspace = client.get("/api/reviews/octo-org/notebooklens/pulls/7").json()
    assert latest_workspace["review"]["selected_snapshot_index"] == 3
    assert latest_workspace["threads"] == []
    origin_workspace = client.get("/api/reviews/octo-org/notebooklens/pulls/7/snapshots/1").json()
    assert origin_workspace["threads"][0]["id"] == thread_id
    assert origin_workspace["threads"][0]["status"] == "outdated"

    engine.dispose()


def test_github_mirror_worker_syncs_native_review_comments_and_updates_in_place(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    engine = get_engine(settings.database_url)
    create_all_tables(engine)
    app = create_app()
    fake_github = FakeManagedGitHubClient(
        files=[
            {
                "filename": REVIEW_WORKSPACE_THREAD_PATH,
                "status": "modified",
                "size": 2048,
            }
        ],
        contents={
            (REVIEW_WORKSPACE_THREAD_PATH, "base-sha"): fixture_text(
                "review_workspace_thread_base.ipynb"
            ),
            (REVIEW_WORKSPACE_THREAD_PATH, "head-sha-1"): fixture_text(
                "review_workspace_thread_head_v1.ipynb"
            ),
        },
    )
    fake_oauth = FakeOAuthClient(
        users_by_token={
            "reviewer-token": GitHubOAuthUser(
                id=101,
                login="reviewer",
                email="reviewer@example.test",
            ),
            "reviewer-2-token": GitHubOAuthUser(
                id=303,
                login="reviewer-2",
                email="reviewer2@example.test",
            ),
            "author-token": GitHubOAuthUser(
                id=202,
                login="pr-author",
                email="author@example.test",
            ),
        }
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_managed_github_client] = lambda: fake_github
    app.dependency_overrides[get_oauth_client] = lambda: fake_oauth
    client = TestClient(app)

    reviewer_session = create_user_session(
        settings,
        github_user_id=101,
        github_login="reviewer",
        access_token="reviewer-token",
    )
    reviewer_two_session = create_user_session(
        settings,
        github_user_id=303,
        github_login="reviewer-2",
        access_token="reviewer-2-token",
    )
    create_user_session(
        settings,
        github_user_id=202,
        github_login="pr-author",
        access_token="author-token",
    )

    response = post_pull_request_webhook(
        client,
        payload=pull_request_payload(head_sha="head-sha-1"),
        delivery_id="mirror-delivery-1",
    )
    assert response.status_code == 202
    run_managed_snapshot_worker(settings=settings, github_client=fake_github)
    initial_results = run_github_mirror_worker_until_idle(
        settings=settings,
        github_client=fake_github,
    )
    assert [result.status for result in initial_results] == ["sent", "idle"]
    assert fake_github.issue_comment_calls[0]["op"] == "create"

    client.cookies.set(SESSION_COOKIE_NAME, reviewer_session)
    workspace = client.get("/api/reviews/octo-org/notebooklens/pulls/7").json()
    metric_row = review_row_for_cell(workspace, cell_id="metric-cell")
    thread_response = client.post(
        f"/api/reviews/{workspace['review']['id']}/threads",
        json={
            "snapshot_id": workspace["snapshot"]["id"],
            "anchor": metric_row["thread_anchors"]["outputs"],
            "body_markdown": "Explain the regression and update the notebook narrative.",
        },
    )
    assert thread_response.status_code == 201
    thread_id = thread_response.json()["thread"]["id"]

    create_results = run_github_mirror_worker_until_idle(
        settings=settings,
        github_client=fake_github,
    )
    assert [result.status for result in create_results] == ["sent", "idle"]
    root_call = fake_github.review_comment_calls[0]
    assert root_call["op"] == "create_root"
    assert root_call["access_token"] == "reviewer-token"
    assert root_call["path"] == REVIEW_WORKSPACE_THREAD_PATH
    assert root_call["line"] > 0

    client.cookies.set(SESSION_COOKIE_NAME, reviewer_two_session)
    reply_response = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"body_markdown": "The output still needs narrative context for the regression."},
    )
    assert reply_response.status_code == 201
    with session_scope(settings) as db_session:
        session_record = db_session.scalars(
            select(UserSession).where(UserSession.github_user_id == 303)
        ).one()
        session_record.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)

    reply_results = run_github_mirror_worker_until_idle(
        settings=settings,
        github_client=fake_github,
    )
    assert [result.status for result in reply_results] == ["sent", "idle"]
    reply_call = fake_github.review_comment_calls[1]
    assert reply_call["op"] == "create_reply"
    assert reply_call["access_token"] is None

    client.cookies.set(SESSION_COOKIE_NAME, reviewer_session)
    assert client.post(f"/api/threads/{thread_id}/resolve").status_code == 200
    resolve_results = run_github_mirror_worker_until_idle(
        settings=settings,
        github_client=fake_github,
    )
    assert [result.status for result in resolve_results] == ["sent", "idle"]
    resolve_call = fake_github.review_comment_calls[2]
    assert resolve_call["op"] == "create_reply"
    assert resolve_call["access_token"] is None
    assert "NotebookLens resolved this thread" in resolve_call["body"]

    assert client.post(f"/api/threads/{thread_id}/reopen").status_code == 200
    reopen_results = run_github_mirror_worker_until_idle(
        settings=settings,
        github_client=fake_github,
    )
    assert [result.status for result in reopen_results] == ["sent", "idle"]
    reopen_call = fake_github.review_comment_calls[3]
    assert reopen_call["op"] == "create_reply"
    assert reopen_call["access_token"] is None
    assert "NotebookLens reopened this thread" in reopen_call["body"]

    with session_scope(settings) as db_session:
        thread = db_session.scalars(select(ReviewThread)).one()
        messages = db_session.scalars(
            select(ThreadMessage).order_by(ThreadMessage.created_at.asc())
        ).all()
        review = db_session.scalars(select(ManagedReview)).one()
        root_comment_id = thread.github_root_comment_id
        assert root_comment_id is not None
        assert thread.github_mirror_state == GitHubMirrorState.MIRRORED
        assert thread.github_mirror_metadata_json["mode"] == "app"
        assert thread.github_mirror_metadata_json["fallback_reason"] is None
        assert thread.github_mirror_metadata_json["last_action"] == "reopened"
        assert messages[1].github_reply_comment_id is not None
        assert review.github_workspace_comment_id is not None
        workspace_comment_id = review.github_workspace_comment_id
        messages[0].body_markdown = "Updated explanation from NotebookLens."
        enqueue_github_mirror_job(
            db_session=db_session,
            managed_review_id=review.id,
            thread_id=thread.id,
            thread_message_id=messages[0].id,
            action=GitHubMirrorAction.CREATE_THREAD,
        )

    update_results = run_github_mirror_worker_until_idle(
        settings=settings,
        github_client=fake_github,
    )
    assert [result.status for result in update_results] == ["sent", "idle"]
    update_call = fake_github.review_comment_calls[-1]
    assert update_call["op"] == "update"
    assert update_call["comment_id"] == root_comment_id
    assert "Updated explanation from NotebookLens." in fake_github.review_comments[root_comment_id]["body"]

    workspace_comment = fake_github.issue_comments[workspace_comment_id]
    assert "[Open in NotebookLens](https://notebooklens.test/reviews/octo-org/notebooklens/pulls/7)" in workspace_comment["body"]
    assert "### Fallback Threads" in workspace_comment["body"]
    assert "None." in workspace_comment["body"]

    engine.dispose()


def test_github_mirror_worker_uses_workspace_comment_fallback_for_unmappable_metadata_threads(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    engine = get_engine(settings.database_url)
    create_all_tables(engine)
    app = create_app()
    metadata_path = "notebooks/training/metadata_only.ipynb"
    base_notebook, head_notebook = review_metadata_thread_notebooks()
    fake_github = FakeManagedGitHubClient(
        files=[
            {
                "filename": metadata_path,
                "status": "modified",
                "size": 1024,
            }
        ],
        contents={
            (metadata_path, "base-sha"): base_notebook,
            (metadata_path, "head-sha-metadata"): head_notebook,
        },
    )
    fake_oauth = FakeOAuthClient(
        users_by_token={
            "reviewer-token": GitHubOAuthUser(
                id=101,
                login="reviewer",
                email="reviewer@example.test",
            ),
            "author-token": GitHubOAuthUser(
                id=202,
                login="pr-author",
                email="author@example.test",
            ),
        }
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_managed_github_client] = lambda: fake_github
    app.dependency_overrides[get_oauth_client] = lambda: fake_oauth
    client = TestClient(app)

    reviewer_session = create_user_session(
        settings,
        github_user_id=101,
        github_login="reviewer",
        access_token="reviewer-token",
    )
    author_session = create_user_session(
        settings,
        github_user_id=202,
        github_login="pr-author",
        access_token="author-token",
    )

    response = post_pull_request_webhook(
        client,
        payload=pull_request_payload(head_sha="head-sha-metadata"),
        delivery_id="mirror-delivery-2",
    )
    assert response.status_code == 202
    run_managed_snapshot_worker(settings=settings, github_client=fake_github)
    run_github_mirror_worker_until_idle(settings=settings, github_client=fake_github)

    client.cookies.set(SESSION_COOKIE_NAME, reviewer_session)
    workspace = client.get("/api/reviews/octo-org/notebooklens/pulls/7").json()
    row = review_row_for_cell(workspace, cell_id="metric-cell")
    assert row["metadata"]["changed"] is True
    thread_response = client.post(
        f"/api/reviews/{workspace['review']['id']}/threads",
        json={
            "snapshot_id": workspace["snapshot"]["id"],
            "anchor": row["thread_anchors"]["metadata"],
            "body_markdown": "Please explain the metadata tag change.",
        },
    )
    assert thread_response.status_code == 201
    thread_id = thread_response.json()["thread"]["id"]
    run_github_mirror_worker_until_idle(settings=settings, github_client=fake_github)

    client.cookies.set(SESSION_COOKIE_NAME, author_session)
    reply_response = client.post(
        f"/api/threads/{thread_id}/messages",
        json={"body_markdown": "I will add the reasoning in the next update."},
    )
    assert reply_response.status_code == 201
    run_github_mirror_worker_until_idle(settings=settings, github_client=fake_github)

    client.cookies.set(SESSION_COOKIE_NAME, reviewer_session)
    assert client.post(f"/api/threads/{thread_id}/resolve").status_code == 200
    run_github_mirror_worker_until_idle(settings=settings, github_client=fake_github)

    with session_scope(settings) as db_session:
        thread = db_session.scalars(select(ReviewThread)).one()
        review = db_session.scalars(select(ManagedReview)).one()
        messages = db_session.scalars(
            select(ThreadMessage).order_by(ThreadMessage.created_at.asc())
        ).all()
        assert thread.github_mirror_state == GitHubMirrorState.SKIPPED
        assert thread.github_root_comment_id is None
        assert thread.github_mirror_metadata_json["fallback_reason"] == "unmappable_anchor"
        assert thread.status == ReviewThreadStatus.RESOLVED
        assert all(message.github_reply_comment_id is None for message in messages)
        workspace_comment_id = review.github_workspace_comment_id

    assert fake_github.review_comment_calls == []
    workspace_comment = fake_github.issue_comments[workspace_comment_id]
    assert metadata_path in workspace_comment["body"]
    assert "metadata" in workspace_comment["body"]
    assert "Please explain the metadata tag change." in workspace_comment["body"]
    assert "I will add the reasoning in the next update." in workspace_comment["body"]
    assert "resolved" in workspace_comment["body"]

    engine.dispose()


def test_sales_forecast_managed_workspace_scenario_covers_compose_assets_gateway_and_github_sync(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    engine = get_engine(settings.database_url)
    create_all_tables(engine)
    installation_id = create_github_installation_fixture(settings)
    base_notebook, head_notebook = sales_forecast_notebooks()
    assert "sales_q1.csv" in base_notebook
    assert "mae = 12.4" in base_notebook
    assert "sales_q2.csv" in head_notebook
    assert "mae = 18.7" in head_notebook
    fake_github = FakeManagedGitHubClient(
        files=[
            {
                "filename": SALES_FORECAST_NOTEBOOK_PATH,
                "status": "modified",
                "size": 4096,
            }
        ],
        contents={
            (SALES_FORECAST_NOTEBOOK_PATH, "sales-base-sha"): base_notebook,
            (SALES_FORECAST_NOTEBOOK_PATH, "sales-head-sha"): head_notebook,
        },
    )
    fake_oauth = FakeOAuthClient(
        users_by_token={
            "gho_owner": GitHubOAuthUser(
                id=101,
                login="octo-owner",
                email="owner@example.test",
            ),
            "gho_reviewer_1": GitHubOAuthUser(
                id=202,
                login="analyst-1",
                email="analyst-1@example.test",
            ),
            "gho_reviewer_2": GitHubOAuthUser(
                id=303,
                login="analyst-2",
                email="analyst-2@example.test",
            ),
        },
        org_owner_access={("gho_owner", "octo-org"): True},
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_managed_github_client] = lambda: fake_github
    app.dependency_overrides[get_oauth_client] = lambda: fake_oauth
    client = TestClient(app)

    owner_session = create_user_session(
        settings,
        github_user_id=101,
        github_login="octo-owner",
        access_token="gho_owner",
    )
    reviewer_session = create_user_session(
        settings,
        github_user_id=202,
        github_login="analyst-1",
        access_token="gho_reviewer_1",
    )
    reviewer_two_session = create_user_session(
        settings,
        github_user_id=303,
        github_login="analyst-2",
        access_token="gho_reviewer_2",
    )

    assert_compose_smoke_stack_supports_managed_review_flow()

    client.cookies.set(SESSION_COOKIE_NAME, owner_session)
    ai_gateway_response = client.put(
        f"/api/settings/ai-gateway?installation_id={installation_id}",
        json={
            "provider_kind": "litellm",
            "display_name": "Internal LiteLLM",
            "github_host_kind": "github_com",
            "github_api_base_url": "https://api.github.com",
            "github_web_base_url": "https://github.com",
            "base_url": "https://litellm.internal.example/v1",
            "model_name": "gpt-4.1",
            "api_key": "Bearer managed-secret",
            "api_key_header_name": "Authorization",
            "static_headers": {"x-tenant-token": "tenant-secret"},
            "use_responses_api": False,
            "litellm_virtual_key_id": "vk-sales",
            "active": True,
        },
    )
    assert ai_gateway_response.status_code == 200
    assert ai_gateway_response.json()["config"]["provider_kind"] == "litellm"
    assert ai_gateway_response.json()["config"]["active"] is True

    webhook_response = post_pull_request_webhook(
        client,
        payload=pull_request_payload(
            base_sha="sales-base-sha",
            head_sha="sales-head-sha",
        ),
        delivery_id="sales-forecast-build-1",
    )
    assert webhook_response.status_code == 202

    success_gateway = FakeLiteLLMGatewayClient(
        responses=[
            json.dumps(
                {
                    "summary": "Managed AI summary for the updated weekly sales forecast.",
                    "flagged_issues": [
                        {
                            "notebook_path": SALES_FORECAST_NOTEBOOK_PATH,
                            "locator": {
                                "cell_id": None,
                                "base_index": None,
                                "head_index": None,
                                "display_index": None,
                            },
                            "code": "ai:forecast_shift",
                            "category": "output",
                            "severity": "medium",
                            "confidence": "medium",
                            "message": "Explain why the forecast curve shifted downward.",
                        }
                    ],
                    "reviewer_guidance": [
                        {
                            "notebook_path": SALES_FORECAST_NOTEBOOK_PATH,
                            "locator": None,
                            "code": "claude:forecast_context",
                            "source": "claude",
                            "label": "AI",
                            "priority": "medium",
                            "message": "Call out the sales_q2.csv input swap and the wider forecast window.",
                        }
                    ],
                }
            )
        ]
    )
    with session_scope(settings) as db_session:
        first_result = run_snapshot_build_worker_once(
            settings=settings,
            db_session=db_session,
            github_client=fake_github,
            litellm_client=success_gateway,
        )
        assert first_result.status == "succeeded"
        assert first_result.snapshot_index == 1
    assert len(success_gateway.calls) == 1
    assert success_gateway.calls[0]["api_key"] == "Bearer managed-secret"
    assert SALES_FORECAST_NOTEBOOK_PATH in success_gateway.calls[0]["prompt"]

    initial_mirror_results = run_github_mirror_worker_until_idle(
        settings=settings,
        github_client=fake_github,
    )
    assert [result.status for result in initial_mirror_results] == ["sent", "idle"]

    client.cookies.set(SESSION_COOKIE_NAME, owner_session)
    review_page_response = client.get("/api/reviews/octo-org/notebooklens/pulls/7")
    assert review_page_response.status_code == 200
    review_page = review_page_response.json()
    assert review_page["review"]["selected_snapshot_index"] == 1
    assert review_page["snapshot"]["payload"]["review"]["notebooks"][0]["path"] == SALES_FORECAST_NOTEBOOK_PATH
    plot_row = review_row_for_cell(review_page, cell_id="forecast-plot")
    metric_row = review_row_for_cell(review_page, cell_id="forecast-metrics")
    assert plot_row["source"]["changed"] is True
    assert plot_row["outputs"]["changed"] is True
    assert plot_row["metadata"]["changed"] is True
    assert plot_row["outputs"]["items"][0]["kind"] == "image"
    assert plot_row["outputs"]["items"][0]["change_type"] == "modified"
    assert metric_row["outputs"]["changed"] is True
    asset_id = plot_row["outputs"]["items"][0]["asset_id"]

    client.cookies.set(SESSION_COOKIE_NAME, reviewer_session)
    asset_response = client.get(f"/api/review-assets/{asset_id}")
    assert asset_response.status_code == 200
    assert asset_response.headers["content-type"] == "image/png"
    assert asset_response.content
    assert ("gho_reviewer_1", "octo-org", "notebooklens") in fake_oauth.repo_access_checks

    client.cookies.set(SESSION_COOKIE_NAME, owner_session)
    rebuild_response = client.post(f"/api/reviews/{review_page['review']['id']}/rebuild-latest")
    assert rebuild_response.status_code == 202
    assert rebuild_response.json()["force_rebuild"] is True

    failing_gateway = FakeLiteLLMGatewayClient(error="gateway exploded")
    with session_scope(settings) as db_session:
        second_result = run_snapshot_build_worker_once(
            settings=settings,
            db_session=db_session,
            github_client=fake_github,
            litellm_client=failing_gateway,
        )
        assert second_result.status == "succeeded"
        assert second_result.snapshot_index == 2
    assert len(failing_gateway.calls) == 2

    rebuild_mirror_results = run_github_mirror_worker_until_idle(
        settings=settings,
        github_client=fake_github,
    )
    assert [result.status for result in rebuild_mirror_results] == ["sent", "idle"]

    with session_scope(settings) as db_session:
        snapshots = db_session.scalars(
            select(ReviewSnapshot).order_by(ReviewSnapshot.snapshot_index.asc())
        ).all()
        assert [snapshot.snapshot_index for snapshot in snapshots] == [1, 2]
        assert snapshots[0].summary_text == "Managed AI summary for the updated weekly sales forecast."
        assert snapshots[1].summary_text is not None
        assert "Managed LiteLLM review unavailable: gateway exploded." in snapshots[1].summary_text
        assert (
            "Managed LiteLLM review unavailable: gateway exploded. Used deterministic local findings."
            in snapshots[1].snapshot_payload_json["review"]["notices"]
        )

    client.cookies.set(SESSION_COOKIE_NAME, reviewer_session)
    latest_workspace_response = client.get("/api/reviews/octo-org/notebooklens/pulls/7")
    assert latest_workspace_response.status_code == 200
    latest_workspace = latest_workspace_response.json()
    assert latest_workspace["review"]["selected_snapshot_index"] == 2
    assert latest_workspace["snapshot"]["summary_text"] is not None
    assert "Managed LiteLLM review unavailable: gateway exploded." in latest_workspace["snapshot"][
        "summary_text"
    ]

    latest_plot_row = review_row_for_cell(latest_workspace, cell_id="forecast-plot")
    create_plot_thread_response = client.post(
        f"/api/reviews/{latest_workspace['review']['id']}/threads",
        json={
            "snapshot_id": latest_workspace["snapshot"]["id"],
            "anchor": latest_plot_row["thread_anchors"]["outputs"],
            "body_markdown": "Explain why the forecast curve shifted downward.",
        },
    )
    assert create_plot_thread_response.status_code == 201
    plot_thread_id = create_plot_thread_response.json()["thread"]["id"]

    client.cookies.set(SESSION_COOKIE_NAME, reviewer_two_session)
    reply_response = client.post(
        f"/api/threads/{plot_thread_id}/messages",
        json={
            "body_markdown": "The Q2 data and wider smoothing window both lowered the projected curve.",
        },
    )
    assert reply_response.status_code == 201

    client.cookies.set(SESSION_COOKIE_NAME, reviewer_session)
    fallback_thread_response = client.post(
        f"/api/reviews/{latest_workspace['review']['id']}/threads",
        json={
            "snapshot_id": latest_workspace["snapshot"]["id"],
            "anchor": latest_plot_row["thread_anchors"]["metadata"],
            "body_markdown": "Please explain why the forecast-review metadata tag was added.",
        },
    )
    assert fallback_thread_response.status_code == 201

    thread_mirror_results = run_github_mirror_worker_until_idle(
        settings=settings,
        github_client=fake_github,
    )
    assert [result.status for result in thread_mirror_results] == ["sent", "sent", "sent", "idle"]

    assert len(fake_github.review_comment_calls) == 2
    root_call = fake_github.review_comment_calls[0]
    assert root_call["op"] == "create_root"
    assert root_call["access_token"] == "gho_reviewer_1"
    assert root_call["path"] == SALES_FORECAST_NOTEBOOK_PATH
    assert root_call["line"] > 0
    assert "Explain why the forecast curve shifted downward." in root_call["body"]

    reply_call = fake_github.review_comment_calls[1]
    assert reply_call["op"] == "create_reply"
    assert reply_call["access_token"] == "gho_reviewer_2"
    assert "The Q2 data and wider smoothing window both lowered the projected curve." in reply_call[
        "body"
    ]

    final_workspace_response = client.get("/api/reviews/octo-org/notebooklens/pulls/7")
    assert final_workspace_response.status_code == 200
    final_workspace = final_workspace_response.json()
    threads_by_body = {
        thread["messages"][0]["body_markdown"]: thread
        for thread in final_workspace["threads"]
    }

    mirrored_thread = threads_by_body["Explain why the forecast curve shifted downward."]
    assert mirrored_thread["messages"][1]["body_markdown"] == (
        "The Q2 data and wider smoothing window both lowered the projected curve."
    )
    assert mirrored_thread["github_mirror"]["state"] == "mirrored"
    assert mirrored_thread["github_mirror"]["root_comment_url"] is not None
    assert mirrored_thread["github_mirror"]["target"] == "github_review_comment"

    fallback_thread = threads_by_body[
        "Please explain why the forecast-review metadata tag was added."
    ]
    assert fallback_thread["github_mirror"]["state"] == "skipped"
    assert fallback_thread["github_mirror"]["fallback_reason"] == "unmappable_anchor"
    assert fallback_thread["github_mirror"]["target"] == "workspace_fallback"

    with session_scope(settings) as db_session:
        review = db_session.scalars(select(ManagedReview)).one()
        assert review.github_workspace_comment_id is not None
        workspace_comment = fake_github.issue_comments[review.github_workspace_comment_id]

    assert "[Open in NotebookLens](https://notebooklens.test/reviews/octo-org/notebooklens/pulls/7)" in (
        workspace_comment["body"]
    )
    assert SALES_FORECAST_NOTEBOOK_PATH in workspace_comment["body"]
    assert "### Fallback Threads" in workspace_comment["body"]
    assert "metadata" in workspace_comment["body"]
    assert "Please explain why the forecast-review metadata tag was added." in workspace_comment[
        "body"
    ]

    engine.dispose()
