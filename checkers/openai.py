"""OpenAI key checker — exact rate-limit windows + dynamic model discovery."""
import re
from typing import Any

import httpx

BASE = "https://api.openai.com"
MAX_PROBE_ATTEMPTS = 3

PROBE_MODELS = [
    {
        "model": "gpt-4o-mini",
        "tpm_to_tier": {
            200_000: "Tier 1",
            2_000_000: "Tier 2",
            4_000_000: "Tier 3",
            10_000_000: "Tier 4",
            150_000_000: "Tier 5",
        },
    },
    {
        "model": "gpt-4o",
        "tpm_to_tier": {
            30_000: "Tier 1",
            450_000: "Tier 2",
            800_000: "Tier 3",
            2_000_000: "Tier 4",
            30_000_000: "Tier 5",
        },
    },
    {
        "model": "gpt-4.1-mini",
        "tpm_to_tier": {
            200_000: "Tier 1",
            2_000_000: "Tier 2",
            4_000_000: "Tier 3",
            10_000_000: "Tier 4",
            150_000_000: "Tier 5",
        },
    },
    {
        "model": "gpt-3.5-turbo",
        "tpm_to_tier": {
            200_000: "Tier 1",
            2_000_000: "Tier 2",
            4_000_000: "Tier 3",
            10_000_000: "Tier 4",
            50_000_000: "Tier 5",
        },
    },
    {
        "model": "o1-mini",
        "tpm_to_tier": {
            100_000: "Tier 1",
            2_000_000: "Tier 2",
            4_000_000: "Tier 3",
            10_000_000: "Tier 4",
            150_000_000: "Tier 5",
        },
    },
]

RATE_LIMIT_HEADERS = {
    "requests": {
        "limit": (
            "x-ratelimit-limit-requests",
            "x-ratelimit-limit-requests-per-minute",
        ),
        "remaining": ("x-ratelimit-remaining-requests",),
        "reset": ("x-ratelimit-reset-requests",),
    },
    "tokens": {
        "limit": (
            "x-ratelimit-limit-tokens",
            "x-ratelimit-limit-tokens-per-minute",
        ),
        "remaining": ("x-ratelimit-remaining-tokens",),
        "reset": ("x-ratelimit-reset-tokens",),
    },
    "project_tokens": {
        "limit": ("x-ratelimit-limit-project-tokens",),
        "remaining": ("x-ratelimit-remaining-project-tokens",),
        "reset": ("x-ratelimit-reset-project-tokens",),
    },
}

QUOTA_429_CODES = {
    "insufficient_quota",
    "credit_balance_exhausted",
    "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded",
    "organization_usage_limit_exceeded",
}

MODEL_GROUP_ORDER = [
    "text",
    "reasoning",
    "image",
    "video",
    "audio",
    "embedding",
    "moderation",
    "other",
]

DISPLAY_MODEL_TARGETS = [
    {"label": "gpt-5.6", "prefixes": ("gpt-5.6",)},
    {"label": "gpt-5.5", "prefixes": ("gpt-5.5", "gpt5.5")},
    {"label": "gpt-image-2", "prefixes": ("gpt-image-2",)},
    {"label": "sora-2", "prefixes": ("sora-2",)},
]


def _parse_int(v):
    if v is None:
        return None
    try:
        return int(str(v).replace(",", "").strip())
    except Exception:
        return None


def _first_header(h: httpx.Headers, names: tuple[str, ...]) -> str | None:
    for name in names:
        value = h.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _extract_rate_limits(h: httpx.Headers) -> dict[str, dict[str, int | str]]:
    """Extract exact current-window values without treating them as cash balance."""
    rate_limits: dict[str, dict[str, int | str]] = {}
    for dimension, fields in RATE_LIMIT_HEADERS.items():
        item: dict[str, int | str] = {}
        for field in ("limit", "remaining"):
            value = _parse_int(_first_header(h, fields[field]))
            if value is not None and value >= 0:
                item[field] = value
        reset = _first_header(h, fields["reset"])
        if reset:
            item["reset"] = reset[:100]
        if item:
            rate_limits[dimension] = item
    return rate_limits


def _extract_rl(h: httpx.Headers) -> tuple[int | None, int | None]:
    rate_limits = _extract_rate_limits(h)
    rpm = rate_limits.get("requests", {}).get("limit")
    tpm = rate_limits.get("tokens", {}).get("limit")
    if tpm is None:
        tpm = rate_limits.get("project_tokens", {}).get("limit")
    return rpm, tpm


def _tpm_to_tier(tpm: int, mp: dict) -> str | None:
    if tpm is None:
        return None
    if tpm in mp:
        return mp[tpm]
    best, best_diff = None, 1e9
    for ref, tier in mp.items():
        if ref == 0:
            continue
        diff = abs(tpm - ref) / ref
        if diff <= 0.15 and diff < best_diff:
            best, best_diff = tier, diff
    return best


def _version_parts(model_id: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", model_id))


def _variant_rank(model_id: str) -> int:
    m = model_id.lower()
    rank = 0
    if "pro" in m:
        rank += 30
    if "turbo" in m:
        rank += 20
    if "mini" in m:
        rank -= 20
    if "nano" in m:
        rank -= 30
    if "preview" in m:
        rank -= 5
    return rank


def _classify_model(model_id: str) -> str:
    m = model_id.lower()
    if "moderation" in m:
        return "moderation"
    if "embedding" in m:
        return "embedding"
    if m.startswith("sora") or "video" in m:
        return "video"
    if m.startswith(("gpt-image", "dall-e")) or "image" in m:
        return "image"
    if (
        "audio" in m
        or "realtime" in m
        or m.startswith(("tts", "whisper", "transcribe"))
    ):
        return "audio"
    if re.match(r"^o\d", m) or m.startswith("o-") or "reasoning" in m:
        return "reasoning"
    if m.startswith(("gpt-", "chatgpt-", "ft:gpt-")):
        return "text"
    return "other"


def _model_family(model_id: str, group: str | None = None) -> str:
    m = model_id.lower()
    if m.startswith("gpt-image"):
        return "gpt-image"
    if m.startswith("sora"):
        return "sora"
    if group in {"audio", "embedding", "moderation"}:
        return group
    if re.match(r"^o\d", m) or m.startswith("o-"):
        return "o"
    if m.startswith(("gpt-", "chatgpt-", "ft:gpt-")):
        return "gpt"
    return re.split(r"[-:]", m, maxsplit=1)[0] or "other"


def _model_sort_key(model_id: str) -> tuple:
    group = _classify_model(model_id)
    group_rank = MODEL_GROUP_ORDER.index(group) if group in MODEL_GROUP_ORDER else len(MODEL_GROUP_ORDER)
    version = _version_parts(model_id)
    # Negative numeric pieces make Python's ascending sort put larger versions first.
    version_rank = tuple(-part for part in version) or (0,)
    return (group_rank, version_rank, -_variant_rank(model_id), model_id)


def _sorted_models(model_ids: list[str]) -> list[str]:
    seen = set()
    out = []
    for mid in model_ids:
        mid = str(mid).strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append(mid)
    return sorted(out, key=_model_sort_key)


def _target_matches(model_id: str, prefix: str) -> bool:
    mid = model_id.lower()
    p = prefix.lower()
    return mid == p or mid.startswith(f"{p}-") or mid.startswith(f"{p}.")


def _find_target_model(sorted_ids: list[str], prefixes: tuple[str, ...]) -> str | None:
    for prefix in prefixes:
        for mid in sorted_ids:
            if mid.lower() == prefix.lower():
                return mid
    for prefix in prefixes:
        for mid in sorted_ids:
            if _target_matches(mid, prefix):
                return mid
    return None


def build_supported_models(model_ids: list[str]) -> dict:
    """Build UI-friendly model capability summary from /v1/models IDs."""
    sorted_ids = _sorted_models(model_ids)
    groups = {group: [] for group in MODEL_GROUP_ORDER}
    latest_by_family = {}

    for mid in sorted_ids:
        group = _classify_model(mid)
        groups.setdefault(group, []).append(mid)
        family = _model_family(mid, group)
        latest_by_family.setdefault(family, mid)

    display_targets = []
    featured = []
    for target in DISPLAY_MODEL_TARGETS:
        mid = _find_target_model(sorted_ids, target["prefixes"])
        if mid and mid not in featured:
            featured.append(mid)
        display_targets.append({
            "label": target["label"],
            "model": mid,
            "supported": bool(mid),
            "group": _classify_model(mid) if mid else None,
        })

    return {
        "groups": groups,
        "featured": featured,
        "display_targets": display_targets,
        "latest_by_family": latest_by_family,
        "all_count": len(sorted_ids),
    }


def _dynamic_probe_sort_key(model_id: str) -> tuple:
    """Prefer cheaper mini/nano variants before larger dynamically discovered models."""
    lowered = model_id.lower()
    size_rank = 0 if "nano" in lowered else 1 if "mini" in lowered else 2
    return (size_rank, _model_sort_key(model_id))


def _probe_candidates(model_ids: list[str]) -> list[dict[str, Any]]:
    """Choose a few visible text models without assuming the catalog is static."""
    available = set(_sorted_models(model_ids))
    candidates: list[dict[str, Any]] = []
    seen = set()

    for configured in PROBE_MODELS:
        model = configured["model"]
        if available and model not in available:
            continue
        candidates.append({
            **configured,
            "endpoint": (
                "responses"
                if _classify_model(model) == "reasoning"
                else "chat_completions"
            ),
        })
        seen.add(model)

    dynamic_models = sorted(
        (
            model
            for model in available
            if model not in seen
            and not model.startswith(("ft:", "chatgpt-"))
            and _classify_model(model) in {"text", "reasoning"}
            and not any(
                marker in model.lower()
                for marker in ("audio", "realtime", "search", "transcribe")
            )
        ),
        key=_dynamic_probe_sort_key,
    )
    for model in dynamic_models:
        candidates.append({
            "model": model,
            "tpm_to_tier": {},
            "endpoint": "responses",
        })

    return candidates[:MAX_PROBE_ATTEMPTS]


def _parse_error_limit_observation(message: str) -> dict[str, Any]:
    """Extract only numeric 429 observations; never retain the upstream message."""
    text = str(message or "").strip()
    if not text:
        return {}

    lowered = text.lower()
    if re.search(r"\b(?:tokens?\s+per\s+min(?:ute)?|tpm)\b", lowered):
        dimension = "tokens"
    elif re.search(r"\b(?:requests?\s+per\s+min(?:ute)?|rpm)\b", lowered):
        dimension = "requests"
    else:
        dimension = None

    observation: dict[str, Any] = {}
    for field in ("limit", "used", "requested"):
        match = re.search(
            rf"\b{field}\s*:?\s*([\d,]+)",
            text,
            flags=re.IGNORECASE,
        )
        value = _parse_int(match.group(1)) if match else None
        if value is not None and value >= 0:
            observation[field] = value
    if "limit" not in observation:
        return {}

    if dimension:
        observation["dimension"] = dimension
    retry_match = re.search(
        r"(?:try again in|retry after)\s*"
        r"([0-9]*\.?[0-9]+\s*(?:ms|s|sec(?:ond)?s?|m|min(?:ute)?s?))",
        text,
        flags=re.IGNORECASE,
    )
    if retry_match:
        observation["retry_after"] = retry_match.group(1).replace(" ", "")[:40]
    observation["source"] = "error_message"
    return observation


def _response_error_info(response: httpx.Response, key: str) -> dict[str, Any]:
    """Return sanitized error metadata without persisting an upstream error body."""
    try:
        body = response.json()
    except (TypeError, ValueError):
        return {}
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return {}

    info: dict[str, Any] = {}
    for source_field, output_field in (("code", "code"), ("type", "type")):
        value = error.get(source_field)
        if value:
            safe_value = str(value).replace(key, "[redacted]").strip()[:100]
            if safe_value:
                info[output_field] = safe_value

    message = str(error.get("message") or "").replace(key, "[redacted]")
    observation = _parse_error_limit_observation(message)
    if observation:
        info["rate_limit_observation"] = observation
    return info


async def _probe_rate_limits(
    client: httpx.AsyncClient,
    key: str,
    model_ids: list[str],
) -> dict[str, Any]:
    """Make at most a few sequential minimal calls and retain exact limit windows."""
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    candidates = _probe_candidates(model_ids)
    outcome: dict[str, Any] = {
        "tier": None,
        "rpm": None,
        "tpm": None,
        "tier_source": None,
        "tier_confidence": None,
        "rate_limit_windows": [],
        "rate_limit_observation": None,
        "retry_after": None,
        "request_id": None,
        "quota_reason": None,
        "probes": [],
        "callable": False,
        "rate_limited": False,
        "no_quota": False,
        "model_blocked": not candidates,
    }
    http_attempts = 0
    quota_responses = 0
    blocked_responses = 0

    for candidate in candidates:
        model = candidate["model"]
        endpoint = candidate["endpoint"]
        if endpoint == "responses":
            url = f"{BASE}/v1/responses"
            payload = {
                "model": model,
                "input": ".",
                "max_output_tokens": 16,
                "store": False,
            }
        else:
            url = f"{BASE}/v1/chat/completions"
            payload = {
                "model": model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "."}],
            }

        probe: dict[str, Any] = {"model": model, "endpoint": endpoint}
        try:
            response = await client.post(
                url,
                headers=headers,
                json=payload,
                timeout=20.0,
            )
        except httpx.TimeoutException:
            probe.update(status="timeout", error="timeout")
            outcome["probes"].append(probe)
            continue
        except httpx.RequestError as exc:
            probe.update(status="network_error", error=type(exc).__name__)
            outcome["probes"].append(probe)
            continue
        except Exception as exc:
            probe.update(status="network_error", error=type(exc).__name__)
            outcome["probes"].append(probe)
            continue

        http_attempts += 1
        status_code = response.status_code
        error_info = _response_error_info(response, key)
        error_code = error_info.get("code") or error_info.get("type")
        error_type = error_info.get("type")
        probe["http_status"] = status_code
        if error_code:
            probe["error_code"] = error_code
        if error_type and error_type != error_code:
            probe["error_type"] = error_type

        request_id = str(response.headers.get("x-request-id") or "").strip()[:100]
        if request_id:
            probe["request_id"] = request_id
            if outcome["request_id"] is None:
                outcome["request_id"] = request_id

        retry_after = str(response.headers.get("retry-after") or "").strip()[:100]
        observation = error_info.get("rate_limit_observation")
        if observation:
            observation = {
                "model": model,
                "endpoint": endpoint,
                **observation,
            }
            probe["rate_limit_observation"] = observation
            if outcome["rate_limit_observation"] is None:
                outcome["rate_limit_observation"] = observation
            if not retry_after:
                retry_after = str(observation.get("retry_after") or "")
            observed_limit = observation.get("limit")
            if observation.get("dimension") == "tokens" and outcome["tpm"] is None:
                outcome["tpm"] = observed_limit
            elif (
                observation.get("dimension") == "requests"
                and outcome["rpm"] is None
            ):
                outcome["rpm"] = observed_limit
        if retry_after:
            probe["retry_after"] = retry_after
            if outcome["retry_after"] is None:
                outcome["retry_after"] = retry_after

        rate_limits = _extract_rate_limits(response.headers)
        if rate_limits:
            window = {
                "model": model,
                "endpoint": endpoint,
                "limits": rate_limits,
            }
            outcome["rate_limit_windows"].append(window)
            probe["rate_limits"] = rate_limits

            request_limit = rate_limits.get("requests", {}).get("limit")
            model_token_limit = rate_limits.get("tokens", {}).get("limit")
            project_token_limit = rate_limits.get("project_tokens", {}).get("limit")
            if outcome["rpm"] is None and request_limit is not None:
                outcome["rpm"] = request_limit
            if outcome["tpm"] is None:
                outcome["tpm"] = (
                    model_token_limit
                    if model_token_limit is not None
                    else project_token_limit
                )

            tier = None
            if model_token_limit is not None and candidate["tpm_to_tier"]:
                tier = _tpm_to_tier(model_token_limit, candidate["tpm_to_tier"])
                if tier:
                    outcome["tier_source"] = "model_tpm_header_estimate"
            if tier:
                outcome["tier"] = tier
                outcome["tier_confidence"] = "low"

        if status_code == 200:
            probe["status"] = "callable"
            outcome["callable"] = True
        elif status_code == 429 and error_code in QUOTA_429_CODES:
            probe["status"] = "no_quota"
            quota_responses += 1
            if outcome["quota_reason"] is None:
                outcome["quota_reason"] = error_code or error_type
        elif status_code == 429:
            probe["status"] = "rate_limited"
            outcome["rate_limited"] = True
        elif status_code == 401:
            probe["status"] = "authentication_failed"
        elif status_code == 403:
            probe["status"] = "access_denied"
            blocked_responses += 1
        elif status_code == 404:
            probe["status"] = "model_unavailable"
            blocked_responses += 1
        elif status_code >= 500:
            probe["status"] = "upstream_error"
        else:
            probe["status"] = "request_rejected"
        outcome["probes"].append(probe)

        # One exact runtime window is more useful than manufacturing a 429.
        if rate_limits and status_code in (200, 429):
            break

    outcome["no_quota"] = (
        quota_responses > 0
        and not outcome["callable"]
        and not outcome["rate_limited"]
    )
    outcome["model_blocked"] = (
        not candidates
        or (http_attempts > 0 and blocked_responses == http_attempts)
    )
    return outcome


async def _list_models(client: httpx.AsyncClient, key: str):
    """Return (alive, model_ids, org_id, error, rate_limit_window)."""
    try:
        resp = await client.get(
            f"{BASE}/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=25.0
        )
    except httpx.TimeoutException:
        return False, [], None, "timeout", {}
    except Exception as e:
        return False, [], None, f"{type(e).__name__}", {}

    if resp.status_code == 401:
        return False, [], None, "invalid/revoked", {}
    if resp.status_code == 429:
        return (
            True,
            [],
            resp.headers.get("openai-organization"),
            "rate_limited",
            _extract_rate_limits(resp.headers),
        )
    if resp.status_code != 200:
        return False, [], None, f"HTTP {resp.status_code}", {}
    try:
        data = resp.json().get("data", [])
        ids = sorted([m["id"] for m in data if "id" in m])
    except Exception:
        ids = []
    return (
        True,
        ids,
        resp.headers.get("openai-organization"),
        None,
        _extract_rate_limits(resp.headers),
    )


async def check(key: str, proxy: str | None = None) -> dict:
    """Full OpenAI key check. Returns dict matching db.save_result schema."""
    result = {
        "status": "error",
        "tier": None,
        "rpm": None,
        "tpm": None,
        "error": None,
        "extra": {},
    }

    client_kw = dict(
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=10),
    )
    if proxy:
        client_kw["proxy"] = proxy
    async with httpx.AsyncClient(**client_kw) as client:
        alive, models, org_id, err, catalog_limits = await _list_models(client, key)
        if not alive:
            result["status"] = "invalid"
            result["error"] = err or "dead"
            return result

        supported = build_supported_models(models)
        result["extra"]["supported_models"] = supported
        result["extra"]["models_count"] = supported["all_count"]
        result["extra"]["org_id"] = org_id
        result["extra"]["models_preview"] = _sorted_models(models)[:30]
        if err:
            result["extra"]["models_error"] = err

        probe = await _probe_rate_limits(client, key, models)
        runtime_windows = list(probe["rate_limit_windows"])
        windows = list(runtime_windows)
        if catalog_limits:
            windows.append({
                "model": "model catalog",
                "endpoint": "models",
                "limits": catalog_limits,
            })
        if windows:
            result["extra"]["rate_limit_window"] = windows[0]
            result["extra"]["rate_limit_windows"] = windows
        if runtime_windows:
            result["extra"]["rate_limit_source"] = "response_headers"
        elif probe["rate_limit_observation"]:
            result["extra"]["rate_limit_source"] = "error_message_observation"
        elif catalog_limits:
            result["extra"]["rate_limit_source"] = "response_headers"
        else:
            result["extra"]["rate_limit_source"] = "unavailable"

        result["extra"]["rate_limit_probes"] = probe["probes"]
        if probe["rate_limit_observation"]:
            result["extra"]["rate_limit_observation"] = probe["rate_limit_observation"]
        if probe["retry_after"]:
            result["extra"]["retry_after"] = probe["retry_after"]
        if probe["request_id"]:
            result["extra"]["request_id"] = probe["request_id"]
        if probe["quota_reason"]:
            result["extra"]["quota_reason"] = probe["quota_reason"]
        result["extra"]["tier_source"] = probe["tier_source"] or "unavailable"
        if probe["tier_confidence"]:
            result["extra"]["tier_confidence"] = probe["tier_confidence"]

        catalog_rpm = catalog_limits.get("requests", {}).get("limit")
        catalog_tpm = catalog_limits.get("tokens", {}).get("limit")
        if catalog_tpm is None:
            catalog_tpm = catalog_limits.get("project_tokens", {}).get("limit")
        result["rpm"] = probe["rpm"] if probe["rpm"] is not None else catalog_rpm
        result["tpm"] = probe["tpm"] if probe["tpm"] is not None else catalog_tpm

        # Legacy compatibility flags derived from the dynamic /v1/models result.
        groups = supported["groups"]
        text_models = groups.get("text", []) + groups.get("reasoning", [])
        image_models = groups.get("image", [])
        video_models = groups.get("video", [])
        result["extra"]["image_access"] = {mid: True for mid in image_models[:20]}
        result["extra"]["video_access"] = {mid: True for mid in video_models[:20]}
        result["extra"]["chat_access"] = {mid: True for mid in text_models[:20]}
        result["extra"]["has_gpt_image_2"] = any(mid.startswith("gpt-image-2") for mid in image_models)
        result["extra"]["has_sora_2"] = any(mid.startswith(("sora-2", "sora-2-pro")) for mid in video_models)
        result["extra"]["has_gpt_5_5"] = any(mid.startswith(("gpt-5.5", "gpt-5.5-mini")) for mid in text_models)

        if probe["callable"]:
            result["extra"]["invocation_verification"] = "success"
        elif probe["no_quota"]:
            result["extra"]["invocation_verification"] = "no_quota"
        elif probe["rate_limited"]:
            result["extra"]["invocation_verification"] = "rate_limited"
        elif probe["model_blocked"]:
            result["extra"]["invocation_verification"] = "model_unavailable"
        else:
            result["extra"]["invocation_verification"] = "inconclusive"

        if probe["no_quota"]:
            result["status"] = "no_quota"
            result["tier"] = None
            return result

        result["status"] = "valid"
        if probe["tier"] is not None:
            result["tier"] = probe["tier"]
            result["extra"]["tier_reason_code"] = "header_estimate"
            result["extra"]["tier_reason"] = "根据响应限流 Header 估算，非 OpenAI 官方等级"
        else:
            result["tier"] = "Unknown"
            if runtime_windows:
                reason_code = "official_tier_unavailable"
                reason = "已获取限流窗口；普通 API Key 无法直接读取官方 Usage Tier"
            elif probe["rate_limit_observation"]:
                observation = probe["rate_limit_observation"]
                metric = (
                    "TPM"
                    if observation.get("dimension") == "tokens"
                    else "RPM"
                    if observation.get("dimension") == "requests"
                    else "限流"
                )
                reason_code = "rate_limit_observed_from_error"
                reason = f"已从 429 错误正文读取{metric}上限；该值不是稳定 Header"
            elif windows:
                reason_code = "official_tier_unavailable"
                reason = "已获取接口限流窗口；普通 API Key 无法直接读取官方 Usage Tier"
            elif probe["callable"] and probe["rate_limited"]:
                reason_code = "partial_probe_rate_limited"
                reason = (
                    "Key 有效且本轮至少一次最小模型调用成功；"
                    "另有探测请求返回 HTTP 429，不代表整条 Key 当前全面限流"
                )
            elif probe["rate_limited"] and probe["retry_after"]:
                reason_code = "rate_limited_retry_after"
                reason = (
                    "Key 有效；本轮最小模型调用探测返回 HTTP 429，"
                    f"服务端建议 {probe['retry_after']} 后重试，"
                    "不代表整条 Key 当前全面限流"
                )
            elif probe["rate_limited"]:
                reason_code = "rate_limited_without_window"
                reason = (
                    "Key 有效；本轮至少一次最小模型调用探测返回 HTTP 429，"
                    "但响应未提供可解析的限流窗口；"
                    "不代表整条 Key 当前全面限流"
                )
            elif probe["model_blocked"]:
                reason_code = "probe_model_unavailable"
                reason = "Key 有效，但当前可见模型无法完成最小调用探测"
            else:
                reason_code = "rate_limit_window_unavailable"
                reason = "Key 有效，但 OpenAI 未返回限流窗口"
            result["extra"]["tier_reason_code"] = reason_code
            result["extra"]["tier_reason"] = reason

    return result
