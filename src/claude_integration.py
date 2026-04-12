"""Provider integration for deterministic and Claude-backed notebook review."""

from __future__ import annotations

from abc import ABC, abstractmethod
import copy
from dataclasses import dataclass
from fnmatch import fnmatchcase
import json
from pathlib import PurePosixPath
import re
import time
from typing import Any, Dict, List, Literal, Optional, Sequence, Set, Tuple

from urllib import error as urllib_error
from urllib import request as urllib_request

from .diff_engine import (
    CellLocator,
    FlaggedIssue,
    NotebookDiff,
    NotebookFileDiff,
    ReviewResult,
    ReviewerGuidanceItem,
    notebook_diff_to_dict,
)


ProviderName = Literal["none", "claude"]

DEFAULT_ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_CLAUDE_MODEL = "claude-3-5-sonnet-latest"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30
DEFAULT_MAX_AI_INPUT_TOKENS = 16_000
DEFAULT_MAX_OUTPUT_TOKENS = 1_200
DEFAULT_RETRY_ATTEMPTS = 1

_SEVERITIES = {"low", "medium", "high"}
_GUIDANCE_PRIORITIES = {"low", "medium", "high"}
_CATEGORIES = {
    "documentation",
    "output",
    "error",
    "data",
    "metadata",
    "policy",
    "review_guidance",
}
_CONFIDENCE = {"low", "medium", "high"}

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    (
        r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASS|API[_-]?KEY|ACCESS[_-]?KEY|"
        r"PRIVATE[_-]?KEY|CONN(?:ECTION)?(?:_STRING)?|DSN|URI|URL)[A-Z0-9_]*)\b\s*[:=]\s*"
        r"([^\s,;]+)"
    )
)
_URI_CREDENTIALS_RE = re.compile(
    r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/\s:@]{1,128}):([^@\s/]{1,256})@"
)
_CONNECTION_STRING_RE = re.compile(
    r"(?i)\b(?:jdbc:)?(?:postgres(?:ql)?|mysql|mssql|mongodb(?:\+srv)?|redis|amqp|snowflake)://[^\s\"']+"
)
_LONG_BASE64_RE = re.compile(r"\b[A-Za-z0-9+/]{80,}={0,2}\b")

_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class ProviderConfig:
    ai_provider: ProviderName = "none"
    ai_api_key: Optional[str] = None
    redact_secrets: bool = True
    redact_emails: bool = True
    claude_model: str = DEFAULT_CLAUDE_MODEL
    anthropic_api_url: str = DEFAULT_ANTHROPIC_API_URL
    anthropic_version: str = DEFAULT_ANTHROPIC_VERSION
    request_timeout_seconds: int = DEFAULT_REQUEST_TIMEOUT_SECONDS
    max_ai_input_tokens: int = DEFAULT_MAX_AI_INPUT_TOKENS
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS
    base_reviewer_guidance: Tuple[ReviewerGuidanceItem, ...] = ()


@dataclass(frozen=True)
class ProviderRunMetadata:
    provider: ProviderName
    claude_called: bool
    used_fallback: bool
    fallback_reason: Optional[str]
    input_tokens: Optional[int]
    output_tokens: Optional[int]


class ClaudeRequestError(RuntimeError):
    """Raised when a Claude request fails."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class ReviewResultValidationError(ValueError):
    """Raised when model output does not match strict ReviewResult schema."""


class ProviderInterface(ABC):
    """Provider contract for notebook review enrichment."""

    def __init__(self) -> None:
        self.last_run_metadata = ProviderRunMetadata(
            provider="none",
            claude_called=False,
            used_fallback=False,
            fallback_reason=None,
            input_tokens=None,
            output_tokens=None,
        )

    @abstractmethod
    def review(self, diff: NotebookDiff) -> ReviewResult:
        """Review a notebook diff and return a structured review result."""


class NoneProvider(ProviderInterface):
    """Deterministic local provider used for `ai-provider: none` and fallback paths."""

    def __init__(
        self,
        *,
        base_reviewer_guidance: Sequence[ReviewerGuidanceItem] = (),
    ) -> None:
        super().__init__()
        self.base_reviewer_guidance = tuple(base_reviewer_guidance)

    def review(self, diff: NotebookDiff) -> ReviewResult:
        issues = _deterministic_findings(diff)
        self.last_run_metadata = ProviderRunMetadata(
            provider="none",
            claude_called=False,
            used_fallback=False,
            fallback_reason=None,
            input_tokens=None,
            output_tokens=None,
        )
        return ReviewResult(
            summary=None,
            flagged_issues=issues,
            reviewer_guidance=list(self.base_reviewer_guidance),
        )


class ClaudeProvider(ProviderInterface):
    """Claude-backed provider with strict JSON validation and deterministic fallback."""

    def __init__(
        self,
        *,
        api_key: Optional[str],
        redact_secrets: bool = True,
        redact_emails: bool = True,
        model: str = DEFAULT_CLAUDE_MODEL,
        anthropic_api_url: str = DEFAULT_ANTHROPIC_API_URL,
        anthropic_version: str = DEFAULT_ANTHROPIC_VERSION,
        request_timeout_seconds: int = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        max_ai_input_tokens: int = DEFAULT_MAX_AI_INPUT_TOKENS,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
        base_reviewer_guidance: Sequence[ReviewerGuidanceItem] = (),
        session: Optional[Any] = None,
        fallback_provider: Optional[ProviderInterface] = None,
    ) -> None:
        super().__init__()
        self.api_key = (api_key or "").strip()
        self.redact_secrets = redact_secrets
        self.redact_emails = redact_emails
        self.model = model
        self.anthropic_api_url = anthropic_api_url
        self.anthropic_version = anthropic_version
        self.request_timeout_seconds = request_timeout_seconds
        self.max_ai_input_tokens = max_ai_input_tokens
        self.max_output_tokens = max_output_tokens
        self.retry_attempts = max(0, retry_attempts)
        self.base_reviewer_guidance = tuple(base_reviewer_guidance)
        self.session = session
        self.fallback_provider = fallback_provider or NoneProvider(
            base_reviewer_guidance=self.base_reviewer_guidance
        )

    def review(self, diff: NotebookDiff) -> ReviewResult:
        if not self.api_key:
            return self._fallback(diff, "missing ai-api-key for ai-provider=claude", 0, 0)

        payload = _prepare_ai_payload(
            diff=diff,
            base_reviewer_guidance=self.base_reviewer_guidance,
            redact_secrets=self.redact_secrets,
            redact_emails=self.redact_emails,
            max_ai_input_tokens=self.max_ai_input_tokens,
        )
        prompt = _build_claude_prompt(payload)

        total_input_tokens = 0
        total_output_tokens = 0
        raw_response: Optional[str] = None

        try:
            raw_response, input_tokens, output_tokens = self._request_with_retry(prompt)
            total_input_tokens += input_tokens
            total_output_tokens += output_tokens
            parsed = parse_strict_review_result(raw_response, diff)
            self.last_run_metadata = ProviderRunMetadata(
                provider="claude",
                claude_called=True,
                used_fallback=False,
                fallback_reason=None,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
            )
            return ReviewResult(
                summary=parsed.summary,
                flagged_issues=parsed.flagged_issues,
                reviewer_guidance=_merge_reviewer_guidance(
                    self.base_reviewer_guidance,
                    parsed.reviewer_guidance,
                ),
            )
        except ReviewResultValidationError as exc:
            repair_prompt = _build_repair_prompt(raw_response or "", str(exc))
            try:
                repaired_text, input_tokens, output_tokens = self._request_with_retry(repair_prompt)
                total_input_tokens += input_tokens
                total_output_tokens += output_tokens
                parsed = parse_strict_review_result(repaired_text, diff)
                self.last_run_metadata = ProviderRunMetadata(
                    provider="claude",
                    claude_called=True,
                    used_fallback=False,
                    fallback_reason=None,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                )
                return ReviewResult(
                    summary=parsed.summary,
                    flagged_issues=parsed.flagged_issues,
                    reviewer_guidance=_merge_reviewer_guidance(
                        self.base_reviewer_guidance,
                        parsed.reviewer_guidance,
                    ),
                )
            except (ClaudeRequestError, ReviewResultValidationError) as repair_exc:
                return self._fallback(
                    diff,
                    f"invalid Claude JSON after one repair attempt ({_stable_reason(repair_exc)})",
                    total_input_tokens,
                    total_output_tokens,
                )
        except ClaudeRequestError as exc:
            return self._fallback(
                diff,
                f"Claude request failed after retry ({_stable_reason(exc)})",
                total_input_tokens,
                total_output_tokens,
            )

    def _request_with_retry(self, prompt: str) -> Tuple[str, int, int]:
        last_error: Optional[ClaudeRequestError] = None
        for attempt in range(self.retry_attempts + 1):
            try:
                return self._send_request(prompt)
            except ClaudeRequestError as exc:
                last_error = exc
                if not exc.retryable or attempt >= self.retry_attempts:
                    raise
                time.sleep(min(2**attempt, 2))
        assert last_error is not None
        raise last_error

    def _send_request(self, prompt: str) -> Tuple[str, int, int]:
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": self.anthropic_version,
            "content-type": "application/json",
        }
        request_payload = {
            "model": self.model,
            "max_tokens": self.max_output_tokens,
            "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self.session is None:
            payload = self._send_with_urllib(headers, request_payload)
        else:
            payload = self._send_with_session(headers, request_payload)

        raw_text = _extract_anthropic_text(payload)
        usage = payload.get("usage", {})
        input_tokens = _read_int(usage.get("input_tokens"))
        output_tokens = _read_int(usage.get("output_tokens"))
        return raw_text, input_tokens, output_tokens

    def _send_with_session(
        self,
        headers: Dict[str, str],
        request_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        try:
            response = self.session.post(
                self.anthropic_api_url,
                headers=headers,
                json=request_payload,
                timeout=self.request_timeout_seconds,
            )
        except Exception as exc:
            raise ClaudeRequestError(f"network error: {exc}", retryable=True) from exc

        status_code = getattr(response, "status_code", None)
        text = str(getattr(response, "text", ""))[:300]
        if isinstance(status_code, int) and (status_code == 429 or status_code >= 500):
            raise ClaudeRequestError(f"HTTP {status_code}: {text}", retryable=True)
        if isinstance(status_code, int) and status_code >= 400:
            raise ClaudeRequestError(f"HTTP {status_code}: {text}", retryable=False)

        try:
            parsed = response.json()
        except Exception as exc:
            raise ClaudeRequestError("Claude response was not JSON", retryable=True) from exc
        if not isinstance(parsed, dict):
            raise ClaudeRequestError("Claude response JSON must be an object", retryable=True)
        return parsed

    def _send_with_urllib(
        self,
        headers: Dict[str, str],
        request_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        body = json.dumps(request_payload).encode("utf-8")
        request = urllib_request.Request(
            self.anthropic_api_url,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib_request.urlopen(request, timeout=self.request_timeout_seconds) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib_error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp is not None else ""
            retryable = exc.code == 429 or exc.code >= 500
            raise ClaudeRequestError(
                f"HTTP {exc.code}: {body[:300]}",
                retryable=retryable,
            ) from exc
        except urllib_error.URLError as exc:
            raise ClaudeRequestError(f"network error: {exc}", retryable=True) from exc

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ClaudeRequestError("Claude response was not JSON", retryable=True) from exc
        if not isinstance(parsed, dict):
            raise ClaudeRequestError("Claude response JSON must be an object", retryable=True)
        return parsed

    def _fallback(
        self,
        diff: NotebookDiff,
        reason: str,
        input_tokens: int,
        output_tokens: int,
    ) -> ReviewResult:
        base = self.fallback_provider.review(diff)
        note = f"Claude unavailable: {reason}. Used deterministic local findings."
        merged_summary = note if base.summary is None else f"{note}\n\n{base.summary}"
        self.last_run_metadata = ProviderRunMetadata(
            provider="claude",
            claude_called=True,
            used_fallback=True,
            fallback_reason=reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        return ReviewResult(
            summary=merged_summary,
            flagged_issues=base.flagged_issues,
            reviewer_guidance=base.reviewer_guidance,
        )


def build_provider(config: ProviderConfig) -> ProviderInterface:
    """Factory for the supported provider modes (`none` and `claude`)."""
    if config.ai_provider == "none":
        return NoneProvider(base_reviewer_guidance=config.base_reviewer_guidance)
    if config.ai_provider == "claude":
        return ClaudeProvider(
            api_key=config.ai_api_key,
            redact_secrets=config.redact_secrets,
            redact_emails=config.redact_emails,
            model=config.claude_model,
            anthropic_api_url=config.anthropic_api_url,
            anthropic_version=config.anthropic_version,
            request_timeout_seconds=config.request_timeout_seconds,
            max_ai_input_tokens=config.max_ai_input_tokens,
            max_output_tokens=config.max_output_tokens,
            retry_attempts=config.retry_attempts,
            base_reviewer_guidance=config.base_reviewer_guidance,
        )
    raise ValueError(f"Unsupported ai-provider for NotebookLens: {config.ai_provider}")


def parse_strict_review_result(raw_text: str, diff: NotebookDiff) -> ReviewResult:
    """Parse and strictly validate Claude JSON against ReviewResult schema."""
    raw_data = _load_model_json_object(raw_text)
    _expect_exact_keys(
        raw_data,
        {"summary", "flagged_issues", "reviewer_guidance"},
        context="ReviewResult",
    )

    summary = raw_data["summary"]
    if summary is not None and not isinstance(summary, str):
        raise ReviewResultValidationError("ReviewResult.summary must be string or null")
    if isinstance(summary, str):
        summary = summary.strip() or None

    flagged_raw = raw_data["flagged_issues"]
    if not isinstance(flagged_raw, list):
        raise ReviewResultValidationError("ReviewResult.flagged_issues must be an array")

    valid_paths = {notebook.path for notebook in diff.notebooks}
    flagged_issues: List[FlaggedIssue] = []
    for idx, raw_issue in enumerate(flagged_raw):
        flagged_issues.append(_parse_issue(raw_issue, idx, valid_paths))

    reviewer_guidance_raw = raw_data["reviewer_guidance"]
    if not isinstance(reviewer_guidance_raw, list):
        raise ReviewResultValidationError("ReviewResult.reviewer_guidance must be an array")

    reviewer_guidance: List[ReviewerGuidanceItem] = []
    for idx, raw_item in enumerate(reviewer_guidance_raw):
        reviewer_guidance.append(_parse_reviewer_guidance_item(raw_item, idx, valid_paths))

    return ReviewResult(
        summary=summary,
        flagged_issues=flagged_issues,
        reviewer_guidance=reviewer_guidance,
    )


def _parse_issue(raw_issue: Any, index: int, valid_paths: Set[str]) -> FlaggedIssue:
    if not isinstance(raw_issue, dict):
        raise ReviewResultValidationError(f"flagged_issues[{index}] must be an object")

    _expect_exact_keys(
        raw_issue,
        {"notebook_path", "locator", "code", "category", "severity", "confidence", "message"},
        context=f"flagged_issues[{index}]",
    )

    notebook_path = raw_issue["notebook_path"]
    if not isinstance(notebook_path, str) or not notebook_path.strip():
        raise ReviewResultValidationError(f"flagged_issues[{index}].notebook_path must be non-empty")
    notebook_path = notebook_path.strip()
    if notebook_path not in valid_paths:
        raise ReviewResultValidationError(
            f"flagged_issues[{index}].notebook_path is not present in NotebookDiff"
        )

    code = raw_issue["code"]
    if not isinstance(code, str) or not code.strip():
        raise ReviewResultValidationError(f"flagged_issues[{index}].code must be non-empty")
    code = code.strip()

    category = raw_issue["category"]
    if category not in _CATEGORIES:
        raise ReviewResultValidationError(f"flagged_issues[{index}].category is invalid: {category}")

    severity = raw_issue["severity"]
    if severity not in _SEVERITIES:
        raise ReviewResultValidationError(f"flagged_issues[{index}].severity is invalid: {severity}")

    confidence = raw_issue["confidence"]
    if confidence is not None and confidence not in _CONFIDENCE:
        raise ReviewResultValidationError(
            f"flagged_issues[{index}].confidence must be low/medium/high/null"
        )

    message = raw_issue["message"]
    if not isinstance(message, str) or not message.strip():
        raise ReviewResultValidationError(f"flagged_issues[{index}].message must be non-empty")
    message = message.strip()

    locator = _parse_locator(raw_issue["locator"], context=f"flagged_issues[{index}].locator")
    return FlaggedIssue(
        notebook_path=notebook_path,
        locator=locator,
        code=code,
        category=category,
        severity=severity,
        confidence=confidence,
        message=message,
    )


def _parse_locator(raw_locator: Any, *, context: str) -> CellLocator:
    if not isinstance(raw_locator, dict):
        raise ReviewResultValidationError(f"{context} must be an object")
    _expect_exact_keys(
        raw_locator,
        {"cell_id", "base_index", "head_index", "display_index"},
        context=context,
    )

    cell_id = raw_locator["cell_id"]
    if cell_id is not None and (not isinstance(cell_id, str) or not cell_id.strip()):
        raise ReviewResultValidationError(f"{context}.cell_id must be string or null")
    if isinstance(cell_id, str):
        cell_id = cell_id.strip()

    base_index = _validate_optional_int(
        raw_locator["base_index"],
        field=f"{context}.base_index",
        minimum=0,
    )
    head_index = _validate_optional_int(
        raw_locator["head_index"],
        field=f"{context}.head_index",
        minimum=0,
    )
    display_index = _validate_optional_int(
        raw_locator["display_index"],
        field=f"{context}.display_index",
        minimum=1,
    )

    return CellLocator(
        cell_id=cell_id,
        base_index=base_index,
        head_index=head_index,
        display_index=display_index,
    )


def _parse_optional_locator(raw_locator: Any, guidance_index: int) -> Optional[CellLocator]:
    if raw_locator is None:
        return None
    return _parse_locator(raw_locator, context=f"reviewer_guidance[{guidance_index}].locator")


def _parse_reviewer_guidance_item(
    raw_item: Any,
    index: int,
    valid_paths: Set[str],
) -> ReviewerGuidanceItem:
    if not isinstance(raw_item, dict):
        raise ReviewResultValidationError(f"reviewer_guidance[{index}] must be an object")

    _expect_exact_keys(
        raw_item,
        {"notebook_path", "locator", "code", "source", "label", "priority", "message"},
        context=f"reviewer_guidance[{index}]",
    )

    notebook_path = raw_item["notebook_path"]
    if not isinstance(notebook_path, str) or not notebook_path.strip():
        raise ReviewResultValidationError(
            f"reviewer_guidance[{index}].notebook_path must be non-empty"
        )
    notebook_path = notebook_path.strip()
    if notebook_path not in valid_paths:
        raise ReviewResultValidationError(
            f"reviewer_guidance[{index}].notebook_path is not present in NotebookDiff"
        )

    code = raw_item["code"]
    if not isinstance(code, str) or not code.strip():
        raise ReviewResultValidationError(f"reviewer_guidance[{index}].code must be non-empty")
    code = code.strip()
    if not code.startswith("claude:"):
        raise ReviewResultValidationError(
            f"reviewer_guidance[{index}].code must start with claude:"
        )

    source = raw_item["source"]
    if source != "claude":
        raise ReviewResultValidationError(
            f"reviewer_guidance[{index}].source must be claude for Claude responses"
        )

    label = raw_item["label"]
    if label is not None and (not isinstance(label, str) or not label.strip()):
        raise ReviewResultValidationError(
            f"reviewer_guidance[{index}].label must be string or null"
        )
    if isinstance(label, str):
        label = label.strip()

    priority = raw_item["priority"]
    if priority not in _GUIDANCE_PRIORITIES:
        raise ReviewResultValidationError(
            f"reviewer_guidance[{index}].priority is invalid: {priority}"
        )

    message = raw_item["message"]
    if not isinstance(message, str) or not message.strip():
        raise ReviewResultValidationError(
            f"reviewer_guidance[{index}].message must be non-empty"
        )
    message = message.strip()

    locator = _parse_optional_locator(raw_item["locator"], index)
    return ReviewerGuidanceItem(
        notebook_path=notebook_path,
        locator=locator,
        code=code,
        source=source,
        label=label,
        priority=priority,
        message=message,
    )


def _validate_optional_int(value: Any, *, field: str, minimum: int) -> Optional[int]:
    if value is None:
        return None
    if not isinstance(value, int):
        raise ReviewResultValidationError(f"{field} must be integer or null")
    if value < minimum:
        raise ReviewResultValidationError(f"{field} must be >= {minimum} when present")
    return value


def _load_model_json_object(raw_text: str) -> Dict[str, Any]:
    stripped = raw_text.strip()
    if not stripped:
        raise ReviewResultValidationError("Claude response was empty")

    candidates = [stripped]
    fenced_match = _FENCED_JSON_RE.search(stripped)
    if fenced_match is not None:
        candidates.append(fenced_match.group(1).strip())

    first_brace = stripped.find("{")
    last_brace = stripped.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        candidates.append(stripped[first_brace : last_brace + 1].strip())

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    raise ReviewResultValidationError("Claude response was not parseable JSON object")


def _expect_exact_keys(raw_dict: Dict[str, Any], expected: Set[str], *, context: str) -> None:
    actual = set(raw_dict.keys())
    if actual != expected:
        missing = sorted(expected.difference(actual))
        extra = sorted(actual.difference(expected))
        raise ReviewResultValidationError(
            f"{context} keys mismatch (missing={missing}, extra={extra})"
        )


def _prepare_ai_payload(
    *,
    diff: NotebookDiff,
    base_reviewer_guidance: Sequence[ReviewerGuidanceItem],
    redact_secrets: bool,
    redact_emails: bool,
    max_ai_input_tokens: int,
) -> Dict[str, Any]:
    payload = notebook_diff_to_dict(diff)
    payload["base_reviewer_guidance"] = [
        _reviewer_guidance_to_dict(item) for item in base_reviewer_guidance
    ]
    payload = _redact_json_value(payload, redact_secrets=redact_secrets, redact_emails=redact_emails)
    if _estimate_tokens(payload) <= max_ai_input_tokens:
        return payload
    return _truncate_payload_for_token_budget(payload, max_ai_input_tokens=max_ai_input_tokens)


def _redact_json_value(
    value: Any,
    *,
    redact_secrets: bool,
    redact_emails: bool,
) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _redact_json_value(
                val,
                redact_secrets=redact_secrets,
                redact_emails=redact_emails,
            )
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [
            _redact_json_value(item, redact_secrets=redact_secrets, redact_emails=redact_emails)
            for item in value
        ]
    if isinstance(value, str):
        return _redact_text(value, redact_secrets=redact_secrets, redact_emails=redact_emails)
    return value


def _redact_text(text: str, *, redact_secrets: bool, redact_emails: bool) -> str:
    redacted = text
    if redact_secrets:
        redacted = _URI_CREDENTIALS_RE.sub(
            r"\1<REDACTED_USER>:<REDACTED_SECRET>@",
            redacted,
        )
        redacted = _CONNECTION_STRING_RE.sub("<REDACTED_CONNECTION_STRING>", redacted)
        redacted = _SENSITIVE_ASSIGNMENT_RE.sub(r"\1=<REDACTED_SECRET>", redacted)
        redacted = _LONG_BASE64_RE.sub("<REDACTED_BASE64_BLOB>", redacted)
    if redact_emails:
        redacted = _EMAIL_RE.sub("<REDACTED_EMAIL>", redacted)
    return redacted


def _truncate_payload_for_token_budget(
    payload: Dict[str, Any],
    *,
    max_ai_input_tokens: int,
) -> Dict[str, Any]:
    trimmed = {
        "notebooks": [],
        "total_notebooks_changed": 0,
        "total_cells_changed": 0,
        "base_reviewer_guidance": list(payload.get("base_reviewer_guidance", [])),
        "notices": list(payload.get("notices", [])),
    }
    exhausted = False

    for notebook in payload.get("notebooks", []):
        candidate_notebook = {
            "path": notebook.get("path"),
            "change_type": notebook.get("change_type"),
            "cell_changes": [],
            "notices": list(notebook.get("notices", [])),
        }
        trimmed["notebooks"].append(candidate_notebook)

        for raw_cell in notebook.get("cell_changes", []):
            cell = copy.deepcopy(raw_cell)
            candidate_notebook["cell_changes"].append(cell)
            if _estimate_tokens(trimmed) <= max_ai_input_tokens:
                continue

            candidate_notebook["cell_changes"].pop()
            compact = _compact_cell_for_budget(cell)
            candidate_notebook["cell_changes"].append(compact)
            if _estimate_tokens(trimmed) <= max_ai_input_tokens:
                candidate_notebook["notices"].append(
                    "AI payload compacted a cell summary to stay within token budget."
                )
                continue

            candidate_notebook["cell_changes"].pop()
            exhausted = True
            break

        if exhausted:
            break

    trimmed["total_notebooks_changed"] = len(trimmed["notebooks"])
    trimmed["total_cells_changed"] = sum(
        len(notebook.get("cell_changes", [])) for notebook in trimmed["notebooks"]
    )
    if exhausted:
        trimmed["notices"].append(
            (
                "AI payload truncated to fit 16000-token budget; "
                "processed deterministic notebook/cell subset."
            )
        )
    return trimmed


def _compact_cell_for_budget(cell: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "locator": cell.get("locator"),
        "cell_type": cell.get("cell_type"),
        "change_type": cell.get("change_type"),
        "summary": cell.get("summary"),
        "source_changed": cell.get("source_changed"),
        "outputs_changed": cell.get("outputs_changed"),
        "material_metadata_changed": cell.get("material_metadata_changed"),
        "output_changes": [],
        "review_context": [],
    }


def _estimate_tokens(payload: Dict[str, Any]) -> int:
    # Standard approximation for LLM token budgeting used in request shaping.
    serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return max(1, len(serialized) // 4)


def _build_claude_prompt(redacted_payload: Dict[str, Any]) -> str:
    schema = {
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
    }
    payload_json = json.dumps(redacted_payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    schema_json = json.dumps(schema, ensure_ascii=True, separators=(",", ":"), sort_keys=True)

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
        "- reviewer_guidance must contain only NEW Claude-added guidance items.\n"
        "- Do not repeat, remove, or rewrite any base_reviewer_guidance items.\n"
        "- Every reviewer_guidance item must use source=claude and code values starting with claude:.\n"
        "- summary may be null when no extra AI summary is useful.\n"
        "Diff payload:\n"
        f"{payload_json}"
    )


def _build_repair_prompt(previous_response: str, reason: str) -> str:
    prior = previous_response.strip()
    if len(prior) > 4_000:
        prior = f"{prior[:4000]}...(truncated)"
    return (
        "Your previous response was invalid for strict JSON parsing.\n"
        f"Validation error: {reason}\n"
        "Return only corrected JSON with keys exactly: summary, flagged_issues, reviewer_guidance.\n"
        "Do not include markdown, fences, explanations, or any extra keys.\n"
        "Previous response:\n"
        f"{prior}"
    )


def _extract_anthropic_text(payload: Dict[str, Any]) -> str:
    blocks = payload.get("content")
    if not isinstance(blocks, list):
        raise ClaudeRequestError("Claude response missing content array", retryable=False)

    parts: List[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)

    merged = "\n".join(parts).strip()
    if not merged:
        raise ClaudeRequestError("Claude response had no text blocks", retryable=False)
    return merged


def _read_int(value: Any) -> int:
    if isinstance(value, int) and value >= 0:
        return value
    return 0


def _stable_reason(exc: BaseException) -> str:
    message = str(exc).strip()
    if not message:
        return exc.__class__.__name__
    return message[:300]


def _deterministic_findings(diff: NotebookDiff) -> List[FlaggedIssue]:
    issues: List[FlaggedIssue] = []
    seen: Set[Tuple[str, Optional[int], str]] = set()

    for notebook in diff.notebooks:
        for notice in notebook.notices:
            if "notebook material metadata changed" not in notice:
                continue
            key = (notebook.path, None, "notebook_material_metadata_changed")
            if key in seen:
                continue
            seen.add(key)
            issues.append(
                FlaggedIssue(
                    notebook_path=notebook.path,
                    locator=CellLocator(
                        cell_id=None,
                        base_index=None,
                        head_index=None,
                        display_index=None,
                    ),
                    code="notebook_material_metadata_changed",
                    category="metadata",
                    severity="low",
                    confidence="high",
                    message=(
                        "Notebook material metadata changed (kernelspec/language_info). "
                        "Confirm environment compatibility is expected."
                    ),
                )
            )

        for cell in notebook.cell_changes:
            cell_key = cell.locator.display_index

            if cell.material_metadata_changed:
                key = (notebook.path, cell_key, "cell_material_metadata_changed")
                if key not in seen:
                    seen.add(key)
                    issues.append(
                        FlaggedIssue(
                            notebook_path=notebook.path,
                            locator=cell.locator,
                            code="cell_material_metadata_changed",
                            category="metadata",
                            severity="low",
                            confidence="high",
                            message=(
                                "Cell review-relevant metadata changed (for example tags). "
                                "Confirm downstream behavior is still expected."
                            ),
                        )
                    )

            if any(output.output_type == "error" for output in cell.output_changes):
                key = (notebook.path, cell_key, "error_output_present")
                if key not in seen:
                    seen.add(key)
                    issues.append(
                        FlaggedIssue(
                            notebook_path=notebook.path,
                            locator=cell.locator,
                            code="error_output_present",
                            category="error",
                            severity="medium",
                            confidence="high",
                            message=(
                                "Changed cell includes an error output. "
                                "Verify the failing state is intentional."
                            ),
                        )
                    )

            if any(output.truncated for output in cell.output_changes):
                key = (notebook.path, cell_key, "large_output_change")
                if key not in seen:
                    seen.add(key)
                    issues.append(
                        FlaggedIssue(
                            notebook_path=notebook.path,
                            locator=cell.locator,
                            code="large_output_change",
                            category="output",
                            severity="low",
                            confidence="high",
                            message=(
                                "Changed cell includes large output content "
                                "that was truncated for AI review budget."
                            ),
                        )
                    )

    return issues


def build_base_reviewer_guidance(
    diff: NotebookDiff,
    reviewer_playbooks: Sequence[Any] = (),
) -> List[ReviewerGuidanceItem]:
    """Build deterministic and playbook guidance before provider invocation."""
    guidance = _deterministic_reviewer_guidance(diff)
    guidance.extend(_playbook_reviewer_guidance(diff, reviewer_playbooks))
    return _merge_reviewer_guidance([], guidance)


def _deterministic_reviewer_guidance(diff: NotebookDiff) -> List[ReviewerGuidanceItem]:
    guidance: List[ReviewerGuidanceItem] = []
    for notebook in diff.notebooks:
        if notebook.change_type == "deleted":
            continue
        guidance.extend(_guidance_for_notebook(notebook))
    return guidance


def _guidance_for_notebook(notebook: NotebookFileDiff) -> List[ReviewerGuidanceItem]:
    guidance: List[ReviewerGuidanceItem] = []
    if _notebook_has_introduced_error_outputs(notebook):
        guidance.append(
            ReviewerGuidanceItem(
                notebook_path=notebook.path,
                locator=None,
                code="built_in:introduced_error_outputs",
                source="built_in",
                label=None,
                priority="high",
                message=(
                    "Review introduced error outputs and confirm the failing state is intentional "
                    "or resolved before merge."
                ),
            )
        )
    if _notebook_has_material_output_changes(notebook):
        guidance.append(
            ReviewerGuidanceItem(
                notebook_path=notebook.path,
                locator=None,
                code="built_in:material_output_changes",
                source="built_in",
                label=None,
                priority="medium",
                message=(
                    "Inspect changed outputs for unexplained metric shifts, stale rendered "
                    "results, or outputs that now need updated narrative."
                ),
            )
        )
    if _notebook_has_review_metadata_changes(notebook):
        guidance.append(
            ReviewerGuidanceItem(
                notebook_path=notebook.path,
                locator=None,
                code="built_in:review_metadata_changes",
                source="built_in",
                label=None,
                priority="medium",
                message=(
                    "Check notebook and cell metadata changes to confirm tags, kernelspec, and "
                    "environment expectations still match downstream workflows."
                ),
            )
        )
    if _notebook_has_flow_sensitive_code_changes(notebook):
        guidance.append(
            ReviewerGuidanceItem(
                notebook_path=notebook.path,
                locator=None,
                code="built_in:execution_flow_changes",
                source="built_in",
                label=None,
                priority="medium",
                message=(
                    "Review moved or deleted code cells for execution-order changes and broken "
                    "references in the notebook narrative."
                ),
            )
        )
    return guidance


def _playbook_reviewer_guidance(
    diff: NotebookDiff,
    reviewer_playbooks: Sequence[Any],
) -> List[ReviewerGuidanceItem]:
    guidance: List[ReviewerGuidanceItem] = []
    for notebook in diff.notebooks:
        if notebook.change_type == "deleted":
            continue
        normalized_path = _normalize_posix_path(notebook.path)
        for playbook in reviewer_playbooks:
            if not _playbook_matches_path(playbook, normalized_path):
                continue
            playbook_name = str(getattr(playbook, "name", "")).strip()
            prompts = tuple(getattr(playbook, "prompts", ()) or ())
            for prompt in prompts:
                prompt_text = str(prompt).strip()
                if not prompt_text:
                    continue
                guidance.append(
                    ReviewerGuidanceItem(
                        notebook_path=notebook.path,
                        locator=None,
                        code=f"playbook:{_slugify_code_fragment(playbook_name)}",
                        source="playbook",
                        label=playbook_name or None,
                        priority="medium",
                        message=prompt_text,
                    )
                )
    return guidance


def _playbook_matches_path(playbook: Any, notebook_path: str) -> bool:
    patterns = tuple(getattr(playbook, "paths", ()) or ())
    for raw_pattern in patterns:
        pattern = _normalize_posix_path(str(raw_pattern).strip())
        for candidate in _playbook_match_patterns(pattern):
            if fnmatchcase(notebook_path, candidate):
                return True
            if PurePosixPath(notebook_path).match(candidate):
                return True
    return False


def _normalize_posix_path(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def _playbook_match_patterns(pattern: str) -> Tuple[str, ...]:
    if not pattern:
        return ()
    candidates = [pattern]
    if "/**/" in pattern:
        candidates.append(pattern.replace("/**/", "/", 1))
    return tuple(dict.fromkeys(candidates))


def _slugify_code_fragment(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "custom"


def _notebook_has_introduced_error_outputs(notebook: NotebookFileDiff) -> bool:
    for cell in notebook.cell_changes:
        if cell.change_type == "deleted":
            continue
        if any(output.output_type == "error" for output in cell.output_changes):
            return True
    return False


def _notebook_has_material_output_changes(notebook: NotebookFileDiff) -> bool:
    for cell in notebook.cell_changes:
        if cell.change_type == "deleted":
            continue
        if not (cell.outputs_changed or cell.change_type == "output_changed"):
            continue
        if any(output.output_type == "error" for output in cell.output_changes):
            continue
        return True
    return False


def _notebook_has_review_metadata_changes(notebook: NotebookFileDiff) -> bool:
    if any("notebook material metadata changed" in notice for notice in notebook.notices):
        return True
    return any(cell.material_metadata_changed for cell in notebook.cell_changes)


def _notebook_has_flow_sensitive_code_changes(notebook: NotebookFileDiff) -> bool:
    return any(
        cell.cell_type == "code" and cell.change_type in {"moved", "deleted"}
        for cell in notebook.cell_changes
    )


def _merge_reviewer_guidance(
    base_guidance: Sequence[ReviewerGuidanceItem],
    extra_guidance: Sequence[ReviewerGuidanceItem],
) -> List[ReviewerGuidanceItem]:
    combined = list(base_guidance) + list(extra_guidance)
    deduped: List[Tuple[int, ReviewerGuidanceItem]] = []
    seen: Set[Tuple[str, str]] = set()
    for index, item in enumerate(combined):
        key = (item.notebook_path, _normalize_guidance_message(item.message))
        if key in seen:
            continue
        seen.add(key)
        deduped.append((index, item))
    deduped.sort(key=lambda pair: _reviewer_guidance_sort_key(pair[1], pair[0]))
    return [item for _, item in deduped]


def _normalize_guidance_message(message: str) -> str:
    return re.sub(r"\s+", " ", message).strip().lower()


def _reviewer_guidance_sort_key(
    item: ReviewerGuidanceItem,
    insertion_index: int,
) -> Tuple[int, int, int]:
    priority_order = {"high": 0, "medium": 1, "low": 2}
    source_order = {"built_in": 0, "playbook": 1, "claude": 2}
    return (
        priority_order.get(item.priority, 99),
        source_order.get(item.source, 99),
        insertion_index,
    )


def _reviewer_guidance_to_dict(item: ReviewerGuidanceItem) -> Dict[str, Any]:
    locator = None
    if item.locator is not None:
        locator = {
            "cell_id": item.locator.cell_id,
            "base_index": item.locator.base_index,
            "head_index": item.locator.head_index,
            "display_index": item.locator.display_index,
        }
    return {
        "notebook_path": item.notebook_path,
        "locator": locator,
        "code": item.code,
        "source": item.source,
        "label": item.label,
        "priority": item.priority,
        "message": item.message,
    }


__all__ = [
    "ClaudeProvider",
    "ProviderConfig",
    "ProviderInterface",
    "ProviderRunMetadata",
    "NoneProvider",
    "build_base_reviewer_guidance",
    "build_provider",
    "parse_strict_review_result",
]
