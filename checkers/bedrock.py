"""AWS Bedrock credential checks using real Claude ``InvokeModel`` calls.

The AWS SDK is synchronous, so every network-bearing helper in this module is
run through :func:`asyncio.to_thread`. The quick check walks regions in order
and stops after the first successful invocation. The deep check scans every
known Bedrock region with at most three region workers and records Claude Fable
5 and Opus inference profiles returned in each region.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
import inspect
import json
import os
import re
from typing import Any

import httpx

from detector import parse_bedrock_api_key, parse_bedrock_key


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
# AWS publishes a smaller, explicit region set for native ``ABSK...`` Bedrock
# API keys. Do not merge this with the SDK endpoint catalog: an endpoint being
# available for SigV4 credentials does not imply bearer-token support.
API_KEY_REGIONS = (
    "ap-northeast-1",
    "ap-northeast-2",
    "ap-northeast-3",
    "ap-south-1",
    "ap-south-2",
    "ap-southeast-1",
    "ap-southeast-2",
    "ca-central-1",
    "eu-central-1",
    "eu-central-2",
    "eu-north-1",
    "eu-south-1",
    "eu-south-2",
    "eu-west-1",
    "eu-west-2",
    "eu-west-3",
    "sa-east-1",
    "us-east-1",
    "us-gov-east-1",
    "us-gov-west-1",
    "us-west-2",
)
MAX_DEEP_REGION_CONCURRENCY = 3
MAX_QUICK_REGION_CONCURRENCY = 6
MAX_API_KEY_REGION_CONCURRENCY = 8
MAX_QUICK_MODEL_ATTEMPTS_PER_REGION = 4
MAX_QUICK_FABLE_ATTEMPTS_PER_REGION = 3
QUICK_GLOBAL_TIMEOUT_SECONDS = 120

_REGION_PATTERN = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+)+-\d+$")
_OPUS_VERSION_PATTERN = re.compile(
    r"opus(?:[-_.\s]+v?)?(\d+)(?:[-_.\s]+(\d+))?",
    re.IGNORECASE,
)
_FABLE_5_PATTERN = re.compile(r"(?:^|[.\s_-])claude[.\s_-]+fable[.\s_-]+5(?:$|[.\s_:-])", re.IGNORECASE)
_GATEWAY_MODEL_ALIAS_PATTERN = re.compile(
    r"^claude-(?:fable|opus)-[a-z0-9]+(?:-[a-z0-9]+)*$",
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
_FABLE_5_INVOKE_BODY = {
    "anthropic_version": "bedrock-2023-05-31",
    "messages": [{"role": "user", "content": "."}],
    "max_tokens": 16,
    "thinking": {"type": "adaptive"},
    "output_config": {"effort": "low"},
}
# Official in-region launch endpoints. These are only used when both discovery
# APIs are denied, allowing invoke-only IAM credentials to prove Fable access
# without blindly probing every Bedrock region.
_FABLE_5_IN_REGION_IDS = {
    "us-east-1": ("anthropic.claude-fable-5", "us.anthropic.claude-fable-5", "global.anthropic.claude-fable-5"),
    "eu-north-1": ("anthropic.claude-fable-5", "eu.anthropic.claude-fable-5", "global.anthropic.claude-fable-5"),
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


def configured_api_key_regions() -> tuple[str, ...]:
    """Return official bearer-token regions or a validated test/deploy override."""
    raw = os.getenv("BEDROCK_API_KEY_REGIONS")
    if not raw:
        return API_KEY_REGIONS

    regions: list[str] = []
    seen: set[str] = set()
    for value in re.split(r"[,;\s]+", raw.lower()):
        region = value.strip()
        if not region or region in seen or not _REGION_PATTERN.fullmatch(region):
            continue
        seen.add(region)
        regions.append(region)
    return tuple(regions) or API_KEY_REGIONS


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


def _model_family(text: str) -> str | None:
    """Return the supported Claude family represented by a model name or ID."""
    value = text or ""
    if _FABLE_5_PATTERN.search(value):
        return "fable"
    if "opus" in value.lower():
        return "opus"
    return None


def _model_version(text: str) -> str | None:
    family = _model_family(text)
    if family == "fable":
        return "5"
    if family == "opus":
        return match_opus_version(text)
    return None


def _model_key(model: dict[str, str]) -> str:
    family = model.get("family") or _model_family(model.get("model_id", "")) or "unknown"
    return f"{family}:{model.get('version') or 'unknown'}"


def gateway_model_alias(model_id: str) -> str | None:
    """Convert one Bedrock Claude model/profile ID to a gateway-facing alias."""
    value = str(model_id or "").strip().lower()
    marker = "anthropic."
    marker_index = value.find(marker)
    if marker_index < 0:
        return None
    alias = value[marker_index + len(marker):]
    alias = re.sub(r"-v\d+(?::\d+)?$", "", alias)
    alias = re.sub(r":\d+$", "", alias)
    if not _GATEWAY_MODEL_ALIAS_PATTERN.fullmatch(alias):
        return None
    return alias


def _gateway_target_preference(model_id: str) -> tuple[int, int, str]:
    """Prefer portable global routes, then geography routes, then direct IDs."""
    value = str(model_id or "").strip().lower()
    if value.startswith("global."):
        route_priority = 3
    elif value.startswith("anthropic."):
        route_priority = 1
    else:
        route_priority = 2
    # Prefer a shorter canonical ID when two successful routes share a scope.
    return route_priority, -len(value), value


def build_gateway_mappings(
    region_results: dict[str, dict] | None,
) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """Build gateway JSON exclusively from successful ``InvokeModel`` calls.

    The first mapping chooses the most portable successfully tested target for
    each Claude alias. The second preserves exact per-source-region mappings so
    callers can avoid using a profile in a region where it was never proven.
    """
    preferred: dict[str, str] = {}
    by_region: dict[str, dict[str, str]] = {}
    if not isinstance(region_results, dict):
        return preferred, by_region

    for region in sorted(region_results):
        region_result = region_results.get(region)
        if not isinstance(region_result, dict):
            continue
        region_mapping: dict[str, str] = {}
        for invocation in region_result.get("invocations", []):
            if not isinstance(invocation, dict) or invocation.get("status") != "success":
                continue
            model_id = str(invocation.get("model_id") or "").strip()
            alias = gateway_model_alias(model_id)
            if not alias:
                continue
            current_region_target = region_mapping.get(alias)
            if (
                current_region_target is None
                or _gateway_target_preference(model_id)
                > _gateway_target_preference(current_region_target)
            ):
                region_mapping[alias] = model_id
            current_target = preferred.get(alias)
            if (
                current_target is None
                or _gateway_target_preference(model_id)
                > _gateway_target_preference(current_target)
            ):
                preferred[alias] = model_id
        if region_mapping:
            by_region[region] = dict(sorted(region_mapping.items()))

    return dict(sorted(preferred.items())), by_region


def select_gateway_primary_model(
    mappings_by_region: dict[str, dict[str, str]] | None,
) -> tuple[str | None, list[str]]:
    """Choose the model whose proven regions should drive credential export.

    Fable 5 is intentionally preferred whenever it has at least one successful
    invocation. Otherwise the newest successfully invoked Opus alias is used.
    This prevents an any-model region union from overstating where one selected
    gateway model can actually run.
    """
    if not isinstance(mappings_by_region, dict):
        return None, []

    aliases = {
        alias
        for mapping in mappings_by_region.values()
        if isinstance(mapping, dict)
        for alias in mapping
    }
    if "claude-fable-5" in aliases:
        primary_model = "claude-fable-5"
    else:
        opus_aliases = [
            alias
            for alias in aliases
            if alias.startswith("claude-opus-")
        ]
        if not opus_aliases:
            return None, []
        primary_model = max(
            opus_aliases,
            key=lambda alias: (_version_sort_key(alias), alias),
        )

    regions = sorted(
        region
        for region, mapping in mappings_by_region.items()
        if isinstance(mapping, dict) and primary_model in mapping
    )
    return primary_model, regions


def build_common_gateway_mapping(
    mappings_by_region: dict[str, dict[str, str]] | None,
    regions: list[str] | tuple[str, ...],
) -> dict[str, str]:
    """Return only alias/target pairs proven identically in every region."""
    if not isinstance(mappings_by_region, dict) or not regions:
        return {}
    selected_mappings: list[dict[str, str]] = []
    for region in regions:
        mapping = mappings_by_region.get(region)
        if not isinstance(mapping, dict) or not mapping:
            return {}
        selected_mappings.append(mapping)

    first_mapping = selected_mappings[0]
    return dict(sorted(
        (alias, target)
        for alias, target in first_mapping.items()
        if all(mapping.get(alias) == target for mapping in selected_mappings[1:])
    ))


def build_compatible_gateway_route_groups(
    mappings_by_region: dict[str, dict[str, str]] | None,
    regions: list[str] | tuple[str, ...],
) -> list[dict[str, Any]]:
    """Partition regions that can use the exact same flat gateway mapping."""
    if not isinstance(mappings_by_region, dict):
        return []
    grouped: dict[tuple[tuple[str, str], ...], list[str]] = {}
    for region in sorted(regions):
        mapping = mappings_by_region.get(region)
        if not isinstance(mapping, dict) or not mapping:
            continue
        signature = tuple(sorted(mapping.items()))
        grouped.setdefault(signature, []).append(region)

    scope_priority = {
        "global": 0,
        "eu": 1,
        "us": 2,
        "au": 3,
    }

    def route_group_sort_key(
        signature: tuple[tuple[str, str], ...],
    ) -> tuple[int, tuple[str, ...], str]:
        scopes = {
            target.split(".", 1)[0]
            for _, target in signature
            if "." in target
        }
        specific_scopes = sorted(
            (scope for scope in scopes if scope != "global"),
            key=lambda scope: (scope_priority.get(scope, 99), scope),
        )
        primary_scope = (
            specific_scopes[0]
            if specific_scopes
            else "global" if "global" in scopes
            else ""
        )
        return (
            scope_priority.get(primary_scope, 99),
            tuple(specific_scopes),
            grouped[signature][0],
        )

    return [
        {
            "regions": grouped[signature],
            "mapping": dict(signature),
        }
        for signature in sorted(
            grouped,
            key=route_group_sort_key,
        )
    ]


def build_gateway_region_groups(
    mappings_by_region: dict[str, dict[str, str]] | None,
) -> list[dict[str, Any]]:
    """Group Fable 5 regions first, then all remaining callable regions."""
    if not isinstance(mappings_by_region, dict):
        return []
    callable_regions = sorted(
        region
        for region, mapping in mappings_by_region.items()
        if isinstance(mapping, dict) and mapping
    )
    fable_regions = [
        region
        for region in callable_regions
        if "claude-fable-5" in mappings_by_region[region]
    ]
    fable_region_set = set(fable_regions)
    other_regions = [
        region
        for region in callable_regions
        if region not in fable_region_set
    ]

    groups: list[dict[str, Any]] = []
    for kind, regions in (
        ("fable_5", fable_regions),
        ("other_models", other_regions),
    ):
        if not regions:
            continue
        common_mapping = build_common_gateway_mapping(
            mappings_by_region,
            regions,
        )
        group: dict[str, Any] = {
            "kind": kind,
            "regions": regions,
            "mapping": common_mapping,
        }
        if not common_mapping:
            group["route_groups"] = build_compatible_gateway_route_groups(
                mappings_by_region,
                regions,
            )
        groups.append(group)
    return groups


def _known_fable_candidates(region: str) -> list[dict[str, str]]:
    return [
        {"model_id": model_id, "family": "fable", "version": "5"}
        for model_id in _FABLE_5_IN_REGION_IDS.get(region, ())
    ]


def _version_sort_key(version: str | None) -> tuple[int, ...]:
    if not version:
        return (-1,)
    values = tuple(int(value) for value in re.findall(r"\d+", version))
    return values or (-1,)


def _profile_sort_key(profile: dict[str, str]) -> tuple[Any, ...]:
    model_id = profile["model_id"]
    family = profile.get("family") or _model_family(model_id)
    family_priority = 2 if family == "fable" else 1 if family == "opus" else 0
    return (
        family_priority,
        _version_sort_key(profile.get("version")),
        tuple(int(x) for x in re.findall(r"\d+", model_id)),
        model_id,
    )


def _profile_preference_key(profile: dict[str, str]) -> tuple[Any, ...]:
    """Prefer a regional inference profile over its global equivalent."""
    model_id = profile["model_id"].lower()
    if model_id.startswith("anthropic."):
        routing_priority = 3
    elif model_id.startswith("global."):
        routing_priority = 1
    else:
        routing_priority = 2
    return (routing_priority, _profile_sort_key(profile))


def _representative_models(
    profiles: list[dict[str, str]],
    foundation_models: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Return callable target candidates in probe order.

    System-defined inference profiles are preferred because recent Opus models
    commonly require profile routing. A direct foundation model is retained for
    an Opus version only when no inference profile for that version was found.
    Every distinct Fable 5 route is retained so a regional IAM denial can fall
    back to a permitted global, geo, or in-region route.
    """
    fable_candidates: list[dict[str, str]] = []
    seen_fable_ids: set[str] = set()
    for collection in (profiles, foundation_models):
        for model in sorted(collection, key=_profile_preference_key, reverse=True):
            if _model_key(model) != "fable:5" or model["model_id"] in seen_fable_ids:
                continue
            seen_fable_ids.add(model["model_id"])
            fable_candidates.append(model)

    representatives: dict[str, dict[str, str]] = {}
    for profile in profiles:
        model_key = _model_key(profile)
        if model_key == "fable:5":
            continue
        current = representatives.get(model_key)
        if current is None or _profile_preference_key(profile) > _profile_preference_key(current):
            representatives[model_key] = profile
    for model in foundation_models:
        model_key = _model_key(model)
        if model_key == "fable:5":
            continue
        if model_key not in representatives:
            representatives[model_key] = model
    return fable_candidates + sorted(representatives.values(), key=_profile_sort_key, reverse=True)


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
    if "provider_data_share" in message or "data retention" in message:
        return "data_retention_required"
    return None


def _is_invalid_credential(code: str) -> bool:
    return code.lower() in _INVALID_CREDENTIAL_CODES


def _is_access_denied(code: str | None) -> bool:
    low = (code or "").lower()
    return "accessdenied" in low or low == "unauthorizedoperation"


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
                "fable_5": {
                    "status": "not_checked",
                    "supported": None,
                    "discovered_models": [],
                    "discovered_regions": [],
                    "successful_models": [],
                    "successful_regions": [],
                    "throttled_models": [],
                    "throttled_regions": [],
                },
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


def _list_target_profiles(client, sensitive: tuple[str, ...]) -> tuple[list[dict[str, str]], str | None]:
    """List Claude Fable 5 and Opus system-defined inference profiles."""
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
            model_text = f"{model_id} {name}"
            family = _model_family(model_text)
            if not model_id or not family or model_id in seen_profiles:
                continue
            seen_profiles.add(model_id)
            profiles.append({
                "model_id": model_id,
                "family": family,
                "version": _model_version(model_text) or "unknown",
            })

        token_value = response.get("nextToken")
        next_token = str(token_value).strip() if token_value else None
        if not next_token:
            break
        if next_token in seen_tokens:
            return profiles, "RepeatedPaginationToken"
        seen_tokens.add(next_token)

    return sorted(profiles, key=_profile_sort_key, reverse=True), None


def _list_target_foundation_models(
    client,
    sensitive: tuple[str, ...],
) -> tuple[list[dict[str, str]], str | None]:
    """Return Claude Fable 5 and Opus base models advertised in one region."""
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
        model_text = f"{model_id} {name}"
        family = _model_family(model_text)
        if not family:
            continue
        seen_models.add(model_id)
        models.append({
            "model_id": model_id,
            "name": name,
            "provider": provider,
            "family": family,
            "version": _model_version(model_text) or "unknown",
        })
    return sorted(models, key=_profile_sort_key, reverse=True), None


def _invoke_profile(runtime_client, profile: dict[str, str], sensitive: tuple[str, ...]) -> dict:
    family = profile.get("family") or _model_family(profile["model_id"]) or "unknown"
    entry = {
        "model_id": profile["model_id"],
        "family": family,
        "version": profile["version"],
    }
    try:
        # A successful SDK response is sufficient. Generated content and token
        # usage are deliberately discarded to minimize persisted data.
        response = runtime_client.invoke_model(
            modelId=profile["model_id"],
            body=json.dumps(_FABLE_5_INVOKE_BODY if family == "fable" else _INVOKE_BODY),
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

    profiles, list_error = _list_target_profiles(bedrock_client, sensitive)
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
        foundation_models, foundation_error = _list_target_foundation_models(
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

    # Fable 5 is sorted first so its access state is always tested before an
    # Opus success can end the quick check. A newly advertised profile can also
    # fail while an older Opus version remains callable, so keep one fallback
    # candidate per family/version.
    discovery_denied = _is_access_denied(list_error) and _is_access_denied(foundation_error)
    known_fable = (
        _known_fable_candidates(region)
        if discovery_denied and not any(_model_key(model) == "fable:5" for model in profiles + foundation_models)
        else []
    )
    candidates = _representative_models(profiles + known_fable, foundation_models)
    if not candidates:
        if not list_error and not foundation_error:
            failures.append(_failure(region, "model_discovery", "NoTargetClaudeModels"))
        return result, failures

    try:
        runtime_client = _client(session, "bedrock-runtime", proxy, fast=True)
    except Exception as exc:
        selected = candidates[0]
        invocation = {
            "model_id": selected["model_id"],
            "family": selected.get("family") or _model_family(selected["model_id"]) or "unknown",
            "version": selected["version"],
            "status": "error",
            "error_code": _error_code(exc, sensitive),
        }
        result["invocations"].append(invocation)
        failures.append(_failure(region, "invoke_model", invocation["error_code"]))
        return result, failures

    attempt_counts = {"fable": 0, "opus": 0}
    for selected in candidates:
        family = selected.get("family") or _model_family(selected["model_id"]) or "unknown"
        limit = (
            MAX_QUICK_FABLE_ATTEMPTS_PER_REGION
            if family == "fable"
            else MAX_QUICK_MODEL_ATTEMPTS_PER_REGION
        )
        if attempt_counts.get(family, 0) >= limit:
            continue
        attempt_counts[family] = attempt_counts.get(family, 0) + 1
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
                family = _model_family(name)
                if not family:
                    continue
                version = _model_version(name)
                kind = _quota_kind(name)
                if not version or not kind:
                    continue
                quota_key = f"fable-{version}" if family == "fable" else version
                quotas.setdefault(quota_key, {}).setdefault(_quota_scope(name), {})[kind] = _safe_quota_value(quota.get("Value"))
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
        profiles, list_error = _list_target_profiles(bedrock_client, sensitive)
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
        foundation_models, foundation_error = _list_target_foundation_models(
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
        failures.append(_failure(region, "model_discovery", "NoTargetClaudeModels"))

    discovery_denied = _is_access_denied(list_error) and _is_access_denied(foundation_error)
    known_fable = (
        _known_fable_candidates(region)
        if discovery_denied and not any(_model_key(model) == "fable:5" for model in profiles + foundation_models)
        else []
    )
    representative_models = _representative_models(profiles + known_fable, foundation_models)

    if representative_models:
        try:
            runtime_client = _client(session, "bedrock-runtime", proxy)
            confirmed_families: set[str] = set()
            for profile in representative_models:
                family = profile.get("family") or _model_family(profile["model_id"]) or "unknown"
                if family == "fable" and family in confirmed_families:
                    continue
                invocation = _invoke_profile(runtime_client, profile, sensitive)
                result["invocations"].append(invocation)
                if family == "fable" and invocation["status"] in {"success", "throttled"}:
                    confirmed_families.add(family)
                if invocation["status"] != "success":
                    failures.append(_failure(region, "invoke_model", invocation.get("error_code", "UnknownError")))
        except Exception as exc:
            code = _error_code(exc, sensitive)
            for profile in representative_models:
                result["invocations"].append({
                    "model_id": profile["model_id"],
                    "family": profile.get("family") or _model_family(profile["model_id"]) or "unknown",
                    "version": profile["version"],
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
    fable_discovered_models: set[str] = set()
    fable_discovered_regions: set[str] = set()
    fable_successful_models: set[str] = set()
    fable_successful_regions: set[str] = set()
    fable_throttled_models: set[str] = set()
    fable_throttled_regions: set[str] = set()
    fable_failure_codes: set[str] = set()
    fable_failure_reasons: set[str] = set()
    for region, region_result in region_results.items():
        region_model_ids: set[str] = set()
        for profile in region_result.get("profiles", []):
            model_id = profile["model_id"]
            profile_ids.add(model_id)
            region_model_ids.add(model_id)
            family = profile.get("family") or _model_family(model_id)
            if family == "fable":
                fable_discovered_models.add(model_id)
                fable_discovered_regions.add(region)
            elif profile.get("version") and profile["version"] != "unknown":
                versions.add(profile["version"])
        for model in region_result.get("foundation_models", []):
            model_id = model["model_id"]
            foundation_model_ids.add(model_id)
            region_model_ids.add(model_id)
            family = model.get("family") or _model_family(model_id)
            if family == "fable":
                fable_discovered_models.add(model_id)
                fable_discovered_regions.add(region)
            elif model.get("version") and model["version"] != "unknown":
                versions.add(model["version"])
        if region_model_ids:
            models_by_region[region] = sorted(region_model_ids)
        for invocation in region_result.get("invocations", []):
            status = invocation.get("status")
            family = invocation.get("family") or _model_family(invocation.get("model_id", ""))
            if family == "fable":
                model_id = invocation.get("model_id")
                if status == "success":
                    fable_successful_regions.add(region)
                    if model_id:
                        fable_successful_models.add(model_id)
                elif status == "throttled":
                    fable_throttled_regions.add(region)
                    if model_id:
                        fable_throttled_models.add(model_id)
                else:
                    if invocation.get("error_code"):
                        fable_failure_codes.add(invocation["error_code"])
                    if invocation.get("error_reason"):
                        fable_failure_reasons.add(invocation["error_reason"])
            if status == "throttled":
                throttled_regions.add(region)
                throttled_attempts += 1
                if invocation.get("model_id"):
                    throttled_models.add(invocation["model_id"])
                if family != "fable" and invocation.get("version") and invocation["version"] != "unknown":
                    throttled_versions.add(invocation["version"])
            if status != "success":
                continue
            successful_regions.add(region)
            if invocation.get("model_id"):
                successful_models.add(invocation["model_id"])
            if family != "fable" and invocation.get("version") and invocation["version"] != "unknown":
                successful_versions.add(invocation["version"])
    supported_model_ids = profile_ids | foundation_model_ids
    if fable_successful_regions:
        fable_status = "supported"
        fable_supported: bool | None = True
    elif fable_throttled_regions:
        fable_status = "throttled"
        fable_supported = True
    elif "data_retention_required" in fable_failure_reasons:
        fable_status = "data_retention_required"
        fable_supported = False
    elif fable_failure_codes:
        definitive_codes = {
            "accessdeniedexception",
            "modelnotreadyexception",
            "resourcenotfoundexception",
        }
        if all(code.lower() in definitive_codes for code in fable_failure_codes):
            fable_status = "not_supported"
            fable_supported = False
        else:
            fable_status = "check_failed"
            fable_supported = None
    elif fable_discovered_models:
        fable_status = "not_invoked"
        fable_supported = None
    else:
        fable_status = "not_discovered"
        fable_supported = False
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
        "fable_5": {
            "status": fable_status,
            "supported": fable_supported,
            "discovered_models": sorted(fable_discovered_models),
            "discovered_regions": sorted(fable_discovered_regions),
            "successful_models": sorted(fable_successful_models),
            "successful_regions": sorted(fable_successful_regions),
            "throttled_models": sorted(fable_throttled_models),
            "throttled_regions": sorted(fable_throttled_regions),
            "failure_codes": sorted(fable_failure_codes),
            "data_retention_required": "data_retention_required" in fable_failure_reasons,
        },
    }


def _finish_result(result: dict) -> dict:
    extra = result["extra"]
    extra["model_summary"] = _summarize(extra["region_results"])
    gateway_mapping, gateway_mappings_by_region = build_gateway_mappings(
        extra["region_results"]
    )
    extra["gateway_mapping"] = gateway_mapping
    extra["gateway_mappings_by_region"] = gateway_mappings_by_region
    extra["gateway_mapping_basis"] = "invoke_model_success"
    (
        extra["gateway_primary_model"],
        extra["gateway_primary_regions"],
    ) = select_gateway_primary_model(gateway_mappings_by_region)
    extra["gateway_region_groups"] = build_gateway_region_groups(
        gateway_mappings_by_region
    )
    extra["has_claude_fable_5"] = extra["model_summary"]["fable_5"]["supported"]
    extra["fable_5_status"] = extra["model_summary"]["fable_5"]["status"]
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
        # not override proven Claude model access and turn the whole key red.
        result["status"] = "no_quota"
        result["error"] = "Claude model access confirmed; calls are throttled or quota-limited"
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
        data_retention_required = any(
            invocation.get("error_reason") == "data_retention_required"
            and (invocation.get("family") or _model_family(invocation.get("model_id", ""))) == "fable"
            for invocation in invocations
        )
        if operation_not_allowed:
            result["error"] = "Bedrock InvokeModel blocked (Operation not allowed)"
            extra["runtime_restriction"] = "operation_not_allowed"
        elif data_retention_required:
            result["error"] = "Claude Fable 5 requires Bedrock provider_data_share"
            extra["runtime_restriction"] = "fable_5_data_retention_required"
        elif non_throttle_errors:
            result["error"] = f"Bedrock check failed ({non_throttle_errors[0]})"
        elif invocation_errors:
            result["error"] = f"Bedrock InvokeModel failed ({invocation_errors[0]})"
        elif extra["model_summary"]["supported_model_count"] == 0:
            result["error"] = "no callable target Claude model found"
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
    result["extra"]["credential_type"] = "aws_access_key_pair"
    result["extra"]["authentication_scheme"] = "sigv4"
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


def _new_bearer_client(proxy: str | None = None) -> httpx.AsyncClient:
    """Build an isolated client so bearer tokens never touch process env vars."""
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(20.0, connect=10.0),
        "follow_redirects": False,
        "limits": httpx.Limits(
            max_connections=MAX_API_KEY_REGION_CONCURRENCY,
            max_keepalive_connections=MAX_API_KEY_REGION_CONCURRENCY,
        ),
    }
    if proxy:
        kwargs["proxy"] = proxy
    return httpx.AsyncClient(**kwargs)


def _bearer_error_code(response: httpx.Response) -> str:
    header = response.headers.get("x-amzn-errortype", "").split(":", 1)[0].strip()
    if header:
        return header[:128]
    try:
        payload = response.json()
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        for name in ("__type", "code", "Code", "errorType"):
            value = str(payload.get(name) or "").strip()
            if value:
                return value.rsplit("#", 1)[-1][:128]
    return f"HTTP{response.status_code}"


def _is_invalid_bearer_response(status_code: int, error_code: str) -> bool:
    if status_code == 401:
        return True
    return error_code.lower() in {
        "authfailure",
        "expiredtoken",
        "expiredtokenexception",
        "invalidapikey",
        "invalidbearertoken",
        "invalidclienttokenid",
        "invalidtoken",
        "unauthorized",
        "unauthorizedexception",
        "unrecognizedclientexception",
    }


async def _check_bearer_api_key(
    api_key: str,
    proxy: str | None,
    mode: str,
    progress_callback=None,
) -> dict:
    """Authenticate a native Bedrock API key against every supported region.

    ``ListFoundationModels`` is read-only and does not generate model output or
    inference charges. A 200 response proves both bearer authentication and
    regional Bedrock control-plane access; it does not claim that every model
    in the returned catalog is callable.
    """
    result = _base_result(mode, proxy)
    extra = result["extra"]
    extra.update({
        "credential_type": "bedrock_api_key",
        "authentication_scheme": "bearer",
        "availability_basis": "list_foundation_models",
        "invocation_verification": "not_attempted",
    })
    regions = configured_api_key_regions()
    semaphore = asyncio.Semaphore(MAX_API_KEY_REGION_CONCURRENCY)

    async def scan(client: httpx.AsyncClient, region: str):
        entry: dict[str, Any] = {"region": region, "status": "error"}
        model_ids: list[str] = []
        try:
            response = await client.get(
                f"https://bedrock.{region}.amazonaws.com/foundation-models",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/json",
                },
            )
            entry["http_status"] = response.status_code
            if response.status_code == 200:
                entry["status"] = "authorized"
                try:
                    payload = response.json()
                except (TypeError, ValueError):
                    payload = {}
                    entry["response_warning"] = "InvalidJSON"
                summaries = payload.get("modelSummaries", []) if isinstance(payload, dict) else []
                if isinstance(summaries, list):
                    for summary in summaries:
                        if not isinstance(summary, dict):
                            continue
                        model_id = str(summary.get("modelId") or "").strip()[:256]
                        if model_id and model_id not in model_ids:
                            model_ids.append(model_id)
                entry["model_count"] = len(model_ids)
            else:
                error_code = _bearer_error_code(response)
                entry["error_code"] = error_code
                if _is_invalid_bearer_response(response.status_code, error_code):
                    entry["status"] = "invalid"
                elif response.status_code == 403:
                    entry["status"] = "denied"
                elif response.status_code == 429:
                    entry["status"] = "throttled"
        except httpx.HTTPError as exc:
            entry["error_code"] = type(exc).__name__
        except Exception as exc:  # defensive: never persist exception messages
            entry["error_code"] = type(exc).__name__
        finally:
            await _notify_progress(progress_callback, region)
        return region, entry, model_ids

    try:
        async with _new_bearer_client(proxy) as client:
            async def bounded_scan(region: str):
                async with semaphore:
                    return await scan(client, region)

            outputs = await asyncio.gather(*(bounded_scan(region) for region in regions))
    except Exception as exc:
        result["error"] = f"Bedrock API key check failed ({type(exc).__name__})"
        extra["credential_status"] = "unverified"
        return result

    catalog_models: set[str] = set()
    authorized_regions: list[str] = []
    denied_regions: list[str] = []
    invalid_regions: list[str] = []
    throttled_regions: list[str] = []
    error_regions: list[str] = []
    counts_by_region: dict[str, int] = {}
    for region, entry, model_ids in outputs:
        extra["regions_checked"].append(region)
        extra["region_results"][region] = entry
        catalog_models.update(model_ids)
        status = entry["status"]
        if status == "authorized":
            authorized_regions.append(region)
            counts_by_region[region] = int(entry.get("model_count") or 0)
        elif status == "denied":
            denied_regions.append(region)
        elif status == "invalid":
            invalid_regions.append(region)
        elif status == "throttled":
            throttled_regions.append(region)
        else:
            error_regions.append(region)
            extra["partial_failures"].append(
                _failure(region, "list_foundation_models", entry.get("error_code", "UnknownError"))
            )

    summary = extra["model_summary"]
    summary.update({
        "authorized_regions": authorized_regions,
        "denied_regions": denied_regions,
        "invalid_regions": invalid_regions,
        "api_throttled_regions": throttled_regions,
        "api_error_regions": error_regions,
        "supported_regions": authorized_regions,
        "catalog_model_count": len(catalog_models),
        "catalog_models": sorted(catalog_models),
        "catalog_model_counts_by_region": counts_by_region,
    })

    if authorized_regions:
        result["status"] = "valid"
        result["error"] = None
        extra["credential_status"] = "bedrock_verified"
    elif throttled_regions:
        result["status"] = "no_quota"
        result["error"] = "Bedrock API key authenticated but region checks were throttled"
        extra["credential_status"] = "throttled"
    elif invalid_regions and not denied_regions and not error_regions:
        result["status"] = "invalid"
        result["error"] = "invalid or expired AWS Bedrock API key"
        extra["credential_status"] = "invalid"
    elif denied_regions:
        result["status"] = "error"
        result["error"] = "AWS Bedrock API key was denied in every supported region"
        extra["credential_status"] = "access_denied"
    else:
        result["status"] = "error"
        result["error"] = "AWS Bedrock API key could not be verified"
        extra["credential_status"] = "unverified"
    return result


async def check(
    key: str,
    proxy: str | None = None,
    progress_callback=None,
) -> dict:
    """Run a bounded quick check, testing Fable 5 before Opus fallbacks."""
    api_key = parse_bedrock_api_key(key)
    if api_key is not None:
        return await _check_bearer_api_key(
            api_key,
            proxy,
            "quick",
            progress_callback,
        )
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
    """Scan all known regions for Claude Fable 5 and Opus profiles/quotas."""
    api_key = parse_bedrock_api_key(key)
    if api_key is not None:
        return await _check_bearer_api_key(
            api_key,
            proxy,
            "bedrock_deep",
            progress_callback,
        )
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
