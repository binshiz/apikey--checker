"""Azure OpenAI validation with catalog discovery and minimal runtime proof."""

import re
from typing import Any

import httpx

from detector import azure_openai_chat_completions_url, parse_azure_openai_key
from checkers.openai import build_supported_models


V1_MODELS_PATH = "/openai/v1/models"
V1_CHAT_COMPLETIONS_PATH = "/openai/v1/chat/completions"
GA_API_VERSION = "2024-10-21"
GA_MODELS_PATH = f"/openai/models?api-version={GA_API_VERSION}"
MODEL_DATE_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")

TARGET_MODEL_PROBES = (
    {
        "label": "gpt-5.5",
        "catalog_prefixes": ("gpt-5.5",),
        "deployment_candidates": ("gpt-5.5",),
    },
    {
        "label": "gpt-5.6",
        "catalog_prefixes": ("gpt-5.6",),
        "deployment_candidates": (
            "gpt-5.6",
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
        ),
    },
)


def _result() -> dict[str, Any]:
    return {
        "status": "error",
        "tier": None,
        "rpm": None,
        "tpm": None,
        "error": None,
        "extra": {},
    }


def _error_message(response: httpx.Response, api_key: str) -> str:
    try:
        error = response.json().get("error", {})
        if isinstance(error, dict):
            message = error.get("message") or error.get("code") or ""
        else:
            message = str(error)
    except (TypeError, ValueError):
        message = ""
    cleaned = str(message).replace(api_key, "[redacted]").strip()[:200]
    return cleaned or f"HTTP {response.status_code}"


def _response_error_code(response: httpx.Response) -> str | None:
    try:
        error = response.json().get("error", {})
    except (TypeError, ValueError):
        return None
    if not isinstance(error, dict):
        return None
    value = error.get("code") or error.get("type")
    return str(value).strip()[:100] if value else None


def _response_model(response: httpx.Response) -> str | None:
    try:
        value = response.json().get("model")
    except (TypeError, ValueError):
        return None
    return str(value).strip()[:128] if value else None


def _model_ids(response: httpx.Response) -> list[str]:
    try:
        data = response.json().get("data", [])
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [
        str(model["id"]).strip()
        for model in data
        if isinstance(model, dict) and str(model.get("id") or "").strip()
    ]


def _matches_prefix(model_id: str, prefixes: tuple[str, ...]) -> bool:
    value = model_id.lower()
    return any(
        value == prefix.lower()
        or value.startswith(f"{prefix.lower()}-")
        or value.startswith(f"{prefix.lower()}.")
        for prefix in prefixes
    )


def _target_catalog_models(model_ids: list[str], prefixes: tuple[str, ...]) -> list[str]:
    return list(dict.fromkeys(
        model_id for model_id in model_ids if _matches_prefix(model_id, prefixes)
    ))


def _deployment_candidates(target: dict, catalog_models: list[str]) -> list[str]:
    candidates = list(target["deployment_candidates"])
    for model_id in catalog_models:
        unversioned = MODEL_DATE_SUFFIX.sub("", model_id)
        if unversioned not in candidates:
            candidates.append(unversioned)
        if model_id not in candidates:
            candidates.append(model_id)
    return candidates


async def _request_models(
    client: httpx.AsyncClient,
    endpoint: str,
    api_key: str,
) -> tuple[httpx.Response, str]:
    headers = {"api-key": api_key, "Accept": "application/json"}
    v1_response = await client.get(
        f"https://{endpoint}{V1_MODELS_PATH}",
        headers=headers,
    )
    if v1_response.status_code not in (400, 404, 405):
        return v1_response, "v1"

    ga_response = await client.get(
        f"https://{endpoint}{GA_MODELS_PATH}",
        headers=headers,
    )
    return ga_response, GA_API_VERSION


async def _probe_target_model(
    client: httpx.AsyncClient,
    endpoint: str,
    api_key: str,
    target: dict,
    catalog_models: list[str],
) -> dict:
    attempts = []
    saw_timeout = False
    headers = {"api-key": api_key, "Content-Type": "application/json"}

    for deployment in _deployment_candidates(target, catalog_models):
        try:
            response = await client.post(
                f"https://{endpoint}{V1_CHAT_COMPLETIONS_PATH}",
                headers=headers,
                json={
                    "model": deployment,
                    "messages": [{"role": "user", "content": "Reply OK."}],
                    "max_completion_tokens": 128,
                    "reasoning_effort": "low",
                },
            )
        except httpx.TimeoutException:
            saw_timeout = True
            attempts.append({"deployment": deployment, "status": "timeout"})
            continue
        except httpx.RequestError as exc:
            attempts.append({
                "deployment": deployment,
                "status": "network_error",
                "error_code": type(exc).__name__,
            })
            continue

        error_code = _response_error_code(response)
        attempt = {
            "deployment": deployment,
            "http_status": response.status_code,
        }
        if error_code:
            attempt["error_code"] = error_code
        attempts.append(attempt)

        if response.status_code == 200:
            return {
                "status": "callable",
                "deployment": deployment,
                "response_model": _response_model(response),
                "catalog_models": catalog_models,
                "attempts": attempts,
            }
        if response.status_code == 429:
            return {
                "status": "rate_limited",
                "deployment": deployment,
                "error_code": error_code,
                "catalog_models": catalog_models,
                "attempts": attempts,
            }
        if response.status_code == 401:
            return {
                "status": "authentication_failed",
                "deployment": deployment,
                "error_code": error_code,
                "catalog_models": catalog_models,
                "attempts": attempts,
            }
        if response.status_code == 403:
            return {
                "status": "access_denied",
                "deployment": deployment,
                "error_code": error_code,
                "catalog_models": catalog_models,
                "attempts": attempts,
            }
        if response.status_code == 404 or (error_code or "").lower() == "deploymentnotfound":
            continue
        return {
            "status": "request_rejected",
            "deployment": deployment,
            "http_status": response.status_code,
            "error_code": error_code,
            "catalog_models": catalog_models,
            "attempts": attempts,
        }

    return {
        "status": "unverified" if saw_timeout else "deployment_not_found",
        "catalog_models": catalog_models,
        "attempts": attempts,
        "custom_deployment_name_required": True,
    }


async def _validate_with_client(client: httpx.AsyncClient, credential: str) -> dict:
    """Validate a credential and prove GPT-5.5/5.6 runtime access when possible."""
    result = _result()
    parsed = parse_azure_openai_key(credential)
    if parsed is None:
        result["status"] = "invalid"
        result["error"] = "invalid Azure OpenAI endpoint/key format"
        return result

    endpoint, api_key = parsed
    result["extra"] = {
        "endpoint": endpoint,
        "chat_completions_url": azure_openai_chat_completions_url(endpoint),
        "validation_method": "models_list+minimal_chat_completion",
    }

    try:
        response, api_version = await _request_models(client, endpoint, api_key)
    except httpx.TimeoutException:
        result["error"] = "timeout"
        return result
    except httpx.RequestError as exc:
        result["error"] = type(exc).__name__
        return result

    result["extra"]["api_version"] = api_version
    request_id = response.headers.get("apim-request-id")
    if request_id:
        result["extra"]["request_id"] = request_id

    if response.status_code == 401:
        result["status"] = "invalid"
        result["error"] = "invalid/revoked"
        return result
    if response.status_code not in (200, 429):
        result["error"] = _error_message(response, api_key)
        result["extra"]["http_status"] = response.status_code
        return result

    models = _model_ids(response) if response.status_code == 200 else []
    catalog = build_supported_models(models)
    result["tier"] = "Unknown"
    result["extra"].update({
        "credential_status": "valid",
        "model_catalog": catalog,
        # Compatibility for existing API/UI consumers. This remains explicitly
        # catalog data; runtime truth lives in target_model_probes below.
        "supported_models": catalog,
        "models_count": catalog["all_count"],
        "models_preview": list(dict.fromkeys(models))[:30],
    })
    if response.status_code == 429:
        result["extra"]["models_list_rate_limited"] = True

    probes = {}
    for target in TARGET_MODEL_PROBES:
        catalog_models = _target_catalog_models(models, target["catalog_prefixes"])
        probes[target["label"]] = await _probe_target_model(
            client,
            endpoint,
            api_key,
            target,
            catalog_models,
        )
    result["extra"]["target_model_probes"] = probes

    callable_targets = [
        label for label, probe in probes.items() if probe["status"] == "callable"
    ]
    rate_limited_targets = [
        label for label, probe in probes.items() if probe["status"] == "rate_limited"
    ]
    result["extra"]["verified_callable_targets"] = callable_targets
    result["extra"]["rate_limited_targets"] = rate_limited_targets

    if callable_targets:
        result["status"] = "valid"
        result["extra"]["invocation_verification"] = "success"
    elif rate_limited_targets:
        result["status"] = "no_quota"
        result["error"] = "target deployment authenticated but rate limited"
        result["extra"]["invocation_verification"] = "rate_limited"
    else:
        result["status"] = "error"
        result["error"] = "valid credential; GPT-5.5/5.6 deployment invocation not verified"
        result["extra"]["invocation_verification"] = "not_verified"
    return result


async def check(key: str, proxy: str | None = None) -> dict:
    """Validate one Azure OpenAI ``endpoint|resource-key`` credential."""
    client_kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(45.0, connect=10.0),
        "follow_redirects": False,
    }
    if proxy:
        client_kwargs["proxy"] = proxy
    async with httpx.AsyncClient(**client_kwargs) as client:
        return await _validate_with_client(client, key)
