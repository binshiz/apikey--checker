"""AWS Bedrock credential checks using real Claude Opus ``Converse`` calls.

The AWS SDK is synchronous, so every network-bearing helper in this module is
run through :func:`asyncio.to_thread`. The quick check walks regions in order
and stops after the first successful invocation. The deep check scans every
configured region with at most three region workers.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
import os
import re
from typing import Any

from detector import parse_bedrock_key


DEFAULT_REGIONS = (
    "us-east-1",
    "us-west-2",
    "eu-west-1",
    "eu-west-3",
    "eu-central-1",
    "ap-southeast-1",
    "ap-southeast-2",
    "ap-northeast-1",
    "ap-south-1",
    "sa-east-1",
    "ca-central-1",
)
MAX_DEEP_REGION_CONCURRENCY = 3

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
    "signaturedoesnotmatch",
    "tokenrefreshrequired",
    "unrecognizedclientexception",
}

_CONVERSE_MESSAGES = [{"role": "user", "content": [{"text": "."}]}]
_CONVERSE_CONFIG = {"maxTokens": 1, "temperature": 0}


def configured_regions() -> tuple[str, ...]:
    """Return the default regions or a validated environment override.

    ``BEDROCK_REGIONS`` is the primary setting. ``AWS_BEDROCK_REGIONS`` is
    accepted as an alias. Values may be comma, semicolon, or whitespace
    separated. Invalid/empty overrides safely fall back to the defaults.
    """
    raw = os.getenv("BEDROCK_REGIONS") or os.getenv("AWS_BEDROCK_REGIONS")
    if not raw:
        return DEFAULT_REGIONS

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


def _new_session(access_key_id: str, secret_access_key: str, region: str):
    # Imported lazily so model-summary/unit tests do not require boto3 to be
    # installed before the project's requirements are installed.
    import boto3

    return boto3.Session(
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name=region,
    )


def _client(session, service_name: str):
    try:
        from botocore.config import Config
    except ImportError:  # pragma: no cover - only useful for dependency-less mocks
        return session.client(service_name)

    config = Config(
        connect_timeout=10,
        read_timeout=30,
        retries={"max_attempts": 2, "mode": "standard"},
    )
    return session.client(service_name, config=config)


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
                "opus_versions": [],
                "successful_versions": [],
                "successful_regions": [],
            },
            "region_results": {},
            "quotas": {},
            "partial_failures": [],
            "proxy_ignored": bool(proxy),
        },
    }


def _validate_identity_sync(
    access_key_id: str,
    secret_access_key: str,
    region: str,
) -> dict:
    sensitive = (access_key_id, secret_access_key)
    try:
        session = _new_session(access_key_id, secret_access_key, region)
        identity = _client(session, "sts").get_caller_identity()
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
            model_id = str(summary.get("inferenceProfileId") or "").strip()
            name = str(summary.get("inferenceProfileName") or "").strip()
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


def _invoke_profile(runtime_client, profile: dict[str, str], sensitive: tuple[str, ...]) -> dict:
    entry = {"model_id": profile["model_id"], "version": profile["version"]}
    try:
        # A successful SDK response is sufficient. Generated content and token
        # usage are deliberately discarded to minimize persisted data.
        runtime_client.converse(
            modelId=profile["model_id"],
            messages=_CONVERSE_MESSAGES,
            inferenceConfig=_CONVERSE_CONFIG,
        )
    except Exception as exc:
        code = _error_code(exc, sensitive)
        entry["status"] = "throttled" if _is_throttled(code) else "error"
        entry["error_code"] = code
        return entry

    entry["status"] = "success"
    return entry


def _empty_region_result(region: str) -> dict:
    return {
        "region": region,
        "list_status": "not_started",
        "profiles": [],
        "invocations": [],
        "quota_status": "not_requested",
    }


def _failure(region: str, stage: str, code: str) -> dict[str, str]:
    return {"region": region, "stage": stage, "code": code}


def _scan_region_quick_sync(
    access_key_id: str,
    secret_access_key: str,
    region: str,
) -> tuple[dict, list[dict[str, str]]]:
    sensitive = (access_key_id, secret_access_key)
    result = _empty_region_result(region)
    failures: list[dict[str, str]] = []

    try:
        session = _new_session(access_key_id, secret_access_key, region)
        bedrock_client = _client(session, "bedrock")
    except Exception as exc:
        code = _error_code(exc, sensitive)
        result.update(list_status="error", error_code=code)
        failures.append(_failure(region, "profile_discovery", code))
        return result, failures

    profiles, list_error = _list_opus_profiles(bedrock_client, sensitive)
    result["profiles"] = profiles
    result["list_status"] = "partial" if list_error and profiles else "error" if list_error else "success"
    if list_error:
        result["error_code"] = list_error
        failures.append(_failure(region, "profile_discovery", list_error))
    if not profiles:
        if not list_error:
            failures.append(_failure(region, "profile_discovery", "NoOpusProfiles"))
        return result, failures

    selected = max(profiles, key=_profile_sort_key)
    try:
        runtime_client = _client(session, "bedrock-runtime")
        invocation = _invoke_profile(runtime_client, selected, sensitive)
    except Exception as exc:
        invocation = {
            "model_id": selected["model_id"],
            "version": selected["version"],
            "status": "error",
            "error_code": _error_code(exc, sensitive),
        }
    result["invocations"].append(invocation)
    if invocation["status"] != "success":
        failures.append(_failure(region, "converse", invocation.get("error_code", "UnknownError")))
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
) -> tuple[dict, dict, list[dict[str, str]]]:
    sensitive = (access_key_id, secret_access_key)
    result = _empty_region_result(region)
    failures: list[dict[str, str]] = []
    quotas: dict = {}

    try:
        session = _new_session(access_key_id, secret_access_key, region)
    except Exception as exc:
        code = _error_code(exc, sensitive)
        result.update(list_status="error", quota_status="error", error_code=code)
        failures.extend([
            _failure(region, "profile_discovery", code),
            _failure(region, "service_quotas", code),
        ])
        return result, quotas, failures

    try:
        bedrock_client = _client(session, "bedrock")
        profiles, list_error = _list_opus_profiles(bedrock_client, sensitive)
    except Exception as exc:
        profiles, list_error = [], _error_code(exc, sensitive)
    result["profiles"] = profiles
    result["list_status"] = "partial" if list_error and profiles else "error" if list_error else "success"
    if list_error:
        result["error_code"] = list_error
        failures.append(_failure(region, "profile_discovery", list_error))
    elif not profiles:
        failures.append(_failure(region, "profile_discovery", "NoOpusProfiles"))

    representatives: dict[str, dict[str, str]] = {}
    for profile in profiles:
        version = profile["version"]
        current = representatives.get(version)
        if current is None or _profile_sort_key(profile) > _profile_sort_key(current):
            representatives[version] = profile

    if representatives:
        try:
            runtime_client = _client(session, "bedrock-runtime")
            ordered_versions = sorted(representatives, key=_version_sort_key, reverse=True)
            for version in ordered_versions:
                invocation = _invoke_profile(runtime_client, representatives[version], sensitive)
                result["invocations"].append(invocation)
                if invocation["status"] != "success":
                    failures.append(_failure(region, "converse", invocation.get("error_code", "UnknownError")))
        except Exception as exc:
            code = _error_code(exc, sensitive)
            for version, profile in representatives.items():
                result["invocations"].append({
                    "model_id": profile["model_id"],
                    "version": version,
                    "status": "error",
                    "error_code": code,
                })
            failures.append(_failure(region, "converse", code))

    try:
        quota_client = _client(session, "service-quotas")
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
    versions: set[str] = set()
    successful_versions: set[str] = set()
    successful_regions: set[str] = set()
    for region, region_result in region_results.items():
        for profile in region_result.get("profiles", []):
            profile_ids.add(profile["model_id"])
            if profile.get("version") and profile["version"] != "unknown":
                versions.add(profile["version"])
        for invocation in region_result.get("invocations", []):
            if invocation.get("status") != "success":
                continue
            successful_regions.add(region)
            if invocation.get("version") and invocation["version"] != "unknown":
                successful_versions.add(invocation["version"])
    return {
        "profiles_found": len(profile_ids),
        "opus_versions": sorted(versions, key=_version_sort_key, reverse=True),
        "successful_versions": sorted(successful_versions, key=_version_sort_key, reverse=True),
        "successful_regions": sorted(successful_regions),
    }


def _finish_result(result: dict) -> dict:
    extra = result["extra"]
    extra["model_summary"] = _summarize(extra["region_results"])
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
        code = failure.get("code")
        observed_failures.append((code, bool(code) and _is_throttled(code)))

    if any(invocation.get("status") == "success" for invocation in invocations):
        result["status"] = "valid"
        result["error"] = None
    elif observed_failures and all(is_throttled for _, is_throttled in observed_failures):
        result["status"] = "no_quota"
        result["error"] = "all Bedrock checks were throttled"
    else:
        result["status"] = "error"
        non_throttle_errors = [
            code for code, is_throttled in observed_failures
            if code and not is_throttled
        ]
        invocation_errors = [
            invocation.get("error_code")
            for invocation in invocations
            if invocation.get("error_code")
        ]
        if non_throttle_errors:
            result["error"] = f"Bedrock check failed ({non_throttle_errors[0]})"
        elif invocation_errors:
            result["error"] = f"Bedrock Converse failed ({invocation_errors[0]})"
        elif extra["model_summary"]["profiles_found"] == 0:
            result["error"] = "no callable Claude Opus inference profile found"
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
    )
    if not identity["ok"]:
        result["extra"]["credential_status"] = "invalid" if identity["invalid"] else "error"
        if identity["invalid"]:
            result["status"] = "invalid"
            result["error"] = f"invalid AWS credentials ({identity['code']})"
        else:
            result["status"] = "error"
            result["error"] = f"AWS STS validation failed ({identity['code']})"
        result["extra"]["partial_failures"].append(_failure(regions[0], "sts", identity["code"]))
        return result, None, regions

    result["extra"]["credential_status"] = "verified"
    result["extra"]["account"] = identity["account"]
    result["extra"]["principal"] = identity["principal"]
    return result, credentials, regions


async def check(key: str, proxy: str | None = None) -> dict:
    """Run a quick Bedrock check and stop at the first callable Opus profile."""
    result, credentials, regions = await _verified_result(key, proxy, "quick")
    if credentials is None:
        return result
    access_key_id, secret_access_key = credentials

    for region in regions:
        try:
            region_result, failures = await asyncio.to_thread(
                _scan_region_quick_sync,
                access_key_id,
                secret_access_key,
                region,
            )
        except Exception as exc:  # defensive: scanners should already contain SDK failures
            code = _error_code(exc, (access_key_id, secret_access_key))
            region_result = _empty_region_result(region)
            region_result.update(list_status="error", error_code=code)
            failures = [_failure(region, "region_scan", code)]
        result["extra"]["regions_checked"].append(region)
        result["extra"]["region_results"][region] = region_result
        result["extra"]["partial_failures"].extend(failures)
        if any(item.get("status") == "success" for item in region_result.get("invocations", [])):
            break

    return _finish_result(result)


async def deep_check(key: str, proxy: str | None = None) -> dict:
    """Scan all regions, one representative per Opus version, plus quotas."""
    result, credentials, regions = await _verified_result(key, proxy, "bedrock_deep")
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
                )
            except Exception as exc:  # defensive: keep other region scans alive
                code = _error_code(exc, (access_key_id, secret_access_key))
                region_result = _empty_region_result(region)
                region_result.update(list_status="error", quota_status="error", error_code=code)
                return region_result, {}, [_failure(region, "region_scan", code)]

    outputs = await asyncio.gather(*(scan(region) for region in regions))
    for region, (region_result, quotas, failures) in zip(regions, outputs):
        result["extra"]["regions_checked"].append(region)
        result["extra"]["region_results"][region] = region_result
        if quotas:
            result["extra"]["quotas"][region] = quotas
        result["extra"]["partial_failures"].extend(failures)

    return _finish_result(result)
