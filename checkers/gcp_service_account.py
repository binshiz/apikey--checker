"""Validate downloaded Google Cloud service-account JSON credentials."""
from __future__ import annotations

import asyncio
from datetime import timezone
from urllib.parse import quote

import requests
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account

from detector import parse_gcp_service_account


GOOGLE_OAUTH_TOKEN_URI = "https://oauth2.googleapis.com/token"
GOOGLE_CLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
TOKEN_REQUEST_TIMEOUT_SECONDS = 20
VERTEX_REQUEST_TIMEOUT_SECONDS = 12
VERTEX_API_BASE = "https://aiplatform.googleapis.com/v1"
VERTEX_LOCATION = "global"
VERTEX_GEMINI_MODEL_IDS = (
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.1-pro-preview",
    "gemini-2.5-pro",
)
# Backward-compatible alias for callers and persisted test fixtures that use the
# original generic name. These models are all Google-published Gemini models.
VERTEX_MODEL_IDS = VERTEX_GEMINI_MODEL_IDS
VERTEX_CLAUDE_MODEL_IDS = (
    "claude-sonnet-5",
    "claude-fable-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-opus-4-5",
    "claude-opus-4-1",
    "claude-opus-4",
    "claude-sonnet-4-5",
    "claude-sonnet-4",
    "claude-haiku-4-5",
    "claude-3-5-haiku",
)
VERTEX_PROBE_BODY = {
    "contents": [{
        "role": "user",
        "parts": [{"text": "Reply OK."}],
    }],
    "generationConfig": {"maxOutputTokens": 8},
}
VERTEX_CLAUDE_COUNT_TOKENS_MESSAGES = [{
    "role": "user",
    "content": "permission check",
}]


class _BoundedGoogleAuthRequest(GoogleAuthRequest):
    """Force a short bound even when google-auth omits a request timeout."""

    def __call__(self, *args, **kwargs):
        requested = kwargs.get("timeout")
        try:
            requested = float(requested)
        except (TypeError, ValueError):
            requested = TOKEN_REQUEST_TIMEOUT_SECONDS
        kwargs["timeout"] = min(requested, TOKEN_REQUEST_TIMEOUT_SECONDS)
        return super().__call__(*args, **kwargs)


def _expiry_timestamp(credentials) -> str | None:
    expiry = credentials.expiry
    if expiry is None:
        return None
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _vertex_model_url(project_id: str, model_id: str) -> str:
    project = quote(project_id, safe="")
    model = quote(model_id, safe="")
    return (
        f"{VERTEX_API_BASE}/projects/{project}/locations/{VERTEX_LOCATION}"
        f"/publishers/google/models/{model}:generateContent"
    )


def _vertex_claude_count_tokens_url(project_id: str) -> str:
    project = quote(project_id, safe="")
    return (
        f"{VERTEX_API_BASE}/projects/{project}/locations/{VERTEX_LOCATION}"
        "/publishers/anthropic/models/count-tokens:rawPredict"
    )


def _vertex_probe_status(http_status: int) -> str:
    if 200 <= http_status < 300:
        return "callable"
    return {
        400: "request_rejected",
        401: "authentication_failed",
        403: "permission_denied",
        404: "not_found",
        429: "rate_limited",
    }.get(http_status, "upstream_error" if http_status >= 500 else "http_error")


def _probe_vertex_models(
    session: requests.Session,
    access_token: str,
    project_id: str,
) -> dict:
    """Invoke each target model and retain only status metadata, never content."""
    results: list[dict[str, str | int]] = []
    for model_id in VERTEX_MODEL_IDS:
        try:
            response = session.post(
                _vertex_model_url(project_id, model_id),
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json=VERTEX_PROBE_BODY,
                timeout=VERTEX_REQUEST_TIMEOUT_SECONDS,
            )
            try:
                http_status = int(response.status_code)
                results.append({
                    "model": model_id,
                    "status": _vertex_probe_status(http_status),
                    "http_status": http_status,
                })
            finally:
                try:
                    response.close()
                except Exception:
                    pass
        except (requests.RequestException, TimeoutError, OSError):
            results.append({"model": model_id, "status": "network_error"})
            # Every probe uses the same Vertex endpoint. Avoid repeating a likely
            # proxy/network timeout for the remaining models.
            break
        except Exception:
            results.append({"model": model_id, "status": "error"})

    supported_models = [
        item["model"] for item in results if item["status"] == "callable"
    ]
    return {
        "vertex_location": VERTEX_LOCATION,
        "vertex_probe_method": "generateContent",
        "model_probe_results": results,
        "models_checked": len(results),
        "supported_models": supported_models,
        "supported_model_count": len(supported_models),
        "model_invocation_verification": (
            "success" if supported_models else "none"
        ),
    }


def _probe_vertex_claude_models(
    session: requests.Session,
    access_token: str,
    project_id: str,
) -> dict:
    """Check Claude model access through the no-charge count-tokens endpoint."""
    results: list[dict[str, str | int]] = []
    url = _vertex_claude_count_tokens_url(project_id)
    for model_id in VERTEX_CLAUDE_MODEL_IDS:
        try:
            response = session.post(
                url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model_id,
                    "messages": VERTEX_CLAUDE_COUNT_TOKENS_MESSAGES,
                },
                timeout=VERTEX_REQUEST_TIMEOUT_SECONDS,
            )
            try:
                http_status = int(response.status_code)
                results.append({
                    "model": model_id,
                    "status": (
                        "permission_granted"
                        if 200 <= http_status < 300
                        else _vertex_probe_status(http_status)
                    ),
                    "http_status": http_status,
                })
            finally:
                try:
                    response.close()
                except Exception:
                    pass
        except (requests.RequestException, TimeoutError, OSError):
            results.append({"model": model_id, "status": "network_error"})
            break
        except Exception:
            results.append({"model": model_id, "status": "error"})

    supported_models = [
        item["model"]
        for item in results
        if item["status"] == "permission_granted"
    ]
    return {
        "claude_probe_method": "countTokens",
        "claude_model_probe_results": results,
        "claude_models_checked": len(results),
        "claude_supported_models": supported_models,
        "claude_supported_model_count": len(supported_models),
        "claude_permission_verification": (
            "success" if supported_models else "none"
        ),
    }


def _refresh_credentials(info: dict, proxy: str | None) -> tuple[str | None, dict]:
    """Refresh once, invoke target models, and return non-secret metadata only."""
    safe_info = dict(info)
    # Never trust a caller-controlled URL from a credential document.
    safe_info["token_uri"] = GOOGLE_OAUTH_TOKEN_URI
    credentials = service_account.Credentials.from_service_account_info(
        safe_info,
        scopes=(GOOGLE_CLOUD_SCOPE,),
    )
    session = requests.Session()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    try:
        credentials.refresh(_BoundedGoogleAuthRequest(session=session))
        if not credentials.token:
            raise RefreshError("token endpoint returned no access token")
        model_probe = _probe_vertex_models(
            session,
            credentials.token,
            info["project_id"],
        )
        model_probe.update(_probe_vertex_claude_models(
            session,
            credentials.token,
            info["project_id"],
        ))
        return _expiry_timestamp(credentials), model_probe
    finally:
        session.close()


def _result_extra(info: dict, expiry: str | None = None) -> dict:
    extra = {
        "credential_type": "gcp_service_account",
        "project_id": info["project_id"],
        "client_email": info["client_email"],
        "private_key_id_suffix": info["private_key_id"][-8:],
    }
    if expiry:
        extra["token_expiry"] = expiry
    return extra


async def check(key: str, proxy: str | None = None) -> dict:
    """Validate a service-account private key with one OAuth token exchange."""
    info = parse_gcp_service_account(key)
    if info is None:
        return {
            "status": "invalid",
            "tier": None,
            "rpm": None,
            "tpm": None,
            "error": "Invalid GCP service account JSON",
            "extra": {"credential_type": "gcp_service_account"},
        }

    extra = _result_extra(info)
    if proxy:
        extra["proxy_used"] = True
    try:
        expiry, model_probe = await asyncio.to_thread(
            _refresh_credentials,
            info,
            proxy,
        )
    except RefreshError as exc:
        if exc.retryable:
            return {
                "status": "error",
                "tier": None,
                "rpm": None,
                "tpm": None,
                "error": "Google OAuth token service is temporarily unavailable",
                "extra": extra,
            }
        return {
            "status": "invalid",
            "tier": None,
            "rpm": None,
            "tpm": None,
            "error": "Google rejected the service account credential",
            "extra": extra,
        }
    except (TransportError, requests.RequestException, TimeoutError, OSError):
        return {
            "status": "error",
            "tier": None,
            "rpm": None,
            "tpm": None,
            "error": "Google OAuth token request failed",
            "extra": extra,
        }
    except (TypeError, ValueError):
        return {
            "status": "invalid",
            "tier": None,
            "rpm": None,
            "tpm": None,
            "error": "Invalid GCP service account private key",
            "extra": extra,
        }
    except Exception:
        return {
            "status": "error",
            "tier": None,
            "rpm": None,
            "tpm": None,
            "error": "Unexpected GCP credential validation failure",
            "extra": extra,
        }

    extra.update({
        "token_exchange": "success",
        "token_expiry": expiry,
        **model_probe,
    })
    if expiry is None:
        extra.pop("token_expiry")
    return {
        "status": "valid",
        "tier": None,
        "rpm": None,
        "tpm": None,
        "error": None,
        "extra": extra,
    }
