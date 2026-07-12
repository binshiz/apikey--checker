"""Gemini key checker — model capability discovery plus RPM tier probing.

Tiers (per user spec):
  RPM <  300         → T1
  300 <= RPM <= 1300 → T2
  RPM > 1300         → T3

Strategy:
  Retrieve every models.list page and summarize declared API capabilities.
  Send small concurrent requests in ramping waves. Count successful responses
  in the current minute. Stop on first 429. The number of successful 200s
  before the 429 is the measured RPM ceiling.
"""
import asyncio
import re
import time
import httpx

BASE = "https://generativelanguage.googleapis.com/v1beta"
PROBE_MODEL = "gemini-2.5-pro"
FALLBACK_MODEL = "gemini-2.5-flash"

# A very cheap payload — 1 input token, 1 output token.
PAYLOAD = {
    "contents": [{"parts": [{"text": "."}]}],
    "generationConfig": {"maxOutputTokens": 1, "temperature": 0},
}

# Max attempts before giving up on T3 verification.
MAX_BURST = 1500
WAVE_SIZE = 60       # parallel requests per wave
TIMEOUT = 20.0
MODEL_PAGE_SIZE = 1000
MAX_MODEL_PAGES = 10

# Keep this short and UI-oriented. The full model list remains available in the
# grouped summary and automatically accommodates newly released model IDs.
DISPLAY_MODEL_TARGETS = [
    {"label": "gemini-3.5-flash", "prefixes": ("gemini-3.5-flash",)},
    {"label": "gemini-3.1-pro", "prefixes": ("gemini-3.1-pro",)},
    {"label": "gemini-3-flash", "prefixes": ("gemini-3-flash",)},
    {"label": "gemini-2.5-pro", "prefixes": ("gemini-2.5-pro",)},
]

MODEL_GROUP_ORDER = ["text", "image", "video", "audio", "live", "embedding", "other"]


def rpm_to_tier(rpm: int) -> str:
    if rpm < 300:
        return "T1"
    if rpm <= 1300:
        return "T2"
    return "T3"


def _model_id(model_info) -> str:
    if isinstance(model_info, str):
        value = model_info
    elif isinstance(model_info, dict):
        value = model_info.get("name") or model_info.get("id") or ""
    else:
        return ""
    value = str(value).strip()
    return value.removeprefix("models/")


def _supported_methods(model_info) -> list[str] | None:
    if not isinstance(model_info, dict):
        return None
    if "supportedGenerationMethods" in model_info:
        methods = model_info.get("supportedGenerationMethods")
    elif "supported_methods" in model_info:
        methods = model_info.get("supported_methods")
    else:
        return None
    if methods is None:
        return None
    if not isinstance(methods, list):
        return []
    return [str(method).strip() for method in methods if str(method).strip()]


def _supports_method(model_info, method: str) -> bool:
    methods = _supported_methods(model_info)
    # String-only/legacy inputs did not carry method metadata. Preserve their
    # previous meaning instead of turning an unknown capability into a denial.
    if methods is None:
        return True
    target = method.lower()
    return any(item.lower() == target for item in methods)


def _model_group(model_id: str) -> str:
    value = model_id.lower()
    if "embedding" in value or value.startswith(("embedding-", "text-embedding-")):
        return "embedding"
    if value.startswith("veo-") or "video-generation" in value or "omni" in value:
        return "video"
    if value.startswith("imagen-") or "image" in value or "nano-banana" in value:
        return "image"
    if "live" in value or "native-audio" in value:
        return "live"
    if "tts" in value or "audio" in value or value.startswith("lyria-"):
        return "audio"
    if value.startswith(("gemini-", "gemma-", "aqa")):
        return "text"
    return "other"


def _model_sort_key(model_info) -> tuple:
    model_id = _model_id(model_info).lower()
    group = _model_group(model_id)
    group_rank = MODEL_GROUP_ORDER.index(group) if group in MODEL_GROUP_ORDER else len(MODEL_GROUP_ORDER)
    version_parts = [int(part) for part in re.findall(r"\d+", model_id)[:6]]
    version = tuple(-part for part in (version_parts + [0] * (6 - len(version_parts))))
    # Prefer stable IDs, then floating latest aliases, then preview/experimental.
    channel_rank = 2 if any(x in model_id for x in ("experimental", "-exp")) else 1 if "preview" in model_id else 0
    return (group_rank, version, channel_rank, model_id)


def _dedupe_model_infos(model_infos: list) -> list[dict]:
    seen = set()
    models = []
    for model_info in model_infos or []:
        model_id = _model_id(model_info)
        if not model_id or model_id.lower() in seen:
            continue
        seen.add(model_id.lower())
        if isinstance(model_info, dict):
            models.append({
                "id": model_id,
                "display_name": model_info.get("displayName"),
                "input_token_limit": model_info.get("inputTokenLimit"),
                "output_token_limit": model_info.get("outputTokenLimit"),
                "supported_methods": _supported_methods(model_info),
                "thinking": model_info.get("thinking"),
            })
        else:
            models.append({"id": model_id, "supported_methods": None})
    return sorted(models, key=_model_sort_key)


def _target_matches(model_id: str, prefix: str) -> bool:
    value = model_id.lower()
    target = prefix.lower()
    return value == target or value.startswith(f"{target}-") or value.startswith(f"{target}.")


def _find_target_model(models: list[dict], prefixes: tuple[str, ...]) -> dict | None:
    callable_models = [model for model in models if _supports_method(model, "generateContent")]
    for prefix in prefixes:
        for model in callable_models:
            if model["id"].lower() == prefix.lower():
                return model
    for prefix in prefixes:
        for model in callable_models:
            if _target_matches(model["id"], prefix):
                return model
    return None


def build_supported_models(model_infos: list) -> dict:
    """Build a UI-friendly capability summary from Gemini ``models.list`` data."""
    models = _dedupe_model_infos(model_infos)
    groups = {group: [] for group in MODEL_GROUP_ORDER}
    method_counts = {}
    callable_count = 0

    for model in models:
        groups[_model_group(model["id"])].append(model["id"])
        methods = model.get("supported_methods")
        if methods is None:
            callable_count += 1
        else:
            if _supports_method(model, "generateContent"):
                callable_count += 1
            for method in set(methods):
                method_counts[method] = method_counts.get(method, 0) + 1

    display_targets = []
    featured = []
    for target in DISPLAY_MODEL_TARGETS:
        model = _find_target_model(models, target["prefixes"])
        if model and model["id"] not in featured:
            featured.append(model["id"])
        display_targets.append({
            "label": target["label"],
            "model": model["id"] if model else None,
            "display_name": model.get("display_name") if model else None,
            "supported": bool(model),
        })

    return {
        "display_targets": display_targets,
        "featured": featured,
        "groups": groups,
        "all_count": len(models),
        "callable_count": callable_count,
        "method_counts": method_counts,
        "models_preview": [model["id"] for model in models[:30]],
    }


def _response_error(resp: httpx.Response) -> str:
    try:
        message = resp.json().get("error", {}).get("message", "")
    except Exception:
        message = ""
    if "API key not valid" in message or "invalid" in message.lower():
        return "invalid"
    return message[:200] or f"HTTP {resp.status_code}"


async def _validate_key(client: httpx.AsyncClient, key: str) -> tuple[bool, str | None, list[dict]]:
    """Verify the key and retrieve every page of model capability metadata."""
    models = []
    page_token = None
    seen_tokens = set()

    for _ in range(MAX_MODEL_PAGES):
        params = {"key": key, "pageSize": MODEL_PAGE_SIZE}
        if page_token:
            params["pageToken"] = page_token
        try:
            resp = await client.get(f"{BASE}/models", params=params, timeout=TIMEOUT)
        except httpx.TimeoutException:
            return (True, "models list timeout", models) if models else (False, "timeout", [])
        except Exception as exc:
            error = type(exc).__name__
            return (True, f"models list {error}", models) if models else (False, error, [])

        if resp.status_code in (400, 401, 403):
            return False, _response_error(resp), []
        if resp.status_code == 429:
            # A rate-limited ListModels response still proves that the key was
            # accepted, but it cannot prove which models are available.
            return True, "models list rate limited", models
        if resp.status_code != 200:
            error = f"HTTP {resp.status_code}"
            return (True, f"partial models list: {error}", models) if models else (False, error, [])

        try:
            body = resp.json()
        except Exception:
            return True, "invalid models response", models
        page_models = body.get("models", [])
        if not isinstance(page_models, list):
            return True, "invalid models response", models
        models.extend(model for model in page_models if isinstance(model, dict))

        page_token = str(body.get("nextPageToken") or "").strip() or None
        if not page_token:
            return True, None, models
        if page_token in seen_tokens:
            return True, "repeated models page token", models
        seen_tokens.add(page_token)

    return True, "models page limit reached", models


async def _single_probe(client: httpx.AsyncClient, key: str, model: str) -> int:
    """One probe call. Return HTTP status code (-1 on exception)."""
    try:
        resp = await client.post(
            f"{BASE}/models/{model}:generateContent",
            params={"key": key},
            json=PAYLOAD,
            timeout=TIMEOUT,
        )
        return resp.status_code
    except Exception:
        return -1


async def _burst_test(client: httpx.AsyncClient, key: str, model: str, cap: int = MAX_BURST) -> dict:
    """
    Fire waves of concurrent requests, count 200s until first 429.
    Stops at: first 429, or cap reached, or 60s elapsed.
    Returns: {rpm, ceiling_hit, first_429_at, total_sent, model_unavailable}
    """
    sent = 0
    ok = 0
    hit_429 = False
    model_unavailable = False
    start = time.monotonic()

    while sent < cap and not hit_429:
        # Use one request as a capability preflight before any concurrent wave.
        # This prevents 60 identical failures when a listed model cannot be
        # called by the project after all.
        wave = 1 if sent == 0 else min(WAVE_SIZE, cap - sent)
        elapsed = time.monotonic() - start
        if elapsed > 65:
            break

        tasks = [_single_probe(client, key, model) for _ in range(wave)]
        statuses = await asyncio.gather(*tasks)
        sent += wave
        for s in statuses:
            if s == 200:
                ok += 1
            elif s == 429:
                hit_429 = True
            elif s in (400, 401, 403, 404) and ok == 0:
                # Model not enabled for this key.
                model_unavailable = True
                hit_429 = True  # break out

    return {
        "rpm": ok,
        "ceiling_hit": hit_429,
        "total_sent": sent,
        "model_unavailable": model_unavailable,
    }


async def check(key: str, proxy: str | None = None) -> dict:
    result = {
        "status": "error",
        "tier": None,
        "rpm": None,
        "tpm": None,
        "error": None,
        "extra": {},
    }
    client_kw = dict(
        timeout=httpx.Timeout(TIMEOUT, connect=10.0),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=WAVE_SIZE + 10),
    )
    if proxy:
        client_kw["proxy"] = proxy
    async with httpx.AsyncClient(**client_kw) as client:
        valid, models_error, model_infos = await _validate_key(client, key)
        if not valid:
            result["status"] = "invalid"
            result["error"] = models_error
            return result

        if model_infos or not models_error:
            supported = build_supported_models(model_infos)
            result["extra"]["supported_models"] = supported
            result["extra"]["models_count"] = supported["all_count"]
            result["extra"]["models_preview"] = supported["models_preview"]
        else:
            result["extra"]["models_count"] = 0
            result["extra"]["models_preview"] = []
        if models_error:
            result["extra"]["models_error"] = models_error
        result["extra"]["probe_model"] = PROBE_MODEL

        burst = await _burst_test(client, key, PROBE_MODEL)

        # Fall back to flash if pro isn't enabled for this key.
        if burst["model_unavailable"]:
            burst = await _burst_test(client, key, FALLBACK_MODEL)
            result["extra"]["probe_model"] = FALLBACK_MODEL

        rpm = burst["rpm"]
        result["rpm"] = rpm
        result["extra"]["burst"] = burst

        if rpm == 0:
            result["status"] = "no_quota"
            result["error"] = "no successful requests"
        else:
            result["status"] = "valid"
            result["tier"] = rpm_to_tier(rpm)

    return result
