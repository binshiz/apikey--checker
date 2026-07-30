"""Anthropic key checker — tier headers, model catalog, and Opus 5 runtime proof.

Anthropic tier thresholds (RPM, approximate, per docs):
  Tier 1: ≤50 RPM
  Tier 2: ≤1000 RPM
  Tier 3: ≤2000 RPM
  Tier 4: ≤4000 RPM

Strategy:
  Validate key first (a single minimal call → check headers).
  Read anthropic-ratelimit-requests-limit header if present (cheapest path).
  Read /v1/models for UI-friendly target model availability.
  Prove Claude Opus 5 access with one minimal paid request.
  If no header info, fall back to burst test.
"""
import asyncio
import time
from typing import Any

import httpx

BASE = "https://api.anthropic.com"
PROBE_MODEL = "claude-haiku-4-5"
FALLBACK_MODEL = "claude-3-5-haiku-latest"
OPUS5_MODEL = "claude-opus-5"
DISPLAY_MODEL_TARGETS = [
    {"label": "opus-5", "prefixes": ("opus-5", "claude-opus-5")},
    {"label": "fable-5", "prefixes": ("fable-5", "claude-fable-5")},
    {"label": "opus-4-8", "prefixes": ("opus-4-8", "claude-opus-4-8")},
    {"label": "opus-4-7", "prefixes": ("opus-4-7", "claude-opus-4-7")},
    {"label": "sonnet-4-6", "prefixes": ("sonnet-4-6", "claude-sonnet-4-6")},
]

PAYLOAD = {
    "model": PROBE_MODEL,
    "max_tokens": 1,
    "messages": [{"role": "user", "content": "."}],
}

ANTHROPIC_VERSION = "2023-06-01"

MAX_BURST = 5000
WAVE_SIZE = 80
TIMEOUT = 20.0


def rpm_to_tier(rpm: int) -> str:
    if rpm <= 60:
        return "Tier 1"
    if rpm <= 1100:
        return "Tier 2"
    if rpm <= 2200:
        return "Tier 3"
    return "Tier 4"


def _parse_int(v):
    try:
        return int(str(v).strip())
    except Exception:
        return None


RATE_LIMIT_HEADERS = {
    "requests": "anthropic-ratelimit-requests",
    "input_tokens": "anthropic-ratelimit-input-tokens",
    "output_tokens": "anthropic-ratelimit-output-tokens",
    "tokens": "anthropic-ratelimit-tokens",
}


def _extract_rate_limits(h: httpx.Headers) -> dict[str, dict[str, int | str]]:
    """Extract current rate-limit capacity without treating it as cash balance."""
    rate_limits: dict[str, dict[str, int | str]] = {}
    for name, prefix in RATE_LIMIT_HEADERS.items():
        item: dict[str, int | str] = {}
        for field in ("limit", "remaining"):
            value = _parse_int(h.get(f"{prefix}-{field}"))
            if value is not None and value >= 0:
                item[field] = value
        reset = str(h.get(f"{prefix}-reset") or "").strip()
        if reset:
            item["reset"] = reset[:100]
        if item:
            rate_limits[name] = item
    return rate_limits


def _extract_rl(h: httpx.Headers) -> tuple[int | None, int | None, int | None]:
    """Return (rpm_limit, input_tpm_limit, output_tpm_limit)."""
    rate_limits = _extract_rate_limits(h)
    rpm = rate_limits.get("requests", {}).get("limit")
    in_tpm = rate_limits.get("input_tokens", {}).get("limit")
    out_tpm = rate_limits.get("output_tokens", {}).get("limit")
    return rpm, in_tpm, out_tpm


def _response_error(response: httpx.Response, key: str) -> tuple[str, str | None]:
    try:
        body = response.json()
    except (TypeError, ValueError):
        body = {}
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        message = error.get("message") or error.get("type") or ""
        code = error.get("type")
    else:
        message = error or ""
        code = None
    cleaned = str(message or "").replace(key, "[redacted]").strip()[:200]
    safe_code = str(code or "").replace(key, "[redacted]").strip()[:100] or None
    return cleaned or f"HTTP {response.status_code}", safe_code


def _response_usage(response: httpx.Response) -> dict[str, int]:
    try:
        body = response.json()
    except (TypeError, ValueError):
        return {}
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return {}
    out = {}
    for field in ("input_tokens", "output_tokens"):
        value = usage.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            out[field] = value
    if out:
        out["total_tokens"] = sum(out.values())
    return out


async def _probe_opus5(
    client: httpx.AsyncClient,
    key: str,
) -> dict[str, Any]:
    """Prove native Claude Opus 5 access with one minimal paid request."""
    probe: dict[str, Any] = {"model": OPUS5_MODEL}
    headers = {
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    try:
        response = await client.post(
            f"{BASE}/v1/messages",
            headers=headers,
            json={
                "model": OPUS5_MODEL,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "."}],
            },
            timeout=TIMEOUT,
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
    rate_limits = _extract_rate_limits(response.headers)
    if rate_limits:
        probe["rate_limits"] = rate_limits
    rpm, input_tpm, output_tpm = _extract_rl(response.headers)
    if rpm is not None:
        probe["rpm"] = rpm
    if input_tpm is not None:
        probe["input_tpm"] = input_tpm
    if output_tpm is not None:
        probe["output_tpm"] = output_tpm

    if response.status_code == 200:
        probe["status"] = "callable"
        try:
            body = response.json()
        except (TypeError, ValueError):
            body = {}
        response_model = body.get("model") if isinstance(body, dict) else None
        if isinstance(response_model, str) and response_model.strip():
            probe["resolved_model"] = response_model.strip()[:160]
        usage = _response_usage(response)
        if usage:
            probe["token_usage"] = usage
        return probe

    error, error_code = _response_error(response, key)
    if response.status_code == 400 and any(
        marker in error.lower()
        for marker in ("credit balance", "billing", "insufficient credit")
    ):
        status = "no_quota"
    else:
        status = {
            401: "authentication_failed",
            402: "no_quota",
            403: "access_denied",
            404: "model_unavailable",
            429: "rate_limited",
        }.get(
            response.status_code,
            "upstream_error" if response.status_code >= 500 else "request_rejected",
        )
    probe["status"] = status
    probe["error"] = error
    if error_code:
        probe["error_code"] = error_code
    return probe


def _model_id(model_info) -> str:
    if isinstance(model_info, str):
        return model_info.strip()
    if isinstance(model_info, dict):
        return str(model_info.get("id") or "").strip()
    return ""


def _dedupe_model_infos(model_infos: list) -> list[dict]:
    seen = set()
    out = []
    for model_info in model_infos or []:
        model_id = _model_id(model_info)
        if not model_id:
            continue
        key = model_id.lower()
        if key in seen:
            continue
        seen.add(key)
        if isinstance(model_info, dict):
            out.append({
                "id": model_id,
                "display_name": model_info.get("display_name"),
                "created_at": model_info.get("created_at"),
                "max_input_tokens": model_info.get("max_input_tokens"),
                "max_tokens": model_info.get("max_tokens"),
            })
        else:
            out.append({"id": model_id})
    return out


def _target_matches(model_id: str, prefix: str) -> bool:
    mid = model_id.lower()
    p = prefix.lower()
    return mid == p or mid.startswith(f"{p}-") or mid.startswith(f"{p}.")


def _find_target_model(models: list[dict], prefixes: tuple[str, ...]) -> dict | None:
    for prefix in prefixes:
        for model in models:
            if model["id"].lower() == prefix.lower():
                return model
    for prefix in prefixes:
        for model in models:
            if _target_matches(model["id"], prefix):
                return model
    return None


def build_supported_models(model_infos: list) -> dict:
    """Build UI-friendly Anthropic model availability summary from /v1/models."""
    models = _dedupe_model_infos(model_infos)
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
        "all_count": len(models),
        "models_preview": [model["id"] for model in models[:30]],
    }


async def _list_models(client: httpx.AsyncClient, key: str) -> tuple[bool, list[dict], str | None]:
    hdrs = {
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
    }
    try:
        resp = await client.get(f"{BASE}/v1/models", headers=hdrs, params={"limit": 1000}, timeout=TIMEOUT)
    except httpx.TimeoutException:
        return False, [], "timeout"
    except Exception as e:
        return False, [], f"{type(e).__name__}"

    if resp.status_code == 401:
        return False, [], "invalid/revoked"
    if resp.status_code != 200:
        return False, [], f"HTTP {resp.status_code}"
    try:
        data = resp.json().get("data", [])
    except Exception:
        return False, [], "invalid models response"
    return True, data if isinstance(data, list) else [], None


async def _single_call(client: httpx.AsyncClient, key: str, model: str) -> httpx.Response | None:
    hdrs = {
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    payload = {**PAYLOAD, "model": model}
    try:
        return await client.post(f"{BASE}/v1/messages", headers=hdrs, json=payload, timeout=TIMEOUT)
    except Exception:
        return None


async def _validate_and_probe_headers(client: httpx.AsyncClient, key: str) -> dict:
    """Single call → check validity and pull rate-limit + org headers."""
    out = {
        "valid": False,
        "error": None,
        "http_status": None,
        "credit_status": "unknown",
        "rate_limits": {},
        "rpm": None,
        "in_tpm": None,
        "out_tpm": None,
        "model": PROBE_MODEL,
        "org_id": None,
    }
    resp = await _single_call(client, key, PROBE_MODEL)
    if resp is None:
        # try fallback
        resp = await _single_call(client, key, FALLBACK_MODEL)
        out["model"] = FALLBACK_MODEL
        if resp is None:
            out["error"] = "network error"
            return out

    if resp.status_code == 401 or resp.status_code == 403:
        try:
            body = resp.json()
            msg = body.get("error", {}).get("message", "")
            if "invalid" in msg.lower() or "authentication" in msg.lower():
                out["error"] = "invalid"
                return out
        except Exception:
            pass
        out["error"] = f"HTTP {resp.status_code}"
        return out

    if resp.status_code == 404:
        # try fallback model
        resp = await _single_call(client, key, FALLBACK_MODEL)
        out["model"] = FALLBACK_MODEL
        if resp is None:
            out["error"] = "network error"
            return out

    if resp.status_code in (200, 429, 400):
        out["valid"] = True
        out["http_status"] = resp.status_code
        out["credit_status"] = "available" if resp.status_code == 200 else "unknown"
        out["rate_limits"] = _extract_rate_limits(resp.headers)
        rpm, in_tpm, out_tpm = _extract_rl(resp.headers)
        out["rpm"] = rpm
        out["in_tpm"] = in_tpm
        out["out_tpm"] = out_tpm
        out["org_id"] = resp.headers.get("anthropic-organization-id")
        if resp.status_code == 400:
            try:
                body = resp.json()
                msg = body.get("error", {}).get("message", "")
                if "credit balance" in msg.lower() or "billing" in msg.lower():
                    out["error"] = "no_quota"
                    out["credit_status"] = "depleted"
            except Exception:
                pass
        return out

    out["error"] = f"HTTP {resp.status_code}"
    return out


async def _burst_test(client: httpx.AsyncClient, key: str, model: str, cap: int = MAX_BURST) -> dict:
    sent = 0
    ok = 0
    hit_429 = False
    start = time.monotonic()
    hdrs_template = {
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    payload = {**PAYLOAD, "model": model}

    async def one():
        nonlocal ok, hit_429
        try:
            r = await client.post(f"{BASE}/v1/messages", headers=hdrs_template, json=payload, timeout=TIMEOUT)
            if r.status_code == 200:
                ok += 1
            elif r.status_code == 429:
                hit_429 = True
        except Exception:
            pass

    while sent < cap and not hit_429:
        elapsed = time.monotonic() - start
        if elapsed > 65:
            break
        wave = min(WAVE_SIZE, cap - sent)
        await asyncio.gather(*(one() for _ in range(wave)))
        sent += wave

    return {"rpm": ok, "ceiling_hit": hit_429, "total_sent": sent}


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
        info = await _validate_and_probe_headers(client, key)
        if not info["valid"]:
            result["status"] = "invalid"
            result["error"] = info["error"]
            return result

        result["extra"]["probe_model"] = info["model"]
        result["extra"]["header_rpm"] = info["rpm"]
        result["extra"]["header_input_tpm"] = info["in_tpm"]
        result["extra"]["header_output_tpm"] = info["out_tpm"]
        result["extra"]["credit_status"] = info["credit_status"]
        if info["rate_limits"]:
            result["extra"]["rate_limit_window"] = {
                "model": info["model"],
                "limits": info["rate_limits"],
            }
        if info.get("org_id"):
            result["extra"]["org_id"] = info["org_id"]

        if info.get("error") == "no_quota":
            result["status"] = "no_quota"
            result["extra"]["credit_status"] = "depleted"
            result["extra"]["opus5_probe"] = {
                "model": OPUS5_MODEL,
                "status": "no_quota",
                "error": "account has no quota",
            }
            result["extra"]["has_opus_5"] = False
            return result

        models_ok, model_infos, models_error = await _list_models(client, key)
        if models_ok:
            supported = build_supported_models(model_infos)
            result["extra"]["supported_models"] = supported
            result["extra"]["models_count"] = supported["all_count"]
            result["extra"]["models_preview"] = supported["models_preview"]
        else:
            result["extra"]["models_error"] = models_error or "models unavailable"

        opus5_probe = await _probe_opus5(client, key)
        result["extra"]["opus5_probe"] = opus5_probe
        result["extra"]["has_opus_5"] = opus5_probe["status"] == "callable"
        if opus5_probe["status"] == "callable":
            result["extra"]["credit_status"] = "available"
        elif (
            opus5_probe["status"] == "no_quota"
            and result["extra"]["credit_status"] == "unknown"
        ):
            result["extra"]["credit_status"] = "depleted"
        if opus5_probe["status"] == "authentication_failed":
            result["status"] = "invalid"
            result["error"] = opus5_probe.get("error") or "invalid/revoked"
            return result
        if info["rpm"] is None and opus5_probe.get("rpm") is not None:
            info["rpm"] = opus5_probe["rpm"]
        if info["in_tpm"] is None and opus5_probe.get("input_tpm") is not None:
            info["in_tpm"] = opus5_probe["input_tpm"]
        if info["out_tpm"] is None and opus5_probe.get("output_tpm") is not None:
            info["out_tpm"] = opus5_probe["output_tpm"]

        # If the API tells us the RPM limit directly, prefer it (no need to burst).
        if info["rpm"] is not None and info["rpm"] > 0:
            result["rpm"] = info["rpm"]
            result["tier"] = rpm_to_tier(info["rpm"])
            result["status"] = "valid"
            result["extra"]["source"] = "header"
            if info["in_tpm"]:
                result["tpm"] = info["in_tpm"]
            return result

        if opus5_probe["status"] == "callable":
            result["status"] = "valid"
            result["tier"] = "Unknown"
            result["extra"]["source"] = "opus5_runtime"
            result["extra"]["tier_reason"] = "runtime succeeded without rate-limit headers"
            return result

        # Otherwise burst test.
        burst = await _burst_test(client, key, info["model"])
        result["extra"]["burst"] = burst
        result["extra"]["source"] = "burst"
        rpm = burst["rpm"]
        result["rpm"] = rpm
        if rpm == 0:
            result["status"] = "no_quota"
            result["error"] = "no successful requests"
        else:
            result["status"] = "valid"
            result["tier"] = rpm_to_tier(rpm)

    return result
