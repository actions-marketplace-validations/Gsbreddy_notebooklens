"""Managed snapshot orchestration for PR webhook ingestion and worker execution."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence
import json
import uuid

import requests
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from src.claude_integration import (
    DEFAULT_MAX_AI_INPUT_TOKENS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    NoneProvider,
    build_base_reviewer_guidance,
    parse_strict_review_result,
    _build_repair_prompt,
    _prepare_ai_payload,
)
from src.diff_engine import DiffLimits, NotebookInput
from src.review_core import (
    REVIEW_SNAPSHOT_SCHEMA_VERSION,
    ReviewAssetDraft,
    ReviewCoreRequest,
    build_review_artifacts,
)

from .check_runs import (
    build_review_url,
    render_review_workspace_check_run_summary,
    sync_review_workspace_check_run,
)
from .config import ApiSettings
from .job_runner import (
    claim_next_snapshot_build_job,
    enqueue_snapshot_build_job,
    mark_snapshot_build_job_failed,
    mark_snapshot_build_job_succeeded,
)
from .managed_github import MANAGED_REVIEW_CHECK_RUN_NAME, ManagedGitHubClient
from .models import (
    GitHubInstallation,
    InstallationAccountType,
    InstallationRepository,
    ManagedAiGatewayConfig,
    ManagedAiGatewayProviderKind,
    ManagedReview,
    ManagedReviewStatus,
    ReviewAsset,
    ReviewSnapshot,
    ReviewSnapshotStatus,
    SnapshotBuildJob,
    SnapshotBuildJobStatus,
)
from .oauth import SessionCipherError, SessionTokenCipher
from .review_workspace import carry_forward_open_threads, count_review_threads
from .reviewer_guidance import (
    NotebookLensConfigError,
    ReviewerPlaybook,
    build_reviewer_guidance,
    parse_reviewer_playbooks,
)


SUPPORTED_PULL_REQUEST_ACTIONS = {"opened", "reopened", "synchronize"}
_CONFIG_PATH = ".github/notebooklens.yml"


class ManagedWebhookPayloadError(ValueError):
    """Raised when a GitHub webhook payload is missing required PR fields."""


@dataclass(frozen=True)
class PullRequestWebhook:
    """Normalized PR webhook payload used for managed review orchestration."""

    action: str
    installation_id: int
    account_login: str
    account_type: InstallationAccountType
    owner: str
    repo: str
    full_name: str
    private: bool
    pull_number: int
    pull_author_github_user_id: int | None
    pull_author_login: str | None
    base_branch: str
    base_sha: str
    head_sha: str


@dataclass(frozen=True)
class WebhookIngestionResult:
    """Structured webhook-ingestion result for route responses and tests."""

    accepted: bool
    reason: str
    action: str | None
    managed_review_id: uuid.UUID | None
    job_id: uuid.UUID | None
    check_run_id: int | None


@dataclass(frozen=True)
class SnapshotBuildResult:
    """Structured worker result for one claimed snapshot build job."""

    status: str
    job_id: uuid.UUID | None
    managed_review_id: uuid.UUID | None
    snapshot_id: uuid.UUID | None
    snapshot_index: int | None
    check_run_id: int | None
    reused_snapshot: bool = False


@dataclass(frozen=True)
class LiteLLMGatewayResponse:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class ManagedReviewProviderState:
    gateway_enabled: bool
    used_fallback: bool = False
    fallback_notice: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class LiteLLMGatewayError(RuntimeError):
    """Raised when the managed LiteLLM gateway request or payload is invalid."""


class LiteLLMGatewayClient:
    """Minimal LiteLLM-compatible client used by the managed snapshot worker."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()

    def complete(
        self,
        *,
        config: ManagedAiGatewayConfig,
        api_key: str,
        static_headers: Mapping[str, str],
        prompt: str,
    ) -> LiteLLMGatewayResponse:
        headers = {
            "Accept": "application/json",
            config.api_key_header_name: api_key,
        }
        headers.update({str(key): str(value) for key, value in static_headers.items()})

        if config.use_responses_api:
            path = "/responses"
            payload = {
                "model": config.model_name,
                "input": prompt,
                "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
            }
        else:
            path = "/chat/completions"
            payload = {
                "model": config.model_name,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
            }

        try:
            response = self.session.post(
                f"{config.base_url}{path}",
                headers=headers,
                json=payload,
                timeout=30,
            )
        except requests.RequestException as exc:
            raise LiteLLMGatewayError(f"network error: {exc}") from exc

        if response.status_code >= 400:
            detail = response.text.strip() or f"status {response.status_code}"
            raise LiteLLMGatewayError(
                f"HTTP {response.status_code}: {detail[:300]}"
            )

        try:
            payload_json = response.json()
        except ValueError as exc:
            raise LiteLLMGatewayError("gateway response was not JSON") from exc

        if not isinstance(payload_json, Mapping):
            raise LiteLLMGatewayError("gateway response JSON must be an object")

        return LiteLLMGatewayResponse(
            text=_extract_litellm_text(payload_json, use_responses_api=config.use_responses_api),
            input_tokens=_extract_usage_value(
                payload_json.get("usage"),
                keys=("input_tokens", "prompt_tokens"),
            ),
            output_tokens=_extract_usage_value(
                payload_json.get("usage"),
                keys=("output_tokens", "completion_tokens"),
            ),
        )


class ManagedLiteLLMReviewer:
    """LiteLLM-backed managed reviewer with deterministic fallback on gateway failure."""

    def __init__(
        self,
        *,
        config: ManagedAiGatewayConfig,
        api_key: str,
        static_headers: Mapping[str, str],
        reviewer_playbooks: Sequence[ReviewerPlaybook],
        gateway_client: LiteLLMGatewayClient,
    ) -> None:
        self.config = config
        self.api_key = api_key
        self.static_headers = dict(static_headers)
        self.reviewer_playbooks = tuple(reviewer_playbooks)
        self.gateway_client = gateway_client
        self.last_run_state = ManagedReviewProviderState(gateway_enabled=True)

    def review(self, diff):
        base_guidance = build_base_reviewer_guidance(
            diff,
            reviewer_playbooks=self.reviewer_playbooks,
        )
        payload = _prepare_ai_payload(
            diff=diff,
            base_reviewer_guidance=base_guidance,
            redact_secrets=True,
            redact_emails=True,
            max_ai_input_tokens=DEFAULT_MAX_AI_INPUT_TOKENS,
        )
        prompt = _build_managed_gateway_prompt(payload)
        total_input_tokens = 0
        total_output_tokens = 0
        raw_response: str | None = None

        try:
            gateway_response = self.gateway_client.complete(
                config=self.config,
                api_key=self.api_key,
                static_headers=self.static_headers,
                prompt=prompt,
            )
            raw_response = gateway_response.text
            total_input_tokens += gateway_response.input_tokens or 0
            total_output_tokens += gateway_response.output_tokens or 0
            parsed = parse_strict_review_result(raw_response, diff)
            self.last_run_state = ManagedReviewProviderState(
                gateway_enabled=True,
                used_fallback=False,
                fallback_notice=None,
                input_tokens=total_input_tokens or None,
                output_tokens=total_output_tokens or None,
            )
            return parsed
        except Exception as exc:
            repair_prompt = _build_repair_prompt(raw_response or "", str(exc))
            try:
                gateway_response = self.gateway_client.complete(
                    config=self.config,
                    api_key=self.api_key,
                    static_headers=self.static_headers,
                    prompt=repair_prompt,
                )
                total_input_tokens += gateway_response.input_tokens or 0
                total_output_tokens += gateway_response.output_tokens or 0
                parsed = parse_strict_review_result(gateway_response.text, diff)
                self.last_run_state = ManagedReviewProviderState(
                    gateway_enabled=True,
                    used_fallback=False,
                    fallback_notice=None,
                    input_tokens=total_input_tokens or None,
                    output_tokens=total_output_tokens or None,
                )
                return parsed
            except Exception as repair_exc:
                return self._fallback(
                    diff,
                    reason=_truncate_provider_error(repair_exc),
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                )

    def _fallback(
        self,
        diff,
        *,
        reason: str,
        input_tokens: int,
        output_tokens: int,
    ):
        base = NoneProvider().review(diff)
        notice = (
            f"Managed LiteLLM review unavailable: {reason}. "
            "Used deterministic local findings."
        )
        self.last_run_state = ManagedReviewProviderState(
            gateway_enabled=True,
            used_fallback=True,
            fallback_notice=notice,
            input_tokens=input_tokens or None,
            output_tokens=output_tokens or None,
        )
        return type(base)(
            summary=notice if base.summary is None else f"{notice}\n\n{base.summary}",
            flagged_issues=base.flagged_issues,
            reviewer_guidance=base.reviewer_guidance,
        )


def ingest_pull_request_webhook(
    *,
    db_session: Session,
    settings: ApiSettings,
    github_client: ManagedGitHubClient,
    github_event: str | None,
    payload: Mapping[str, Any],
) -> WebhookIngestionResult:
    """Ingest a supported PR webhook into managed review + snapshot job state."""
    webhook = parse_pull_request_webhook(github_event=github_event, payload=payload)
    if webhook is None:
        return WebhookIngestionResult(
            accepted=False,
            reason="Ignored non-managed GitHub event",
            action=None,
            managed_review_id=None,
            job_id=None,
            check_run_id=None,
        )

    installation, repository, review, reusable_check_run_id = _upsert_review_state(
        db_session=db_session,
        webhook=webhook,
    )

    current_ready_snapshot = db_session.execute(
        select(ReviewSnapshot)
        .where(
            ReviewSnapshot.managed_review_id == review.id,
            ReviewSnapshot.base_sha == webhook.base_sha,
            ReviewSnapshot.head_sha == webhook.head_sha,
            ReviewSnapshot.status == ReviewSnapshotStatus.READY,
        )
        .order_by(ReviewSnapshot.snapshot_index.desc())
        .limit(1)
    ).scalar_one_or_none()

    details_url = build_review_url(
        settings=settings,
        review=review,
        snapshot_index=current_ready_snapshot.snapshot_index if current_ready_snapshot else None,
    )
    thread_counts = count_review_threads(db_session=db_session, managed_review_id=review.id)

    job = _find_active_snapshot_job(
        db_session=db_session,
        managed_review_id=review.id,
        base_sha=webhook.base_sha,
        head_sha=webhook.head_sha,
    )

    if current_ready_snapshot is not None and job is None:
        review.status = ManagedReviewStatus.READY
        review.latest_snapshot_id = current_ready_snapshot.id
        check_run = github_client.create_or_update_check_run(
            settings=settings,
            installation_id=installation.github_installation_id,
            repository=repository.full_name,
            head_sha=webhook.head_sha,
            status="completed",
            conclusion="neutral",
            details_url=details_url,
            external_id=str(review.id),
            summary=render_review_workspace_check_run_summary(
                review_url=details_url,
                snapshot_status="ready",
                activity=(
                    f"Existing snapshot v{current_ready_snapshot.snapshot_index} already matches "
                    "the latest push."
                ),
                unresolved_threads=thread_counts.unresolved,
                resolved_threads=thread_counts.resolved,
                outdated_threads=thread_counts.outdated,
            ),
            check_run_id=reusable_check_run_id,
        )
        review.latest_check_run_id = check_run.check_run_id
        db_session.flush()
        return WebhookIngestionResult(
            accepted=True,
            reason="Reused existing ready snapshot for the latest push",
            action=webhook.action,
            managed_review_id=review.id,
            job_id=None,
            check_run_id=check_run.check_run_id,
        )

    review.status = ManagedReviewStatus.PENDING

    if job is None:
        job = enqueue_snapshot_build_job(
            db_session,
            managed_review_id=review.id,
            base_sha=webhook.base_sha,
            head_sha=webhook.head_sha,
        )

    review.latest_check_run_id = reusable_check_run_id
    check_run_id = sync_review_workspace_check_run(
        settings=settings,
        db_session=db_session,
        github_client=github_client,
        review=review,
        activity="Snapshot queued for the latest push.",
    )

    db_session.flush()
    return WebhookIngestionResult(
        accepted=True,
        reason="Queued managed snapshot build",
        action=webhook.action,
        managed_review_id=review.id,
        job_id=job.id,
        check_run_id=check_run_id,
    )


def run_snapshot_build_worker_once(
    *,
    settings: ApiSettings,
    db_session: Session,
    github_client: ManagedGitHubClient,
    litellm_client: LiteLLMGatewayClient | None = None,
    limits: DiffLimits = DiffLimits(),
    now: datetime | None = None,
) -> SnapshotBuildResult:
    """Claim and process a single managed snapshot build job."""
    job = claim_next_snapshot_build_job(db_session, now=now)
    if job is None:
        return SnapshotBuildResult(
            status="idle",
            job_id=None,
            managed_review_id=None,
            snapshot_id=None,
            snapshot_index=None,
            check_run_id=None,
        )

    review = db_session.execute(
        select(ManagedReview)
        .options(
            selectinload(ManagedReview.installation_repository).selectinload(
                InstallationRepository.installation
            )
        )
        .where(ManagedReview.id == job.managed_review_id)
    ).scalar_one()
    repository = review.installation_repository
    installation = repository.installation

    existing_ready_snapshot = db_session.execute(
        select(ReviewSnapshot)
        .where(
            ReviewSnapshot.managed_review_id == review.id,
            ReviewSnapshot.base_sha == job.base_sha,
            ReviewSnapshot.head_sha == job.head_sha,
            ReviewSnapshot.status == ReviewSnapshotStatus.READY,
        )
        .order_by(ReviewSnapshot.snapshot_index.desc())
        .limit(1)
    ).scalar_one_or_none()
    if existing_ready_snapshot is not None and not job.force_rebuild:
        if _job_matches_latest_review(review=review, job=job):
            review.status = ManagedReviewStatus.READY
            review.latest_snapshot_id = existing_ready_snapshot.id
            check_run_id = sync_review_workspace_check_run(
                settings=settings,
                db_session=db_session,
                github_client=github_client,
                review=review,
                activity=(
                    f"Existing snapshot v{existing_ready_snapshot.snapshot_index} already matches "
                    f"head `{_short_sha(job.head_sha)}`."
                ),
            )
        else:
            check_run_id = review.latest_check_run_id
        mark_snapshot_build_job_succeeded(db_session, job)
        db_session.flush()
        return SnapshotBuildResult(
            status="reused",
            job_id=job.id,
            managed_review_id=review.id,
            snapshot_id=existing_ready_snapshot.id,
            snapshot_index=existing_ready_snapshot.snapshot_index,
            check_run_id=check_run_id,
            reused_snapshot=True,
        )

    if _job_matches_latest_review(review=review, job=job):
        sync_review_workspace_check_run(
            settings=settings,
            db_session=db_session,
            github_client=github_client,
            review=review,
            activity=f"Snapshot build started for head `{_short_sha(job.head_sha)}`.",
        )

    snapshot = _create_pending_snapshot(db_session=db_session, review=review, job=job)
    try:
        notebook_inputs = _build_notebook_inputs_for_review(
            settings=settings,
            github_client=github_client,
            installation_id=installation.github_installation_id,
            repository_full_name=repository.full_name,
            pull_number=review.pull_number,
            base_sha=job.base_sha,
            head_sha=job.head_sha,
            limits=limits,
        )
        config_text = github_client.get_file_content(
            settings=settings,
            installation_id=installation.github_installation_id,
            repository=repository.full_name,
            path=_CONFIG_PATH,
            ref=job.head_sha,
        )
        playbooks, guidance_notices = _load_reviewer_playbooks(config_text)
        reviewer, provider_state, provider_notices = _resolve_managed_reviewer(
            db_session=db_session,
            settings=settings,
            installation=installation,
            playbooks=playbooks,
            gateway_client=litellm_client or LiteLLMGatewayClient(),
        )
        review_artifacts = build_review_artifacts(
            ReviewCoreRequest(
                notebook_inputs=notebook_inputs,
                reviewer=reviewer,
                limits=limits,
            )
        )
        provider_state = getattr(reviewer, "last_run_state", provider_state)
        snapshot_payload = review_artifacts.snapshot_payload
        asset_ids_by_key = _persist_review_assets(
            db_session=db_session,
            snapshot=snapshot,
            review_assets=review_artifacts.review_assets,
        )
        snapshot_payload = _rewrite_snapshot_payload_asset_refs(
            snapshot_payload=snapshot_payload,
            asset_ids_by_key=asset_ids_by_key,
        )
        if guidance_notices or provider_notices:
            snapshot_payload = {
                **snapshot_payload,
                "review": {
                    **snapshot_payload["review"],
                    "notices": _merge_review_notices(
                        snapshot_payload["review"].get("notices", []),
                        guidance_notices,
                        provider_notices,
                    ),
                },
            }
        reviewer_guidance = _merge_snapshot_reviewer_guidance(
            build_reviewer_guidance(
                review_artifacts.notebook_diff,
                playbooks=playbooks,
            ),
            review_artifacts.review_result.reviewer_guidance,
        )
        if provider_state.used_fallback and provider_state.fallback_notice:
            snapshot_payload = {
                **snapshot_payload,
                "review": {
                    **snapshot_payload["review"],
                    "notices": _merge_review_notices(
                        snapshot_payload["review"].get("notices", []),
                        [provider_state.fallback_notice],
                    ),
                },
            }
        snapshot.status = ReviewSnapshotStatus.READY
        snapshot.schema_version = int(snapshot_payload["schema_version"])
        snapshot.summary_text = review_artifacts.review_result.summary
        snapshot.flagged_findings_json = [
            asdict(issue) for issue in review_artifacts.review_result.flagged_issues
        ]
        snapshot.reviewer_guidance_json = reviewer_guidance
        snapshot.snapshot_payload_json = snapshot_payload
        snapshot.notebook_count = review_artifacts.notebook_diff.total_notebooks_changed
        snapshot.changed_cell_count = review_artifacts.notebook_diff.total_cells_changed
        snapshot.failure_reason = None

        if _job_matches_latest_review(review=review, job=job):
            carry_forward_open_threads(
                db_session=db_session,
                review=review,
                snapshot=snapshot,
            )
            review.status = ManagedReviewStatus.READY
            review.latest_snapshot_id = snapshot.id
            review.latest_check_run_id = sync_review_workspace_check_run(
                settings=settings,
                db_session=db_session,
                github_client=github_client,
                review=review,
                activity=(
                    f"Snapshot v{snapshot.snapshot_index} built for head "
                    f"`{_short_sha(job.head_sha)}`."
                ),
            )
        mark_snapshot_build_job_succeeded(db_session, job)
        db_session.flush()
        return SnapshotBuildResult(
            status="succeeded",
            job_id=job.id,
            managed_review_id=review.id,
            snapshot_id=snapshot.id,
            snapshot_index=snapshot.snapshot_index,
            check_run_id=review.latest_check_run_id,
        )
    except Exception as exc:
        failure_reason = _truncate_error(exc)
        snapshot.status = ReviewSnapshotStatus.FAILED
        snapshot.failure_reason = failure_reason
        snapshot.summary_text = None
        snapshot.flagged_findings_json = []
        snapshot.reviewer_guidance_json = []
        snapshot.snapshot_payload_json = {
            "schema_version": REVIEW_SNAPSHOT_SCHEMA_VERSION,
            "review": {
                "notices": [],
                "notebooks": [],
            },
        }
        snapshot.notebook_count = 0
        snapshot.changed_cell_count = 0
        if _job_matches_latest_review(review=review, job=job):
            review.status = ManagedReviewStatus.FAILED
            review.latest_check_run_id = sync_review_workspace_check_run(
                settings=settings,
                db_session=db_session,
                github_client=github_client,
                review=review,
                activity=f"Snapshot build failed for head `{_short_sha(job.head_sha)}`.",
            )
        mark_snapshot_build_job_failed(
            db_session,
            job,
            error_message=failure_reason,
        )
        db_session.flush()
        return SnapshotBuildResult(
            status="failed",
            job_id=job.id,
            managed_review_id=review.id,
            snapshot_id=snapshot.id,
            snapshot_index=snapshot.snapshot_index,
            check_run_id=review.latest_check_run_id,
        )


def parse_pull_request_webhook(
    *,
    github_event: str | None,
    payload: Mapping[str, Any],
) -> PullRequestWebhook | None:
    """Parse a supported GitHub pull_request webhook payload, or ignore it."""
    event_name = (github_event or "").strip()
    action = str(payload.get("action", "")).strip()
    if event_name != "pull_request" or action not in SUPPORTED_PULL_REQUEST_ACTIONS:
        return None

    installation = _require_mapping(payload, "installation")
    repository = _require_mapping(payload, "repository")
    pull_request = _require_mapping(payload, "pull_request")
    base = _require_mapping(pull_request, "base")
    head = _require_mapping(pull_request, "head")

    installation_id = _require_int(installation, "id")
    account = installation.get("account") or repository.get("owner")
    account_login = _require_str(account, "login")
    account_type_raw = _require_str(account, "type").lower()
    if account_type_raw == "organization":
        account_type = InstallationAccountType.ORGANIZATION
    elif account_type_raw == "user":
        account_type = InstallationAccountType.USER
    else:
        raise ManagedWebhookPayloadError(f"Unsupported GitHub installation account type: {account_type_raw}")

    owner = _require_str(repository.get("owner"), "login")
    repo = _require_str(repository, "name")
    full_name = _require_str(repository, "full_name")

    return PullRequestWebhook(
        action=action,
        installation_id=installation_id,
        account_login=account_login,
        account_type=account_type,
        owner=owner,
        repo=repo,
        full_name=full_name,
        private=bool(repository.get("private", False)),
        pull_number=_require_int(payload, "number"),
        pull_author_github_user_id=_optional_int(pull_request.get("user"), "id"),
        pull_author_login=_optional_str(pull_request.get("user"), "login"),
        base_branch=_require_str(base, "ref"),
        base_sha=_require_str(base, "sha"),
        head_sha=_require_str(head, "sha"),
    )


def _upsert_review_state(
    *,
    db_session: Session,
    webhook: PullRequestWebhook,
) -> tuple[GitHubInstallation, InstallationRepository, ManagedReview, int | None]:
    installation = db_session.execute(
        select(GitHubInstallation).where(
            GitHubInstallation.github_installation_id == webhook.installation_id
        )
    ).scalar_one_or_none()
    if installation is None:
        installation = GitHubInstallation(
            github_installation_id=webhook.installation_id,
            account_login=webhook.account_login,
            account_type=webhook.account_type,
        )
        db_session.add(installation)
        db_session.flush()
    else:
        installation.account_login = webhook.account_login
        installation.account_type = webhook.account_type

    repository = db_session.execute(
        select(InstallationRepository).where(
            InstallationRepository.installation_id == installation.id,
            InstallationRepository.full_name == webhook.full_name,
        )
    ).scalar_one_or_none()
    if repository is None:
        repository = InstallationRepository(
            installation_id=installation.id,
            owner=webhook.owner,
            name=webhook.repo,
            full_name=webhook.full_name,
            private=webhook.private,
            active=True,
        )
        db_session.add(repository)
        db_session.flush()
    else:
        repository.owner = webhook.owner
        repository.name = webhook.repo
        repository.private = webhook.private
        repository.active = True

    review = db_session.execute(
        select(ManagedReview).where(
            ManagedReview.installation_repository_id == repository.id,
            ManagedReview.pull_number == webhook.pull_number,
        )
    ).scalar_one_or_none()
    reusable_check_run_id: int | None = None
    if review is None:
        review = ManagedReview(
            installation_repository_id=repository.id,
            owner=webhook.owner,
            repo=webhook.repo,
            pull_number=webhook.pull_number,
            pull_author_github_user_id=webhook.pull_author_github_user_id,
            pull_author_login=webhook.pull_author_login,
            base_branch=webhook.base_branch,
            latest_base_sha=webhook.base_sha,
            latest_head_sha=webhook.head_sha,
            status=ManagedReviewStatus.PENDING,
        )
        db_session.add(review)
        db_session.flush()
    else:
        reusable_check_run_id = (
            review.latest_check_run_id if review.latest_head_sha == webhook.head_sha else None
        )
        review.owner = webhook.owner
        review.repo = webhook.repo
        review.base_branch = webhook.base_branch
        review.pull_author_github_user_id = webhook.pull_author_github_user_id
        review.pull_author_login = webhook.pull_author_login
        review.latest_base_sha = webhook.base_sha
        review.latest_head_sha = webhook.head_sha
        review.status = ManagedReviewStatus.PENDING

    return installation, repository, review, reusable_check_run_id


def _find_active_snapshot_job(
    *,
    db_session: Session,
    managed_review_id: uuid.UUID,
    base_sha: str,
    head_sha: str,
) -> SnapshotBuildJob | None:
    return db_session.execute(
        select(SnapshotBuildJob)
        .where(
            SnapshotBuildJob.managed_review_id == managed_review_id,
            SnapshotBuildJob.base_sha == base_sha,
            SnapshotBuildJob.head_sha == head_sha,
            SnapshotBuildJob.status.in_(
                (
                    SnapshotBuildJobStatus.QUEUED,
                    SnapshotBuildJobStatus.RUNNING,
                    SnapshotBuildJobStatus.RETRYABLE_FAILED,
                )
            ),
        )
        .order_by(SnapshotBuildJob.scheduled_at.asc())
        .limit(1)
    ).scalar_one_or_none()


def _build_notebook_inputs_for_review(
    *,
    settings: ApiSettings,
    github_client: ManagedGitHubClient,
    installation_id: int,
    repository_full_name: str,
    pull_number: int,
    base_sha: str,
    head_sha: str,
    limits: DiffLimits,
) -> list[NotebookInput]:
    raw_files = github_client.list_pull_request_files(
        settings=settings,
        installation_id=installation_id,
        repository=repository_full_name,
        pull_number=pull_number,
    )
    inputs: list[NotebookInput] = []
    notebook_count = 0

    for raw_file in raw_files:
        selection = _coerce_notebook_file(raw_file)
        if selection is None:
            continue
        should_fetch_content = notebook_count < limits.max_notebooks_per_pr
        notebook_count += 1

        skip_for_size = (
            selection["head_size_bytes"] is not None
            and selection["head_size_bytes"] > limits.max_notebook_bytes
        )
        base_content = None
        head_content = None

        if should_fetch_content and not skip_for_size and selection["base_path"] is not None:
            base_content = github_client.get_file_content(
                settings=settings,
                installation_id=installation_id,
                repository=repository_full_name,
                path=str(selection["base_path"]),
                ref=base_sha,
            )
        if should_fetch_content and not skip_for_size and selection["head_path"] is not None:
            head_content = github_client.get_file_content(
                settings=settings,
                installation_id=installation_id,
                repository=repository_full_name,
                path=str(selection["head_path"]),
                ref=head_sha,
            )

        inputs.append(
            NotebookInput(
                path=str(selection["path"]),
                change_type=str(selection["change_type"]),
                base_content=base_content,
                head_content=head_content,
                head_size_bytes=selection["head_size_bytes"],
            )
        )

    return inputs


def _coerce_notebook_file(raw_file: Any) -> dict[str, Any] | None:
    if isinstance(raw_file, Mapping):
        payload = raw_file
    else:
        payload = {
            "filename": getattr(raw_file, "filename", getattr(raw_file, "path", None)),
            "status": getattr(raw_file, "status", None),
            "previous_filename": getattr(
                raw_file,
                "previous_filename",
                getattr(raw_file, "previous_path", None),
            ),
            "size": getattr(raw_file, "size", getattr(raw_file, "size_bytes", None)),
        }

    filename = payload.get("filename", payload.get("path"))
    status = str(payload.get("status", "")).strip().lower()
    previous_filename = payload.get("previous_filename", payload.get("previous_path"))
    size = payload.get("size", payload.get("size_bytes"))
    size_bytes = size if isinstance(size, int) else None
    current_is_notebook = isinstance(filename, str) and filename.lower().endswith(".ipynb")
    previous_is_notebook = isinstance(previous_filename, str) and previous_filename.lower().endswith(
        ".ipynb"
    )

    if not current_is_notebook and not previous_is_notebook:
        return None
    if status in {"removed", "deleted"}:
        if not current_is_notebook:
            return None
        return {
            "path": filename,
            "change_type": "deleted",
            "base_path": filename,
            "head_path": None,
            "head_size_bytes": size_bytes,
        }
    if status == "added":
        if not current_is_notebook:
            return None
        return {
            "path": filename,
            "change_type": "added",
            "base_path": None,
            "head_path": filename,
            "head_size_bytes": size_bytes,
        }
    if status == "renamed":
        if current_is_notebook and previous_is_notebook:
            return {
                "path": filename,
                "change_type": "modified",
                "base_path": previous_filename,
                "head_path": filename,
                "head_size_bytes": size_bytes,
            }
        if previous_is_notebook and not current_is_notebook:
            return {
                "path": previous_filename,
                "change_type": "deleted",
                "base_path": previous_filename,
                "head_path": None,
                "head_size_bytes": size_bytes,
            }
        if current_is_notebook:
            return {
                "path": filename,
                "change_type": "added",
                "base_path": None,
                "head_path": filename,
                "head_size_bytes": size_bytes,
            }
        return None
    if not current_is_notebook:
        return None
    return {
        "path": filename,
        "change_type": "modified",
        "base_path": filename,
        "head_path": filename,
        "head_size_bytes": size_bytes,
    }


def _resolve_managed_reviewer(
    *,
    db_session: Session,
    settings: ApiSettings,
    installation: GitHubInstallation,
    playbooks: Sequence[ReviewerPlaybook],
    gateway_client: LiteLLMGatewayClient,
) -> tuple[Any, ManagedReviewProviderState, list[str]]:
    config = _load_active_managed_ai_gateway_config(
        db_session=db_session,
        installation_id=installation.id,
    )
    if config is None:
        return NoneProvider(), ManagedReviewProviderState(gateway_enabled=False), []

    try:
        api_key, static_headers = _decrypt_managed_ai_gateway_secrets(
            settings=settings,
            config=config,
        )
    except ValueError as exc:
        notice = (
            f"Managed LiteLLM review unavailable: {_truncate_provider_error(exc)}. "
            "Used deterministic local findings."
        )
        return (
            NoneProvider(),
            ManagedReviewProviderState(
                gateway_enabled=True,
                used_fallback=True,
                fallback_notice=notice,
            ),
            [notice],
        )

    return (
        ManagedLiteLLMReviewer(
            config=config,
            api_key=api_key,
            static_headers=static_headers,
            reviewer_playbooks=playbooks,
            gateway_client=gateway_client,
        ),
        ManagedReviewProviderState(gateway_enabled=True),
        [],
    )


def _load_active_managed_ai_gateway_config(
    *,
    db_session: Session,
    installation_id: uuid.UUID,
) -> ManagedAiGatewayConfig | None:
    return db_session.execute(
        select(ManagedAiGatewayConfig).where(
            ManagedAiGatewayConfig.installation_id == installation_id,
            ManagedAiGatewayConfig.active.is_(True),
            ManagedAiGatewayConfig.provider_kind == ManagedAiGatewayProviderKind.LITELLM,
        )
    ).scalar_one_or_none()


def _decrypt_managed_ai_gateway_secrets(
    *,
    settings: ApiSettings,
    config: ManagedAiGatewayConfig,
) -> tuple[str, dict[str, str]]:
    cipher = SessionTokenCipher(settings.encryption_key)
    try:
        api_key = cipher.decrypt(config.api_key_encrypted)
    except SessionCipherError as exc:
        raise ValueError("stored LiteLLM API key could not be decrypted") from exc

    if not config.static_headers_encrypted_json:
        return api_key, {}

    try:
        static_headers_payload = cipher.decrypt(config.static_headers_encrypted_json)
    except SessionCipherError as exc:
        raise ValueError("stored LiteLLM static headers could not be decrypted") from exc

    try:
        raw_headers = json.loads(static_headers_payload)
    except json.JSONDecodeError as exc:
        raise ValueError("stored LiteLLM static headers are invalid") from exc

    if not isinstance(raw_headers, Mapping):
        raise ValueError("stored LiteLLM static headers are invalid")

    return api_key, {str(key): str(value) for key, value in raw_headers.items()}


def _merge_review_notices(*notice_groups: Sequence[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in notice_groups:
        for raw_notice in group:
            if not isinstance(raw_notice, str):
                continue
            notice = raw_notice.strip()
            if not notice or notice in seen:
                continue
            seen.add(notice)
            merged.append(notice)
    return merged


def _merge_snapshot_reviewer_guidance(
    base_items: Sequence[Mapping[str, Any]],
    ai_items: Sequence[Any],
) -> list[dict[str, Any]]:
    combined: list[dict[str, Any]] = [dict(item) for item in base_items]
    for item in ai_items:
        combined.append(asdict(item))

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in combined:
        notebook_path = str(item.get("notebook_path", "")).strip()
        message = " ".join(str(item.get("message", "")).strip().lower().split())
        if not notebook_path or not message:
            continue
        key = (notebook_path, message)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    priority_order = {"high": 0, "medium": 1, "low": 2}
    source_order = {"built_in": 0, "playbook": 1, "claude": 2}
    deduped.sort(
        key=lambda item: (
            priority_order.get(str(item.get("priority")), 99),
            source_order.get(str(item.get("source")), 99),
            str(item.get("notebook_path")),
            str(item.get("code")),
            str(item.get("message")),
        )
    )
    return deduped


def _build_managed_gateway_prompt(redacted_payload: Mapping[str, Any]) -> str:
    payload_json = json.dumps(
        redacted_payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    schema_json = json.dumps(
        {
            "summary": "string|null",
            "flagged_issues": [
                {
                    "notebook_path": "string",
                    "locator": {
                        "cell_id": "string|null",
                        "base_index": "int|null",
                        "head_index": "int|null",
                        "display_index": "int|null",
                    },
                    "code": "string",
                    "category": (
                        "documentation|output|error|data|metadata|policy|review_guidance"
                    ),
                    "severity": "low|medium|high",
                    "confidence": "low|medium|high|null",
                    "message": "string",
                }
            ],
            "reviewer_guidance": [
                {
                    "notebook_path": "string",
                    "locator": {
                        "cell_id": "string|null",
                        "base_index": "int|null",
                        "head_index": "int|null",
                        "display_index": "int|null",
                    },
                    "code": "claude:string",
                    "source": "claude",
                    "label": "string|null",
                    "priority": "low|medium|high",
                    "message": "string",
                }
            ],
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        "You are NotebookLens. Review the provided notebook diff payload and return ONLY valid JSON.\n"
        "No markdown. No code fences. No prose outside JSON.\n"
        "Follow this exact schema and key names:\n"
        f"{schema_json}\n"
        "Rules:\n"
        "- Keep findings conservative, objective, and tied to changed cells.\n"
        "- Only reference notebook paths that exist in the payload.\n"
        "- Include flagged_issues only when meaningful.\n"
        "- base_reviewer_guidance already contains deterministic and playbook guidance.\n"
        "- reviewer_guidance must contain only NEW AI-added guidance items.\n"
        "- Do not repeat, remove, or rewrite any base_reviewer_guidance items.\n"
        "- Every reviewer_guidance item must use source=claude and code values starting with claude:.\n"
        "- summary may be null when no extra AI summary is useful.\n"
        "Diff payload:\n"
        f"{payload_json}"
    )


def _extract_litellm_text(
    payload: Mapping[str, Any],
    *,
    use_responses_api: bool,
) -> str:
    if use_responses_api:
        output_text = payload.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()

        output = payload.get("output")
        if isinstance(output, list):
            parts: list[str] = []
            for block in output:
                if not isinstance(block, Mapping):
                    continue
                content = block.get("content")
                if not isinstance(content, list):
                    continue
                for item in content:
                    if not isinstance(item, Mapping):
                        continue
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
            merged = "\n".join(parts).strip()
            if merged:
                return merged
        raise LiteLLMGatewayError("responses output did not include text")

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LiteLLMGatewayError("chat completions response missing choices")
    first_choice = choices[0]
    if not isinstance(first_choice, Mapping):
        raise LiteLLMGatewayError("chat completions response choice was invalid")
    message = first_choice.get("message")
    if not isinstance(message, Mapping):
        raise LiteLLMGatewayError("chat completions response missing message")
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        parts = [
            str(item.get("text", "")).strip()
            for item in content
            if isinstance(item, Mapping) and str(item.get("text", "")).strip()
        ]
        merged = "\n".join(parts).strip()
        if merged:
            return merged
    raise LiteLLMGatewayError("chat completions response had no text content")


def _extract_usage_value(usage: Any, *, keys: Sequence[str]) -> int | None:
    if not isinstance(usage, Mapping):
        return None
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return None


def _create_pending_snapshot(
    *,
    db_session: Session,
    review: ManagedReview,
    job: SnapshotBuildJob,
) -> ReviewSnapshot:
    next_index = (
        db_session.execute(
            select(func.max(ReviewSnapshot.snapshot_index)).where(
                ReviewSnapshot.managed_review_id == review.id
            )
        ).scalar_one()
        or 0
    ) + 1
    snapshot = ReviewSnapshot(
        managed_review_id=review.id,
        base_sha=job.base_sha,
        head_sha=job.head_sha,
        snapshot_index=next_index,
        status=ReviewSnapshotStatus.PENDING,
        schema_version=REVIEW_SNAPSHOT_SCHEMA_VERSION,
        summary_text=None,
        flagged_findings_json=[],
        reviewer_guidance_json=[],
        snapshot_payload_json={
            "schema_version": REVIEW_SNAPSHOT_SCHEMA_VERSION,
            "review": {"notices": [], "notebooks": []},
        },
        notebook_count=0,
        changed_cell_count=0,
        failure_reason=None,
    )
    db_session.add(snapshot)
    db_session.flush()
    return snapshot


def _load_reviewer_playbooks(config_text: str | None) -> tuple[tuple[ReviewerPlaybook, ...], list[str]]:
    if config_text is None:
        return (), []
    try:
        return parse_reviewer_playbooks(config_text), []
    except NotebookLensConfigError as exc:
        return (), [f"{_CONFIG_PATH}: invalid config ignored ({exc})"]


def _persist_review_assets(
    *,
    db_session: Session,
    snapshot: ReviewSnapshot,
    review_assets: Sequence[ReviewAssetDraft],
) -> dict[str, str]:
    asset_ids_by_key: dict[str, str] = {}
    assets_by_sha: dict[str, ReviewAsset] = {}
    for review_asset in review_assets:
        existing_asset = assets_by_sha.get(review_asset.sha256)
        if existing_asset is None:
            existing_asset = ReviewAsset(
                snapshot_id=snapshot.id,
                sha256=review_asset.sha256,
                mime_type=review_asset.mime_type,
                byte_size=review_asset.byte_size,
                width=review_asset.width,
                height=review_asset.height,
                storage_key=_review_asset_storage_key(snapshot.id, review_asset),
                content_bytes=review_asset.content_bytes,
            )
            db_session.add(existing_asset)
            db_session.flush()
            assets_by_sha[review_asset.sha256] = existing_asset
        asset_ids_by_key[review_asset.asset_key] = str(existing_asset.id)
    return asset_ids_by_key


def _review_asset_storage_key(snapshot_id: uuid.UUID, review_asset: ReviewAssetDraft) -> str:
    extension = review_asset.mime_type.split("/", 1)[1]
    return f"review-snapshots/{snapshot_id}/assets/{review_asset.sha256}.{extension}"


def _rewrite_snapshot_payload_asset_refs(
    *,
    snapshot_payload: Mapping[str, Any],
    asset_ids_by_key: Mapping[str, str],
) -> dict[str, Any]:
    notebooks = snapshot_payload.get("review", {}).get("notebooks", [])
    rewritten_notebooks: list[dict[str, Any]] = []
    for notebook in notebooks:
        render_rows = notebook.get("render_rows")
        if not isinstance(render_rows, list):
            rewritten_notebooks.append(dict(notebook))
            continue
        rewritten_rows: list[dict[str, Any]] = []
        for row in render_rows:
            outputs = row.get("outputs")
            items = outputs.get("items") if isinstance(outputs, dict) else None
            if not isinstance(items, list):
                rewritten_rows.append(dict(row))
                continue
            rewritten_items: list[dict[str, Any]] = []
            for item in items:
                if item.get("kind") != "image" or "asset_key" not in item:
                    rewritten_items.append(dict(item))
                    continue
                asset_key = str(item["asset_key"])
                asset_id = asset_ids_by_key.get(asset_key)
                if asset_id is None:
                    raise ValueError(f"Missing persisted review asset for key: {asset_key}")
                rewritten_items.append(
                    {
                        "kind": "image",
                        "asset_id": asset_id,
                        "mime_type": item.get("mime_type"),
                        "width": item.get("width"),
                        "height": item.get("height"),
                        "change_type": item.get("change_type"),
                    }
                )
            rewritten_rows.append(
                {
                    **row,
                    "outputs": {
                        **outputs,
                        "items": rewritten_items,
                    },
                }
            )
        rewritten_notebooks.append(
            {
                **notebook,
                "render_rows": rewritten_rows,
            }
        )
    return {
        **snapshot_payload,
        "review": {
            **snapshot_payload["review"],
            "notebooks": rewritten_notebooks,
        },
    }


def _job_matches_latest_review(*, review: ManagedReview, job: SnapshotBuildJob) -> bool:
    return review.latest_base_sha == job.base_sha and review.latest_head_sha == job.head_sha


def _short_sha(value: str) -> str:
    return value[:7]


def _truncate_error(exc: BaseException) -> str:
    message = str(exc).strip()
    if not message:
        return exc.__class__.__name__
    return message[:300]


def _truncate_provider_error(exc: BaseException) -> str:
    return _truncate_error(exc)


def _require_mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if isinstance(value, Mapping):
        return value
    raise ManagedWebhookPayloadError(f"GitHub webhook payload is missing `{key}`")


def _require_str(payload: Mapping[str, Any] | Any, key: str) -> str:
    value = payload.get(key) if isinstance(payload, Mapping) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ManagedWebhookPayloadError(f"GitHub webhook payload is missing `{key}`")


def _require_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, int):
        return value
    raise ManagedWebhookPayloadError(f"GitHub webhook payload is missing `{key}`")


def _optional_int(payload: Mapping[str, Any] | Any, key: str) -> int | None:
    value = payload.get(key) if isinstance(payload, Mapping) else None
    return value if isinstance(value, int) else None


def _optional_str(payload: Mapping[str, Any] | Any, key: str) -> str | None:
    value = payload.get(key) if isinstance(payload, Mapping) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


__all__ = [
    "LiteLLMGatewayClient",
    "LiteLLMGatewayResponse",
    "MANAGED_REVIEW_CHECK_RUN_NAME",
    "ManagedWebhookPayloadError",
    "PullRequestWebhook",
    "SnapshotBuildResult",
    "SUPPORTED_PULL_REQUEST_ACTIONS",
    "WebhookIngestionResult",
    "ingest_pull_request_webhook",
    "parse_pull_request_webhook",
    "run_snapshot_build_worker_once",
]
