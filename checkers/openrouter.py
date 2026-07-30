"""OpenRouter metadata, Claude Opus/Fable 5 probes, and free fallback proof."""

from __future__ import annotations

import math
from typing import Any

import httpx

from detector import parse_openrouter_key


BASE_URL = "https://openrouter.ai"
KEY_INFO_PATH = "/api/v1/key"
CREDITS_PATH = "/api/v1/credits"
CHAT_COMPLETIONS_PATH = "/api/v1/chat/completions"
PROBE_MODEL = "openrouter/free"
FABLE5_MODEL = "anthropic/claude-fable-5"
OPUS5_MODEL = "anthropic/claude-opus-5"


def _result() -> dict[str, Any]:
    return {
        "status": "error",
        "tier": None,
        "rpm": None,
        "tpm": None,
        "error": None,
        "extra": {},
    }


def _safe_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return value


def _safe_text(value: Any, *, limit: int = 160) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned[:limit] if cleaned else None


def _error_message(response: httpx.Response, api_key: str) -> str:
    try:
        body = response.json()
    except (TypeError, ValueError):
        body = {}

    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        value = error.get("message") or error.get("code") or error.get("type")
    elif error:
        value = error
    elif isinstance(body, dict):
        value = body.get("message")
    else:
        value = None

    cleaned = str(value or "").replace(api_key, "[redacted]").strip()[:200]
    return cleaned or f"HTTP {response.status_code}"


def _error_code(response: httpx.Response, api_key: str) -> str | None:
    try:
        body = response.json()
    except (TypeError, ValueError):
        return None
    if not isinstance(body, dict) or not isinstance(body.get("error"), dict):
        return None
    value = body["error"].get("code") or body["error"].get("type")
    cleaned = _safe_text(value, limit=100)
    return cleaned.replace(api_key, "[redacted]") if cleaned else None


def _key_metadata(data: dict[str, Any]) -> dict[str, Any]:
    is_free_tier = data.get("is_free_tier") is True
    limit = _safe_number(data.get("limit"))
    limit_remaining = _safe_number(data.get("limit_remaining"))
    extra: dict[str, Any] = {
        "credential_status": "valid",
        "validation_method": "current_key+opus5+fable5_or_free_chat_completion",
        "account_type": "free" if is_free_tier else "paid",
        "is_free_tier": is_free_tier,
        "is_management_key": data.get("is_management_key") is True,
        "is_provisioning_key": data.get("is_provisioning_key") is True,
        "include_byok_in_limit": data.get("include_byok_in_limit") is True,
        "key_limit_configured": limit is not None,
    }
    if limit is not None:
        extra["limit"] = limit
    if limit_remaining is not None:
        extra["limit_remaining"] = limit_remaining

    for field in (
        "usage",
        "usage_daily",
        "usage_weekly",
        "usage_monthly",
        "byok_usage",
        "byok_usage_daily",
        "byok_usage_weekly",
        "byok_usage_monthly",
    ):
        value = _safe_number(data.get(field))
        if value is not None:
            extra[field] = value

    limit_reset = _safe_text(data.get("limit_reset"), limit=32)
    if limit_reset:
        extra["limit_reset"] = limit_reset
    expires_at = _safe_text(data.get("expires_at"), limit=64)
    if expires_at:
        extra["expires_at"] = expires_at
    return extra


def _response_model(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except (TypeError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    return _safe_text(body.get("model"), limit=160)


def _token_usage(response: httpx.Response) -> dict[str, int | float]:
    try:
        body = response.json()
    except (TypeError, ValueError):
        return {}
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return {}

    safe_usage = {}
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = _safe_number(usage.get(field))
        if value is not None:
            safe_usage[field] = value
    return safe_usage


async def _probe_target_model(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    api_key: str,
    model: str,
) -> dict[str, Any]:
    """Prove one paid target model with a minimal request."""
    probe: dict[str, Any] = {"model": model}
    request_headers = dict(headers)
    request_headers["Content-Type"] = "application/json"
    try:
        response = await client.post(
            f"{BASE_URL}{CHAT_COMPLETIONS_PATH}",
            headers=request_headers,
            json={
                "model": model,
                "messages": [{"role": "user", "content": "."}],
                "max_tokens": 1,
            },
        )
    except httpx.TimeoutException:
        probe["status"] = "timeout"
        probe["error"] = "timeout"
        return probe
    except httpx.RequestError as exc:
        probe["status"] = "network_error"
        probe["error"] = type(exc).__name__
        return probe

    probe["http_status"] = response.status_code
    if response.status_code == 200:
        probe["status"] = "callable"
        resolved_model = _response_model(response)
        if resolved_model:
            probe["resolved_model"] = resolved_model
        token_usage = _token_usage(response)
        if token_usage:
            probe["token_usage"] = token_usage
        return probe

    probe["status"] = {
        401: "authentication_failed",
        402: "no_quota",
        403: "access_denied",
        404: "model_unavailable",
        429: "rate_limited",
    }.get(
        response.status_code,
        "upstream_error" if response.status_code >= 500 else "request_rejected",
    )
    probe["error"] = _error_message(response, api_key)
    error_code = _error_code(response, api_key)
    if error_code:
        probe["error_code"] = error_code
    return probe


async def _probe_opus5(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    api_key: str,
) -> dict[str, Any]:
    return await _probe_target_model(client, headers, api_key, OPUS5_MODEL)


async def _probe_fable5(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    api_key: str,
) -> dict[str, Any]:
    return await _probe_target_model(client, headers, api_key, FABLE5_MODEL)


async def _load_account_credits(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    api_key: str,
    extra: dict[str, Any],
) -> None:
    """Attach account-level purchased, used, and remaining credit totals."""
    try:
        response = await client.get(
            f"{BASE_URL}{CREDITS_PATH}",
            headers=headers,
        )
    except httpx.TimeoutException:
        extra["credits_status"] = "timeout"
        extra["credits_error"] = "timeout"
        return
    except httpx.RequestError as exc:
        extra["credits_status"] = "network_error"
        extra["credits_error"] = type(exc).__name__
        return

    if response.status_code != 200:
        extra["credits_http_status"] = response.status_code
        extra["credits_status"] = {
            401: "unauthorized",
            403: "forbidden",
            429: "rate_limited",
        }.get(response.status_code, "failed")
        extra["credits_error"] = _error_message(response, api_key)
        error_code = _error_code(response, api_key)
        if error_code:
            extra["credits_error_code"] = error_code
        return

    try:
        body = response.json()
    except (TypeError, ValueError):
        body = None
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        extra["credits_status"] = "invalid_response"
        extra["credits_error"] = "invalid credits response"
        return

    total_credits = _safe_number(data.get("total_credits"))
    total_usage = _safe_number(data.get("total_usage"))
    if total_credits is None or total_usage is None:
        extra["credits_status"] = "invalid_response"
        extra["credits_error"] = "credits response is missing totals"
        return

    extra["credits_status"] = "success"
    extra["account_total_credits"] = total_credits
    extra["account_total_usage"] = total_usage
    extra["account_balance"] = round(total_credits - total_usage, 8)


def _apply_http_failure(
    result: dict[str, Any],
    response: httpx.Response,
    api_key: str,
    *,
    stage: str,
) -> dict[str, Any]:
    status_code = response.status_code
    result["extra"]["http_status"] = status_code
    result["extra"]["failure_stage"] = stage
    error_code = _error_code(response, api_key)
    if error_code:
        result["extra"]["error_code"] = error_code
    result["error"] = _error_message(response, api_key)

    if status_code == 401:
        result["status"] = "invalid"
        result["extra"]["credential_status"] = "invalid"
        if stage == "runtime_probe":
            result["extra"]["invocation_verification"] = "authentication_failed"
    elif status_code in (402, 429):
        result["status"] = "no_quota"
        if stage == "runtime_probe":
            result["extra"]["invocation_verification"] = "quota_limited"
    else:
        result["status"] = "error"
        if stage == "runtime_probe":
            result["extra"]["invocation_verification"] = (
                "access_denied" if status_code == 403 else "failed"
            )
    return result


async def _validate_with_client(client: httpx.AsyncClient, key: str) -> dict[str, Any]:
    """Validate one OpenRouter inference key and prove a model invocation."""
    result = _result()
    api_key = parse_openrouter_key(key)
    if api_key is None:
        result["status"] = "invalid"
        result["error"] = "invalid OpenRouter key format"
        return result

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    try:
        key_response = await client.get(
            f"{BASE_URL}{KEY_INFO_PATH}",
            headers=headers,
        )
    except httpx.TimeoutException:
        result["error"] = "timeout"
        result["extra"] = {"failure_stage": "key_info"}
        return result
    except httpx.RequestError as exc:
        result["error"] = type(exc).__name__
        result["extra"] = {"failure_stage": "key_info"}
        return result

    if key_response.status_code != 200:
        return _apply_http_failure(
            result,
            key_response,
            api_key,
            stage="key_info",
        )

    try:
        key_body = key_response.json()
    except (TypeError, ValueError):
        key_body = None
    key_data = key_body.get("data") if isinstance(key_body, dict) else None
    if not isinstance(key_data, dict):
        result["error"] = "invalid key-info response"
        result["extra"] = {"failure_stage": "key_info"}
        return result

    result["extra"] = _key_metadata(key_data)
    if not result["extra"]["is_provisioning_key"]:
        await _load_account_credits(client, headers, api_key, result["extra"])

    if result["extra"]["is_management_key"]:
        result["extra"]["validation_method"] = "current_key+account_credits"
        result["extra"]["invocation_verification"] = "not_applicable"
        result["error"] = "management key is not an inference API key"
        return result
    if result["extra"]["is_provisioning_key"]:
        result["extra"]["validation_method"] = "current_key_only"
        result["error"] = "provisioning key is not an inference API key"
        result["extra"]["invocation_verification"] = "not_applicable"
        return result

    result["extra"]["validation_method"] = (
        "current_key+account_credits+opus5+fable5_or_free_chat_completion"
    )
    opus5_probe = await _probe_opus5(client, headers, api_key)
    result["extra"]["opus5_probe"] = opus5_probe
    result["extra"]["has_opus_5"] = opus5_probe["status"] == "callable"
    if opus5_probe["status"] == "authentication_failed":
        result["status"] = "invalid"
        result["error"] = opus5_probe.get("error") or "invalid/revoked"
        result["extra"]["credential_status"] = "invalid"
        result["extra"]["failure_stage"] = "opus5_probe"
        result["extra"]["invocation_verification"] = "authentication_failed"
        return result

    fable5_probe = await _probe_fable5(client, headers, api_key)
    result["extra"]["fable5_probe"] = fable5_probe
    result["extra"]["has_fable_5"] = fable5_probe["status"] == "callable"
    if fable5_probe["status"] == "authentication_failed":
        result["status"] = "invalid"
        result["error"] = fable5_probe.get("error") or "invalid/revoked"
        result["extra"]["credential_status"] = "invalid"
        result["extra"]["failure_stage"] = "fable5_probe"
        result["extra"]["invocation_verification"] = "authentication_failed"
        return result

    successful_paid_probe = next(
        (
            probe
            for probe in (opus5_probe, fable5_probe)
            if probe["status"] == "callable"
        ),
        None,
    )
    if successful_paid_probe is not None:
        result["status"] = "valid"
        result["tier"] = "Free" if result["extra"]["is_free_tier"] else "Paid"
        result["extra"]["invocation_verification"] = "success"
        result["extra"]["requested_model"] = successful_paid_probe["model"]
        result["extra"]["resolved_model"] = (
            successful_paid_probe.get("resolved_model")
            or successful_paid_probe["model"]
        )
        if successful_paid_probe.get("token_usage"):
            result["extra"]["token_usage"] = successful_paid_probe["token_usage"]
        return result

    result["extra"]["requested_model"] = PROBE_MODEL
    probe_headers = dict(headers)
    probe_headers["Content-Type"] = "application/json"
    try:
        probe_response = await client.post(
            f"{BASE_URL}{CHAT_COMPLETIONS_PATH}",
            headers=probe_headers,
            json={
                "model": PROBE_MODEL,
                "messages": [{"role": "user", "content": "."}],
                "max_tokens": 1,
            },
        )
    except httpx.TimeoutException:
        result["error"] = "timeout"
        result["extra"]["failure_stage"] = "runtime_probe"
        result["extra"]["invocation_verification"] = "timeout"
        return result
    except httpx.RequestError as exc:
        result["error"] = type(exc).__name__
        result["extra"]["failure_stage"] = "runtime_probe"
        result["extra"]["invocation_verification"] = "network_error"
        return result

    if probe_response.status_code != 200:
        return _apply_http_failure(
            result,
            probe_response,
            api_key,
            stage="runtime_probe",
        )

    result["status"] = "valid"
    result["tier"] = "Free" if result["extra"]["is_free_tier"] else "Paid"
    result["extra"]["invocation_verification"] = "success"
    resolved_model = _response_model(probe_response)
    if resolved_model:
        result["extra"]["resolved_model"] = resolved_model
    token_usage = _token_usage(probe_response)
    if token_usage:
        result["extra"]["token_usage"] = token_usage
    return result


async def check(key: str, proxy: str | None = None) -> dict[str, Any]:
    """Check an OpenRouter key using metadata plus minimal runtime probes."""
    client_kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(30.0, connect=10.0),
        "follow_redirects": True,
        "limits": httpx.Limits(max_connections=10),
    }
    if proxy:
        client_kwargs["proxy"] = proxy
    async with httpx.AsyncClient(**client_kwargs) as client:
        return await _validate_with_client(client, key)
