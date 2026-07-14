"""AWS Bedrock credential checks using real Claude Opus ``InvokeModel`` calls.

The AWS SDK is synchronous, so every network-bearing helper in this module is
run through :func:`asyncio.to_thread`. The quick check walks regions in order
and stops after the first successful invocation. The deep check scans every
known Bedrock region with at most three region workers and records every Claude
Opus inference profile returned in each region.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
import inspect
import json
import os
import re
from typing import Any

from detector import parse_bedrock_key


DEFAULT_REGIONS = (
    "us-east-1",
    "us-east-2",
    "us-west-1",
    "us-west-2",
    "ca-central-1",
    "ca-west-1",
    "sa-east-1",
    "mx-central-1",
    "eu-west-1",
    "eu-west-2",
    "eu-west-3",
    "eu-central-1",
    "eu-central-2",
    "eu-north-1",
    "eu-south-1",
    "eu-south-2",
    "me-south-1",
    "me-central-1",
    "af-south-1",
    "il-central-1",
    "ap-northeast-1",
    "ap-northeast-2",
    "ap-northeast-3",
    "ap-southeast-1",
    "ap-southeast-2",
    "ap-southeast-3",
    "ap-southeast-4",
    "ap-southeast-5",
    "ap-southeast-6",
    "ap-southeast-7",
    "ap-south-1",
    "ap-south-2",
    # The current botocore endpoint catalog does not advertise Hong Kong for
    # Bedrock, but some existing accounts can still reach that regional endpoint.
    "ap-east-1",
    "ap-east-2",
)
MAX_DEEP_REGION_CONCURRENCY = 3
MAX_QUICK_REGION_CONCURRENCY = 6
MAX_QUICK_MODEL_ATTEMPTS_PER_REGION = 4
QUICK_GLOBAL_TIMEOUT_SECONDS = 120

_REGION_PATTERN = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+)+-\d+$")
_OPUS_VERSION_PATTERN = re.compile(
    r"opus(?:[-_.\s]+v?)?(\d+)(?:[-_.\s]+(\d+))?",
    re.IGNORECASE,
)
_INVALID_CREDENTIAL_CODES = {
    "authfailure",
    "expiredtoken",
    "expiredtokenexception",
    "invalidaccesskeyid",
    "invalidclienttokenid",
    "invalidsignatureexception",
    "signaturedoesnotmatch",
    "tokenrefreshrequired",
    "unrecognizedclientexception",
}

_INVOKE_BODY = {
    "anthropic_version": "bedrock-2023-05-31",
    "messages": [{"role": "user", "content": "."}],
    "max_tokens": 1,
}


def configured_regions() -> tuple[str, ...]:
    """Return all known Bedrock regions or a validated environment override.

    ``BEDROCK_REGIONS`` is the primary setting. ``AWS_BEDROCK_REGIONS`` is
    accepted as an alias. Values may be comma, semicolon, or whitespace
    separated. Without an override, the maintained fallback list is merged with
    the installed boto3 endpoint catalog so newly added regions are picked up
    after an SDK update.
    """
    raw = os.getenv("BEDROCK_REGIONS") or os.getenv("AWS_BEDROCK_REGIONS")
    if not raw:
        regions = list(DEFAULT_REGIONS)
        seen = set(regions)
        try:
            import boto3

            sdk_regions = boto3.Session().get_available_regions("bedrock")
        except Exception:  # pragma: no cover - defaults remain fully usable
            sdk_regions = ()
        for region in sdk_regions:
            if region not in seen and _REGION_PATTERN.fullmatch(region):
                seen.add(region)
                regions.append(region)
        return tuple(regions)

    regions: list[str] = []
    seen: set[str] = set()
    for value in re.split(r"[,;\s]+", raw.lower()):
        region = value.strip()
        if not region or region in seen or not _REGION_PATTERN.fullmatch(region):
            continue
        seen.add(region)
        regions.append(region)
    return tuple(regions) or DEFAULT_REGIONS


def match_opus_version(text: str) -> str | None:
    """Extract an Opus version from an inference profile ID or quota name."""
    match = _OPUS_VERSION_PATTERN.search(text or "")
    if not match:
        return None
    major = int(match.group(1))
    # Avoid treating dates from legacy ``claude-3-opus-YYYYMMDD`` IDs as a
    # version placed after the word "opus".
    if major >= 100:
        return None
    minor = match.group(2)
    # A date follows the major version in IDs such as
    # ``claude-opus-4-20250514``; it is not a minor version.
    if minor is None or int(minor) >= 100:
        return str(major)
    return f"{major}.{int(minor)}"


def _version_sort_key(version: str | None) -> tuple[int, ...]:
    if not version:
        return (-1,)
    values = tuple(int(value) for value in re.findall(r"\d+", version))
    return values or (-1,)


def _profile_sort_key(profile: dict[str, str]) -> tuple[Any, ...]:
    model_id = profile["model_id"]
    return (_version_sort_key(profile.get("version")), tuple(int(x) for x in re.findall(r"\d+", model_id)), model_id)


def _profile_preference_key(profile: dict[str, str]) -> tuple[Any, ...]:
    """Prefer a regional inference profile over its global equivalent."""
    model_id = profile["model_id"].lower()
    return (not model_id.startswith("global."), _profile_sort_key(profile))


def _representative_models(
    profiles: list[dict[str, str]],
    foundation_models: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Return one preferred callable candidate for every discovered version.

    System-defined inference profiles are preferred because recent Opus models
    commonly require profile routing. A direct foundation model is retained for
    a version only when no inference profile for that version was discovered.
    """
    representatives: dict[str, dict[str, str]] = {}
    for profile in profiles:
        version = profile["version"]
        current = representatives.get(version)
        if current is None or _profile_preference_key(profile) > _profile_preference_key(current):
            representatives[version] = profile
    for model in foundation_models:
        version = model["version"]
        if version not in representatives:
            representatives[version] = model
    return [
        representatives[version]
        for version in sorted(representatives, key=_version_sort_key, reverse=True)
    ]


def _new_session(access_key_id: str, secret_access_key: str, region: str):
    # Imported lazily so model-summary/unit tests do not require boto3 to be
    # installed before the project's requirements are installed.
    import boto3

    return boto3.Session(
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name=region,
    )


class _SocksURLLib3Session:
    """Build botocore's HTTP transport with urllib3 SOCKS support.

    Botocore only creates HTTP/HTTPS proxy managers by default.  Importing and
    subclassing its transport lazily keeps the checker importable in minimal
    test environments while allowing every AWS service client to share the
    same SOCKS behavior.
    """

    def __new__(cls, *args, **kwargs):
        from botocore.httpsession import ProxyConfiguration, URLLib3Session

        class SocksProxyConfiguration(ProxyConfiguration):
            def _fix_proxy_url(self, proxy_url):
                # Botocore's default normalizer prepends ``http://`` to every
                # non-HTTP scheme, turning ``socks5://`` into the invalid
                # ``http://socks5://``. Preserve native SOCKS URLs for urllib3.
                if proxy_url.lower().startswith(("socks5://", "socks5h://")):
                    return proxy_url
                return super()._fix_proxy_url(proxy_url)

        class SocksSession(URLLib3Session):
            def __init__(self, *session_args, **session_kwargs):
                proxies = session_kwargs.get("proxies")
                proxies_config = session_kwargs.get("proxies_config")
                super().__init__(*session_args, **session_kwargs)
                self._proxy_config = SocksProxyConfiguration(
                    proxies=proxies,
                    proxies_settings=proxies_config,
                )

            def _get_proxy_manager(self, proxy_url):
                if proxy_url not in self._proxy_managers:
                    from urllib3.contrib.socks import SOCKSProxyManager

                    self._proxy_managers[proxy_url] = SOCKSProxyManager(
                        proxy_url,
                        num_pools=self._max_pool_connections,
                        **self._get_pool_manager_kwargs(),
                    )
                return self._proxy_managers[proxy_url]

        return SocksSession(*args, **kwargs)


def _attach_socks_proxy(client, proxy: str, config) -> None:
    """Replace one botocore client's transport with a SOCKS-aware session."""
    transport = _SocksURLLib3Session(
        proxies={"http": proxy, "https": proxy},
        timeout=(config.connect_timeout, config.read_timeout),
        max_pool_connections=config.max_pool_connections,
    )
    old_transport = client._endpoint.http_session
    client._endpoint.http_session = transport
    old_transport.close()


def _client(
    session,
    service_name: str,
    proxy: str | None = None,
    fast: bool = False,
):
    try:
        from botocore.config import Config
    except ImportError:  # pragma: no cover - only useful for dependency-less mocks
        return session.client(service_name)

    config = Config(
        connect_timeout=4 if fast else 10,
        read_timeout=8 if fast else 30,
        retries=(
            {"total_max_attempts": 1, "mode": "standard"}
            if fast
            else {"max_attempts": 2, "mode": "standard"}
        ),
    )
    client = session.client(service_name, config=config)
    if proxy:
        _attach_socks_proxy(client, proxy, config)
    return client


def _redact(value: Any, sensitive_values: tuple[str, ...]) -> str:
    text = str(value)
    for sensitive in sensitive_values:
        if sensitive:
            text = text.replace(sensitive, "[redacted]")
    return text


def _error_code(exc: BaseException, sensitive_values: tuple[str, ...]) -> str:
    response = getattr(exc, "response", None)
    raw_code = None
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            raw_code = error.get("Code")
    if not raw_code:
        raw_code = getattr(exc, "code", None) or type(exc).__name__
    code = _redact(raw_code, sensitive_values)[:120]
    # AWS error codes are identifiers. Avoid persisting arbitrary exception
    # messages even when a third-party exception overloads ``code``.
    return re.sub(r"[^A-Za-z0-9_.:\-\[\]]", "_", code) or type(exc).__name__


def _error_reason(exc: BaseException, sensitive_values: tuple[str, ...]) -> str | None:
    """Return a safe, allow-listed reason for actionable Bedrock failures."""
    response = getattr(exc, "response", None)
    message = ""
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            message = _redact(error.get("Message", ""), sensitive_values).strip().lower()
    if "operation not allowed" in message:
        return "operation_not_allowed"
    return None


def _is_invalid_credential(code: str) -> bool:
    return code.lower() in _INVALID_CREDENTIAL_CODES


def _is_throttled(code: str) -> bool:
    low = code.lower()
    return (
        "throttl" in low
        or "toomanyrequests" in low
        or "requestlimitexceeded" in low
        or "servicequotaexceeded" in low
        or "limitexceeded" in low
    )


def _is_transport_error(code: str | None) -> bool:
    low = (code or "").lower()
    return any(
        marker in low
        for marker in (
            "connect",
            "endpoint",
            "httpclienterror",
            "network",
            "proxy",
            "timeout",
        )
    )


def _base_result(mode: str, proxy: str | None) -> dict:
    return {
        "status": "error",
        "tier": None,
        "rpm": None,
        "tpm": None,
        "error": None,
        "extra": {
            "check_mode": mode,
            "credential_status": "unchecked",
            "account": None,
            "principal": None,
            "regions_checked": [],
            "model_summary": {
                "profiles_found": 0,
                "foundation_models_found": 0,
                "supported_models": [],
                "supported_regions": [],
                "opus_versions": [],
                "successful_versions": [],
                "successful_regions": [],
                "successful_models": [],
            },
            "region_results": {},
            "quotas": {},
            "partial_failures": [],
            "proxy_used": bool(proxy),
            "proxy_ignored": False,
        },
    }


def _validate_identity_sync(
    access_key_id: str,
    secret_access_key: str,
    region: str,
    proxy: str | None = None,
    fast: bool = False,
) -> dict:
    sensitive = (access_key_id, secret_access_key)
    try:
        session = _new_session(access_key_id, secret_access_key, region)
        identity = _client(session, "sts", proxy, fast=fast).get_caller_identity()
    except Exception as exc:
        code = _error_code(exc, sensitive)
        return {"ok": False, "code": code, "invalid": _is_invalid_credential(code)}

    account = _redact(identity.get("Account", ""), sensitive)[:64] or None
    principal = _redact(identity.get("Arn", ""), sensitive)[:512] or None
    return {"ok": True, "account": account, "principal": principal}


def _list_opus_profiles(client, sensitive: tuple[str, ...]) -> tuple[list[dict[str, str]], str | None]:
    profiles: list[dict[str, str]] = []
    seen_profiles: set[str] = set()
    seen_tokens: set[str] = set()
    next_token: str | None = None

    while True:
        request: dict[str, Any] = {"typeEquals": "SYSTEM_DEFINED", "maxResults": 1000}
        if next_token:
            request["nextToken"] = next_token
        try:
            response = client.list_inference_profiles(**request)
        except Exception as exc:
            return profiles, _error_code(exc, sensitive)

        for summary in response.get("inferenceProfileSummaries", []) or []:
            if not isinstance(summary, dict):
                continue
            model_id = _redact(
                summary.get("inferenceProfileId", ""),
                sensitive,
            ).strip()[:256]
            name = _redact(
                summary.get("inferenceProfileName", ""),
                sensitive,
            ).strip()[:256]
            if not model_id or "opus" not in f"{model_id} {name}".lower() or model_id in seen_profiles:
                continue
            seen_profiles.add(model_id)
            profiles.append({
                "model_id": model_id,
                "version": match_opus_version(f"{model_id} {name}") or "unknown",
            })

        token_value = response.get("nextToken")
        next_token = str(token_value).strip() if token_value else None
        if not next_token:
            break
        if next_token in seen_tokens:
            return profiles, "RepeatedPaginationToken"
        seen_tokens.add(next_token)

    return sorted(profiles, key=_profile_sort_key, reverse=True), None


def _list_opus_foundation_models(
    client,
    sensitive: tuple[str, ...],
) -> tuple[list[dict[str, str]], str | None]:
    """Return only Claude Opus base models advertised in one region."""
    try:
        response = client.list_foundation_models()
    except Exception as exc:
        return [], _error_code(exc, sensitive)

    models: list[dict[str, str]] = []
    seen_models: set[str] = set()
    for summary in response.get("modelSummaries", []) or []:
        if not isinstance(summary, dict):
            continue
        model_id = _redact(summary.get("modelId", ""), sensitive).strip()[:256]
        name = _redact(summary.get("modelName", ""), sensitive).strip()[:256]
        provider = _redact(summary.get("providerName", ""), sensitive).strip()[:128]
        if not model_id or model_id in seen_models:
            continue
        if "opus" not in f"{model_id} {name}".lower():
            continue
        seen_models.add(model_id)
        models.append({
            "model_id": model_id,
            "name": name,
            "provider": provider,
            "version": match_opus_version(f"{model_id} {name}") or "unknown",
        })
    return sorted(models, key=_profile_sort_key, reverse=True), None


def _invoke_profile(runtime_client, profile: dict[str, str], sensitive: tuple[str, ...]) -> dict:
    entry = {"model_id": profile["model_id"], "version": profile["version"]}
    try:
        # A successful SDK response is sufficient. Generated content and token
        # usage are deliberately discarded to minimize persisted data.
        response = runtime_client.invoke_model(
            modelId=profile["model_id"],
            body=json.dumps(_INVOKE_BODY),
            contentType="application/json",
            accept="application/json",
        )
        response_body = response.get("body") if isinstance(response, dict) else None
        if response_body is not None and hasattr(response_body, "close"):
            response_body.close()
    except Exception as exc:
        code = _error_code(exc, sensitive)
        entry["status"] = "throttled" if _is_throttled(code) else "error"
        entry["error_code"] = code
        reason = _error_reason(exc, sensitive)
        if reason:
            entry["error_reason"] = reason
        return entry

    entry["status"] = "success"
    return entry


def _empty_region_result(region: str) -> dict:
    return {
        "region": region,
        "list_status": "not_started",
        "profiles": [],
        "foundation_model_list_status": "not_requested",
        "foundation_models": [],
        "invocations": [],
        "quota_status": "not_requested",
    }


def _failure(region: str, stage: str, code: str) -> dict[str, str]:
    return {"region": region, "stage": stage, "code": code}


def _scan_region_quick_sync(
    access_key_id: str,
    secret_access_key: str,
    region: str,
    proxy: str | None = None,
) -> tuple[dict, list[dict[str, str]]]:
    sensitive = (access_key_id, secret_access_key)
    result = _empty_region_result(region)
    failures: list[dict[str, str]] = []

    try:
        session = _new_session(access_key_id, secret_access_key, region)
        bedrock_client = _client(session, "bedrock", proxy, fast=True)
    except Exception as exc:
        code = _error_code(exc, sensitive)
        result.update(
            list_status="error",
            foundation_model_list_status="error",
            error_code=code,
        )
        failures.extend([
            _failure(region, "profile_discovery", code),
            _failure(region, "foundation_model_discovery", code),
        ])
        return result, failures

    profiles, list_error = _list_opus_profiles(bedrock_client, sensitive)
    result["profiles"] = profiles
    result["list_status"] = "partial" if list_error and profiles else "error" if list_error else "success"
    if list_error:
        result["error_code"] = list_error
        failures.append(_failure(region, "profile_discovery", list_error))

    if _is_transport_error(list_error):
        # A second request to the same unreachable regional endpoint only
        # doubles the quick-check delay.
        foundation_models, foundation_error = [], list_error
    else:
        foundation_models, foundation_error = _list_opus_foundation_models(
            bedrock_client,
            sensitive,
        )
    result["foundation_models"] = foundation_models
    result["foundation_model_list_status"] = (
        "partial" if foundation_error and foundation_models
        else "error" if foundation_error
        else "success"
    )
    if foundation_error:
        result["foundation_model_error_code"] = foundation_error
        failures.append(_failure(region, "foundation_model_discovery", foundation_error))

    # A newly advertised profile can temporarily fail at runtime even while an
    # older Opus version remains callable. Try one preferred model per version
    # instead of treating the newest profile as the credential's only signal.
    candidates = _representative_models(profiles, foundation_models)
    if not candidates:
        if not list_error and not foundation_error:
            failures.append(_failure(region, "model_discovery", "NoOpusModels"))
        return result, failures

    try:
        runtime_client = _client(session, "bedrock-runtime", proxy, fast=True)
    except Exception as exc:
        selected = candidates[0]
        invocation = {
            "model_id": selected["model_id"],
            "version": selected["version"],
            "status": "error",
            "error_code": _error_code(exc, sensitive),
        }
        result["invocations"].append(invocation)
        failures.append(_failure(region, "invoke_model", invocation["error_code"]))
        return result, failures

    for selected in candidates[:MAX_QUICK_MODEL_ATTEMPTS_PER_REGION]:
        invocation = _invoke_profile(runtime_client, selected, sensitive)
        result["invocations"].append(invocation)
        if invocation["status"] == "success":
            break
        code = invocation.get("error_code", "UnknownError")
        failures.append(_failure(region, "invoke_model", code))
        # A throttle already proves the request reached an authorized runtime.
        # Invalid credentials and transport failures are model-independent, so
        # retrying additional model IDs would only make quick checks hang longer.
        if invocation["status"] == "throttled" or _is_invalid_credential(code) or _is_transport_error(code):
            break
    return result, failures


def _quota_scope(name: str) -> str:
    low = name.lower()
    if "mantle" in low:
        return "mantle"
    if "global" in low:
        return "global"
    if "cross-region" in low:
        return "xregion"
    return "ondemand"


def _quota_kind(name: str) -> str | None:
    low = name.lower()
    if "request" in low and "per minute" in low:
        return "rpm"
    if "input token" in low and "per minute" in low:
        return "tpm_in"
    if "output token" in low and "per minute" in low:
        return "tpm_out"
    if "token" in low and "per minute" in low:
        return "tpm"
    if "model invocation max tokens per day" in low:
        return "tpd_account"
    if "token" in low and "per day" in low:
        return "tpd_xregion"
    return None


def _safe_quota_value(value: Any) -> int | float | str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, (float, Decimal)):
        number = float(value)
        return int(number) if number.is_integer() else number
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)[:64]
    return int(number) if number.is_integer() else number


def _read_quotas(client, sensitive: tuple[str, ...]) -> tuple[dict, str | None]:
    quotas: dict[str, dict[str, dict[str, int | float | str | None]]] = {}
    try:
        pages = client.get_paginator("list_service_quotas").paginate(ServiceCode="bedrock")
        for page in pages:
            for quota in page.get("Quotas", []) or []:
                if not isinstance(quota, dict):
                    continue
                name = str(quota.get("QuotaName") or "")
                if "opus" not in name.lower():
                    continue
                version = match_opus_version(name)
                kind = _quota_kind(name)
                if not version or not kind:
                    continue
                quotas.setdefault(version, {}).setdefault(_quota_scope(name), {})[kind] = _safe_quota_value(quota.get("Value"))
    except Exception as exc:
        return quotas, _error_code(exc, sensitive)
    return quotas, None


def _scan_region_deep_sync(
    access_key_id: str,
    secret_access_key: str,
    region: str,
    proxy: str | None = None,
) -> tuple[dict, dict, list[dict[str, str]]]:
    sensitive = (access_key_id, secret_access_key)
    result = _empty_region_result(region)
    failures: list[dict[str, str]] = []
    quotas: dict = {}

    try:
        session = _new_session(access_key_id, secret_access_key, region)
    except Exception as exc:
        code = _error_code(exc, sensitive)
        result.update(
            list_status="error",
            foundation_model_list_status="error",
            quota_status="error",
            error_code=code,
        )
        failures.extend([
            _failure(region, "profile_discovery", code),
            _failure(region, "foundation_model_discovery", code),
            _failure(region, "service_quotas", code),
        ])
        return result, quotas, failures

    bedrock_client = None
    try:
        bedrock_client = _client(session, "bedrock", proxy)
        profiles, list_error = _list_opus_profiles(bedrock_client, sensitive)
    except Exception as exc:
        profiles, list_error = [], _error_code(exc, sensitive)
    result["profiles"] = profiles
    result["list_status"] = "partial" if list_error and profiles else "error" if list_error else "success"
    if list_error:
        result["error_code"] = list_error
        failures.append(_failure(region, "profile_discovery", list_error))

    if bedrock_client is None:
        foundation_models, foundation_error = [], list_error
    else:
        foundation_models, foundation_error = _list_opus_foundation_models(
            bedrock_client,
            sensitive,
        )
    result["foundation_models"] = foundation_models
    result["foundation_model_list_status"] = (
        "partial" if foundation_error and foundation_models
        else "error" if foundation_error
        else "success"
    )
    if foundation_error:
        result["foundation_model_error_code"] = foundation_error
        failures.append(_failure(region, "foundation_model_discovery", foundation_error))
    if not profiles and not foundation_models and not list_error and not foundation_error:
        failures.append(_failure(region, "model_discovery", "NoOpusModels"))

    representative_models = _representative_models(profiles, foundation_models)
    representatives = {
        model["version"]: model
        for model in representative_models
    }

    if representatives:
        try:
            runtime_client = _client(session, "bedrock-runtime", proxy)
            ordered_versions = sorted(representatives, key=_version_sort_key, reverse=True)
            for version in ordered_versions:
                invocation = _invoke_profile(runtime_client, representatives[version], sensitive)
                result["invocations"].append(invocation)
                if invocation["status"] != "success":
                    failures.append(_failure(region, "invoke_model", invocation.get("error_code", "UnknownError")))
        except Exception as exc:
            code = _error_code(exc, sensitive)
            for version, profile in representatives.items():
                result["invocations"].append({
                    "model_id": profile["model_id"],
                    "version": version,
                    "status": "error",
                    "error_code": code,
                })
            failures.append(_failure(region, "invoke_model", code))

    try:
        quota_client = _client(session, "service-quotas", proxy)
        quotas, quota_error = _read_quotas(quota_client, sensitive)
    except Exception as exc:
        quota_error = _error_code(exc, sensitive)
    result["quota_status"] = "partial" if quota_error and quotas else "error" if quota_error else "success"
    result["quota_versions"] = sorted(quotas, key=_version_sort_key, reverse=True)
    if quota_error:
        result["quota_error_code"] = quota_error
        failures.append(_failure(region, "service_quotas", quota_error))

    return result, quotas, failures


def _summarize(region_results: dict[str, dict]) -> dict:
    profile_ids: set[str] = set()
    foundation_model_ids: set[str] = set()
    models_by_region: dict[str, list[str]] = {}
    versions: set[str] = set()
    successful_versions: set[str] = set()
    successful_regions: set[str] = set()
    successful_models: set[str] = set()
    throttled_regions: set[str] = set()
    throttled_versions: set[str] = set()
    throttled_models: set[str] = set()
    throttled_attempts = 0
    for region, region_result in region_results.items():
        region_model_ids: set[str] = set()
        for profile in region_result.get("profiles", []):
            model_id = profile["model_id"]
            profile_ids.add(model_id)
            region_model_ids.add(model_id)
            if profile.get("version") and profile["version"] != "unknown":
                versions.add(profile["version"])
        for model in region_result.get("foundation_models", []):
            model_id = model["model_id"]
            foundation_model_ids.add(model_id)
            region_model_ids.add(model_id)
            if model.get("version") and model["version"] != "unknown":
                versions.add(model["version"])
        if region_model_ids:
            models_by_region[region] = sorted(region_model_ids)
        for invocation in region_result.get("invocations", []):
            status = invocation.get("status")
            if status == "throttled":
                throttled_regions.add(region)
                throttled_attempts += 1
                if invocation.get("model_id"):
                    throttled_models.add(invocation["model_id"])
                if invocation.get("version") and invocation["version"] != "unknown":
                    throttled_versions.add(invocation["version"])
            if status != "success":
                continue
            successful_regions.add(region)
            if invocation.get("model_id"):
                successful_models.add(invocation["model_id"])
            if invocation.get("version") and invocation["version"] != "unknown":
                successful_versions.add(invocation["version"])
    supported_model_ids = profile_ids | foundation_model_ids
    return {
        "profiles_found": len(profile_ids),
        "foundation_models_found": len(foundation_model_ids),
        "supported_model_count": len(supported_model_ids),
        "supported_models": sorted(supported_model_ids),
        "supported_regions": list(models_by_region),
        "models_by_region": models_by_region,
        "opus_versions": sorted(versions, key=_version_sort_key, reverse=True),
        "successful_versions": sorted(successful_versions, key=_version_sort_key, reverse=True),
        "successful_regions": sorted(successful_regions),
        "successful_models": sorted(successful_models),
        "throttled_regions": sorted(throttled_regions),
        "throttled_versions": sorted(throttled_versions, key=_version_sort_key, reverse=True),
        "throttled_models": sorted(throttled_models),
        "throttled_attempts": throttled_attempts,
    }


def _finish_result(result: dict) -> dict:
    extra = result["extra"]
    extra["model_summary"] = _summarize(extra["region_results"])
    if extra.get("credential_status") == "sts_unavailable" and any(
        region_result.get("list_status") in {"success", "partial"}
        or region_result.get("foundation_model_list_status") in {"success", "partial"}
        for region_result in extra["region_results"].values()
    ):
        # A successfully signed Bedrock response is sufficient proof that the
        # credential works even when the account/proxy cannot call STS.
        extra["credential_status"] = "bedrock_verified"
    invocations = [
        invocation
        for region_result in extra["region_results"].values()
        for invocation in region_result.get("invocations", [])
    ]
    observed_failures: list[tuple[str | None, bool]] = []
    for invocation in invocations:
        if invocation.get("status") == "success":
            continue
        code = invocation.get("error_code")
        observed_failures.append(
            (
                code,
                _is_throttled(code)
                if code
                else invocation.get("status") == "throttled",
            )
        )
    for failure in extra.get("partial_failures", []):
        # STS is an identity-enrichment check, not a Bedrock availability
        # signal.  Keep its diagnostic without letting it override Bedrock.
        if failure.get("stage") == "sts":
            continue
        code = failure.get("code")
        observed_failures.append((code, bool(code) and _is_throttled(code)))

    successful_invocation = any(
        invocation.get("status") == "success"
        for invocation in invocations
    )
    throttled_invocations = [
        invocation
        for invocation in invocations
        if invocation.get("status") == "throttled"
        or _is_throttled(invocation.get("error_code", ""))
    ]

    if successful_invocation:
        result["status"] = "valid"
        result["error"] = None
        extra["availability_basis"] = "invoke_model"
        extra["invocation_verification"] = "success"
    elif throttled_invocations:
        # A runtime throttle is a positively authenticated Bedrock model call.
        # Other versions/regions may still be denied or missing, but they must
        # not override proven Opus access and turn the whole key red.
        result["status"] = "no_quota"
        result["error"] = "Opus access confirmed; calls are throttled or quota-limited"
        extra["availability_basis"] = "invoke_model_throttled"
        extra["invocation_verification"] = "throttled"
    elif observed_failures and all(
        bool(code) and _is_invalid_credential(code)
        for code, _ in observed_failures
    ):
        result["status"] = "invalid"
        result["error"] = "invalid AWS credentials (Bedrock rejected the signature)"
        extra["credential_status"] = "invalid"
    elif observed_failures and all(is_throttled for _, is_throttled in observed_failures):
        result["status"] = "no_quota"
        result["error"] = "all Bedrock checks were throttled"
        extra["invocation_verification"] = "throttled"
    else:
        result["status"] = "error"
        extra["availability_basis"] = "runtime_failed" if invocations else "not_invoked"
        extra["invocation_verification"] = "failed" if invocations else "not_attempted"
        non_throttle_errors = [
            code for code, is_throttled in observed_failures
            if code and not is_throttled
        ]
        invocation_errors = [
            invocation.get("error_code")
            for invocation in invocations
            if invocation.get("error_code")
        ]
        operation_not_allowed = any(
            invocation.get("error_reason") == "operation_not_allowed"
            for invocation in invocations
        )
        if operation_not_allowed:
            result["error"] = "Bedrock InvokeModel blocked (Operation not allowed)"
            extra["runtime_restriction"] = "operation_not_allowed"
        elif non_throttle_errors:
            result["error"] = f"Bedrock check failed ({non_throttle_errors[0]})"
        elif invocation_errors:
            result["error"] = f"Bedrock InvokeModel failed ({invocation_errors[0]})"
        elif extra["model_summary"]["supported_model_count"] == 0:
            result["error"] = "no callable Claude Opus model found"
        else:
            result["error"] = "Bedrock check did not complete"
    return result


async def _verified_result(
    key: str,
    proxy: str | None,
    mode: str,
) -> tuple[dict, tuple[str, str] | None, tuple[str, ...]]:
    result = _base_result(mode, proxy)
    credentials = parse_bedrock_key(key)
    regions = configured_regions()
    if credentials is None:
        result["status"] = "invalid"
        result["error"] = "invalid AWS Bedrock credential format"
        result["extra"]["credential_status"] = "invalid_format"
        return result, None, regions

    access_key_id, secret_access_key = credentials
    identity = await asyncio.to_thread(
        _validate_identity_sync,
        access_key_id,
        secret_access_key,
        regions[0],
        proxy,
        mode == "quick",
    )
    if not identity["ok"]:
        result["extra"]["partial_failures"].append(
            _failure(regions[0], "sts", identity["code"])
        )
        if identity["invalid"]:
            result["extra"]["credential_status"] = "invalid"
            result["status"] = "invalid"
            result["error"] = f"invalid AWS credentials ({identity['code']})"
            return result, None, regions

        # Regional STS can be disabled or unreachable through a particular
        # proxy even though the same signed credentials work with Bedrock.
        # Preserve the STS diagnostic and let the actual Bedrock calls decide.
        result["extra"]["credential_status"] = "sts_unavailable"
        result["extra"]["sts_error_code"] = identity["code"]
        return result, credentials, regions

    result["extra"]["credential_status"] = "verified"
    result["extra"]["account"] = identity["account"]
    result["extra"]["principal"] = identity["principal"]
    return result, credentials, regions


async def _notify_progress(progress_callback, label: str) -> None:
    if progress_callback is None:
        return
    try:
        outcome = progress_callback(label)
        if inspect.isawaitable(outcome):
            await outcome
    except Exception:
        # Progress reporting is best-effort and must never invalidate a Key.
        return


async def check(
    key: str,
    proxy: str | None = None,
    progress_callback=None,
) -> dict:
    """Run a bounded quick check and stop at the first callable Opus model."""
    result, credentials, regions = await _verified_result(key, proxy, "quick")
    await _notify_progress(progress_callback, "STS 验证")
    if credentials is None:
        return result
    access_key_id, secret_access_key = credentials
    semaphore = asyncio.Semaphore(MAX_QUICK_REGION_CONCURRENCY)
    remaining_tasks: list[asyncio.Task] = []

    async def scan(region: str):
        async with semaphore:
            try:
                region_result, failures = await asyncio.to_thread(
                    _scan_region_quick_sync,
                    access_key_id,
                    secret_access_key,
                    region,
                    proxy,
                )
            except Exception as exc:  # defensive: contain every regional failure
                code = _error_code(exc, (access_key_id, secret_access_key))
                region_result = _empty_region_result(region)
                region_result.update(list_status="error", error_code=code)
                failures = [_failure(region, "region_scan", code)]
            finally:
                await _notify_progress(progress_callback, region)
        return region, region_result, failures

    def record(output) -> bool:
        region, region_result, failures = output
        result["extra"]["regions_checked"].append(region)
        result["extra"]["region_results"][region] = region_result
        result["extra"]["partial_failures"].extend(failures)
        return any(
            item.get("status") == "success"
            for item in region_result.get("invocations", [])
        )

    try:
        async with asyncio.timeout(QUICK_GLOBAL_TIMEOUT_SECONDS):
            # Keep the common us-east-1 success path to one inexpensive request
            # before fanning out across the remaining regions.
            found = record(await scan(regions[0]))
            if not found:
                remaining_tasks = [
                    asyncio.create_task(scan(region))
                    for region in regions[1:]
                ]
                for completed in asyncio.as_completed(remaining_tasks):
                    if record(await completed):
                        break
    except TimeoutError:
        result["extra"]["partial_failures"].append(
            _failure("all", "quick_deadline", "QuickCheckTimeout")
        )
    finally:
        for task in remaining_tasks:
            if not task.done():
                task.cancel()
        if remaining_tasks:
            await asyncio.gather(*remaining_tasks, return_exceptions=True)

    return _finish_result(result)


async def deep_check(
    key: str,
    proxy: str | None = None,
    progress_callback=None,
) -> dict:
    """Scan all known regions, discovering only Opus profiles and quotas."""
    result, credentials, regions = await _verified_result(key, proxy, "bedrock_deep")
    await _notify_progress(progress_callback, "STS 验证")
    if credentials is None:
        return result
    access_key_id, secret_access_key = credentials
    semaphore = asyncio.Semaphore(MAX_DEEP_REGION_CONCURRENCY)

    async def scan(region: str):
        async with semaphore:
            try:
                return await asyncio.to_thread(
                    _scan_region_deep_sync,
                    access_key_id,
                    secret_access_key,
                    region,
                    proxy,
                )
            except Exception as exc:  # defensive: keep other region scans alive
                code = _error_code(exc, (access_key_id, secret_access_key))
                region_result = _empty_region_result(region)
                region_result.update(list_status="error", quota_status="error", error_code=code)
                return region_result, {}, [_failure(region, "region_scan", code)]
            finally:
                await _notify_progress(progress_callback, region)

    outputs = await asyncio.gather(*(scan(region) for region in regions))
    for region, (region_result, quotas, failures) in zip(regions, outputs):
        result["extra"]["regions_checked"].append(region)
        result["extra"]["region_results"][region] = region_result
        if quotas:
            result["extra"]["quotas"][region] = quotas
        result["extra"]["partial_failures"].extend(failures)

    return _finish_result(result)
