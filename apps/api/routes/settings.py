"""Installation-scoped managed AI gateway settings routes."""

from __future__ import annotations

import json
import uuid
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import ApiSettings, get_settings
from ..database import get_db_session
from ..models import (
    GitHubHostKind,
    GitHubInstallation,
    ManagedAiGatewayConfig,
    ManagedAiGatewayProviderKind,
)
from ..oauth import GitHubOAuthClient, SessionCipherError, SessionTokenCipher
from .auth import AuthenticatedUser, ensure_installation_admin, get_oauth_client, require_authenticated_user


router = APIRouter(prefix="/api/settings", tags=["settings"])


class AiGatewaySettingsRequest(BaseModel):
    provider_kind: ManagedAiGatewayProviderKind = ManagedAiGatewayProviderKind.LITELLM
    display_name: str
    github_host_kind: GitHubHostKind
    github_api_base_url: str
    github_web_base_url: str
    base_url: str
    model_name: str
    api_key: str | None = None
    api_key_header_name: str
    static_headers: dict[str, str] | None = None
    use_responses_api: bool = False
    litellm_virtual_key_id: str | None = None
    active: bool = False


class LiteLLMConnectionError(RuntimeError):
    """Raised when a LiteLLM connection test fails."""


class LiteLLMConnectionTester:
    """Minimal HTTP client for validating a LiteLLM-compatible endpoint."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()

    def test_connection(
        self,
        *,
        base_url: str,
        model_name: str,
        api_key_header_name: str,
        api_key: str,
        static_headers: dict[str, str],
        use_responses_api: bool,
    ) -> str:
        headers = {
            "Accept": "application/json",
            api_key_header_name: api_key,
        }
        headers.update(static_headers)
        if use_responses_api:
            path = "/responses"
            payload = {
                "model": model_name,
                "input": "Reply with ok.",
                "max_output_tokens": 4,
            }
        else:
            path = "/chat/completions"
            payload = {
                "model": model_name,
                "messages": [{"role": "user", "content": "Reply with ok."}],
                "max_tokens": 4,
            }
        response = self.session.post(
            f"{base_url}{path}",
            headers=headers,
            json=payload,
            timeout=30,
        )
        if response.status_code >= 400:
            detail = response.text.strip() or f"status {response.status_code}"
            raise LiteLLMConnectionError(
                f"LiteLLM connection test failed with status {response.status_code}: {detail}"
            )
        return path


def get_litellm_connection_tester() -> LiteLLMConnectionTester:
    return LiteLLMConnectionTester()


def _normalize_origin_url(name: str, value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail=f"{name} must be an absolute http(s) origin")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise HTTPException(
            status_code=400,
            detail=f"{name} must be an origin without a path, query, or fragment",
        )
    return f"{parsed.scheme}://{parsed.netloc}"


def _normalize_absolute_url(name: str, value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail=f"{name} must be an absolute http(s) URL")
    if parsed.params or parsed.query or parsed.fragment:
        raise HTTPException(
            status_code=400,
            detail=f"{name} must not include params, query, or fragment",
        )
    path = parsed.path or ""
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _required_text(name: str, value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise HTTPException(status_code=400, detail=f"{name} is required")
    return normalized


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _normalize_static_headers(headers: dict[str, str] | None) -> dict[str, str]:
    if headers is None:
        return {}
    normalized: dict[str, str] = {}
    for key, value in headers.items():
        clean_key = key.strip()
        clean_value = value.strip()
        if not clean_key:
            raise HTTPException(status_code=400, detail="Static header names must be non-empty")
        if not clean_value:
            raise HTTPException(
                status_code=400,
                detail=f"Static header '{clean_key}' must have a non-empty value",
            )
        normalized[clean_key] = clean_value
    return normalized


def _load_installation(
    *,
    installation_id: uuid.UUID,
    db_session: Session,
) -> GitHubInstallation:
    installation = db_session.get(GitHubInstallation, installation_id)
    if installation is None:
        raise HTTPException(status_code=404, detail="GitHub installation not found")
    return installation


def _load_config(
    *,
    installation_id: uuid.UUID,
    db_session: Session,
) -> ManagedAiGatewayConfig | None:
    return db_session.execute(
        select(ManagedAiGatewayConfig).where(ManagedAiGatewayConfig.installation_id == installation_id)
    ).scalar_one_or_none()


def _decrypt_static_headers(
    *,
    cipher: SessionTokenCipher,
    config: ManagedAiGatewayConfig,
) -> dict[str, str]:
    if not config.static_headers_encrypted_json:
        return {}
    try:
        payload = cipher.decrypt(config.static_headers_encrypted_json)
    except SessionCipherError as exc:
        raise HTTPException(
            status_code=500,
            detail="Stored AI gateway static headers could not be decrypted",
        ) from exc
    try:
        headers = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail="Stored AI gateway static headers are invalid",
        ) from exc
    if not isinstance(headers, dict):
        raise HTTPException(status_code=500, detail="Stored AI gateway static headers are invalid")
    return {str(key): str(value) for key, value in headers.items()}


def _serialize_redacted_config(
    *,
    config: ManagedAiGatewayConfig | None,
    installation_id: uuid.UUID,
    cipher: SessionTokenCipher,
) -> dict[str, object]:
    if config is None:
        return {
            "installation_id": installation_id,
            "provider_kind": ManagedAiGatewayProviderKind.NONE.value,
            "display_name": None,
            "github_host_kind": None,
            "github_api_base_url": None,
            "github_web_base_url": None,
            "base_url": None,
            "model_name": None,
            "api_key_header_name": None,
            "has_api_key": False,
            "static_header_names": [],
            "use_responses_api": False,
            "litellm_virtual_key_id": None,
            "active": False,
            "updated_by_github_user_id": None,
            "updated_at": None,
        }
    return {
        "id": config.id,
        "installation_id": config.installation_id,
        "provider_kind": config.provider_kind.value,
        "display_name": config.display_name,
        "github_host_kind": config.github_host_kind.value,
        "github_api_base_url": config.github_api_base_url,
        "github_web_base_url": config.github_web_base_url,
        "base_url": config.base_url,
        "model_name": config.model_name,
        "api_key_header_name": config.api_key_header_name,
        "has_api_key": True,
        "static_header_names": sorted(_decrypt_static_headers(cipher=cipher, config=config).keys()),
        "use_responses_api": config.use_responses_api,
        "litellm_virtual_key_id": config.litellm_virtual_key_id,
        "active": config.active,
        "updated_by_github_user_id": config.updated_by_github_user_id,
        "updated_at": config.updated_at,
    }


def _resolve_api_key_encrypted(
    *,
    request: AiGatewaySettingsRequest,
    existing_config: ManagedAiGatewayConfig | None,
    cipher: SessionTokenCipher,
) -> str:
    api_key = _optional_text(request.api_key)
    if api_key is not None:
        return cipher.encrypt(api_key)
    if existing_config is not None:
        return existing_config.api_key_encrypted
    raise HTTPException(status_code=400, detail="api_key is required")


def _resolve_plaintext_api_key(
    *,
    request: AiGatewaySettingsRequest,
    existing_config: ManagedAiGatewayConfig | None,
    cipher: SessionTokenCipher,
) -> str:
    api_key = _optional_text(request.api_key)
    if api_key is not None:
        return api_key
    if existing_config is None:
        raise HTTPException(status_code=400, detail="api_key is required")
    try:
        return cipher.decrypt(existing_config.api_key_encrypted)
    except SessionCipherError as exc:
        raise HTTPException(
            status_code=500,
            detail="Stored AI gateway API key could not be decrypted",
        ) from exc


def _resolve_static_headers_encrypted_json(
    *,
    request: AiGatewaySettingsRequest,
    existing_config: ManagedAiGatewayConfig | None,
    cipher: SessionTokenCipher,
) -> str | None:
    if "static_headers" in request.model_fields_set:
        static_headers = _normalize_static_headers(request.static_headers)
        if not static_headers:
            return None
        return cipher.encrypt(json.dumps(static_headers, separators=(",", ":"), sort_keys=True))
    if existing_config is not None:
        return existing_config.static_headers_encrypted_json
    return None


def _resolve_plaintext_static_headers(
    *,
    request: AiGatewaySettingsRequest,
    existing_config: ManagedAiGatewayConfig | None,
    cipher: SessionTokenCipher,
) -> dict[str, str]:
    if "static_headers" in request.model_fields_set:
        return _normalize_static_headers(request.static_headers)
    if existing_config is None:
        return {}
    return _decrypt_static_headers(cipher=cipher, config=existing_config)


def _cipher(encryption_key: str) -> SessionTokenCipher:
    return SessionTokenCipher(encryption_key)


def _normalize_request_fields(request: AiGatewaySettingsRequest) -> dict[str, object]:
    if request.provider_kind != ManagedAiGatewayProviderKind.LITELLM:
        raise HTTPException(
            status_code=400,
            detail="Only the LiteLLM managed gateway is supported",
        )
    return {
        "provider_kind": request.provider_kind,
        "display_name": _required_text("display_name", request.display_name),
        "github_host_kind": request.github_host_kind,
        "github_api_base_url": _normalize_origin_url(
            "github_api_base_url",
            request.github_api_base_url,
        ),
        "github_web_base_url": _normalize_origin_url(
            "github_web_base_url",
            request.github_web_base_url,
        ),
        "base_url": _normalize_absolute_url("base_url", request.base_url),
        "model_name": _required_text("model_name", request.model_name),
        "api_key_header_name": _required_text(
            "api_key_header_name",
            request.api_key_header_name,
        ),
        "use_responses_api": request.use_responses_api,
        "litellm_virtual_key_id": _optional_text(request.litellm_virtual_key_id),
        "active": request.active,
    }


@router.get("/ai-gateway")
def get_ai_gateway_settings(
    installation_id: uuid.UUID = Query(...),
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
    db_session: Session = Depends(get_db_session),
    settings: ApiSettings = Depends(get_settings),
    oauth_client: GitHubOAuthClient = Depends(get_oauth_client),
) -> dict[str, object]:
    installation = _load_installation(installation_id=installation_id, db_session=db_session)
    ensure_installation_admin(
        current_user=current_user,
        installation=installation,
        oauth_client=oauth_client,
    )
    config = _load_config(installation_id=installation_id, db_session=db_session)
    return {
        "config": _serialize_redacted_config(
            config=config,
            installation_id=installation_id,
            cipher=_cipher(settings.encryption_key),
        )
    }


@router.put("/ai-gateway")
def put_ai_gateway_settings(
    request: AiGatewaySettingsRequest,
    installation_id: uuid.UUID = Query(...),
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
    db_session: Session = Depends(get_db_session),
    settings: ApiSettings = Depends(get_settings),
    oauth_client: GitHubOAuthClient = Depends(get_oauth_client),
) -> dict[str, object]:
    installation = _load_installation(installation_id=installation_id, db_session=db_session)
    ensure_installation_admin(
        current_user=current_user,
        installation=installation,
        oauth_client=oauth_client,
    )
    existing_config = _load_config(installation_id=installation_id, db_session=db_session)
    cipher = _cipher(settings.encryption_key)
    normalized = _normalize_request_fields(request)

    if existing_config is None:
        config = ManagedAiGatewayConfig(
            installation_id=installation_id,
            updated_by_github_user_id=current_user.github_user_id,
            api_key_encrypted=_resolve_api_key_encrypted(
                request=request,
                existing_config=None,
                cipher=cipher,
            ),
            static_headers_encrypted_json=_resolve_static_headers_encrypted_json(
                request=request,
                existing_config=None,
                cipher=cipher,
            ),
            **normalized,
        )
        db_session.add(config)
    else:
        config = existing_config
        config.provider_kind = normalized["provider_kind"]
        config.display_name = normalized["display_name"]
        config.github_host_kind = normalized["github_host_kind"]
        config.github_api_base_url = normalized["github_api_base_url"]
        config.github_web_base_url = normalized["github_web_base_url"]
        config.base_url = normalized["base_url"]
        config.model_name = normalized["model_name"]
        config.api_key_header_name = normalized["api_key_header_name"]
        config.use_responses_api = normalized["use_responses_api"]
        config.litellm_virtual_key_id = normalized["litellm_virtual_key_id"]
        config.active = normalized["active"]
        config.updated_by_github_user_id = current_user.github_user_id
        config.api_key_encrypted = _resolve_api_key_encrypted(
            request=request,
            existing_config=existing_config,
            cipher=cipher,
        )
        config.static_headers_encrypted_json = _resolve_static_headers_encrypted_json(
            request=request,
            existing_config=existing_config,
            cipher=cipher,
        )

    db_session.flush()
    db_session.commit()
    return {
        "config": _serialize_redacted_config(
            config=config,
            installation_id=installation_id,
            cipher=cipher,
        )
    }


@router.post("/ai-gateway/test")
def test_ai_gateway_settings(
    request: AiGatewaySettingsRequest,
    installation_id: uuid.UUID = Query(...),
    current_user: AuthenticatedUser = Depends(require_authenticated_user),
    db_session: Session = Depends(get_db_session),
    settings: ApiSettings = Depends(get_settings),
    oauth_client: GitHubOAuthClient = Depends(get_oauth_client),
    tester: LiteLLMConnectionTester = Depends(get_litellm_connection_tester),
) -> dict[str, object]:
    installation = _load_installation(installation_id=installation_id, db_session=db_session)
    ensure_installation_admin(
        current_user=current_user,
        installation=installation,
        oauth_client=oauth_client,
    )
    existing_config = _load_config(installation_id=installation_id, db_session=db_session)
    cipher = _cipher(settings.encryption_key)
    normalized = _normalize_request_fields(request)
    try:
        tested_path = tester.test_connection(
            base_url=normalized["base_url"],
            model_name=normalized["model_name"],
            api_key_header_name=normalized["api_key_header_name"],
            api_key=_resolve_plaintext_api_key(
                request=request,
                existing_config=existing_config,
                cipher=cipher,
            ),
            static_headers=_resolve_plaintext_static_headers(
                request=request,
                existing_config=existing_config,
                cipher=cipher,
            ),
            use_responses_api=normalized["use_responses_api"],
        )
    except LiteLLMConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "ok": True,
        "provider_kind": normalized["provider_kind"].value,
        "model_name": normalized["model_name"],
        "tested_endpoint": tested_path,
    }
