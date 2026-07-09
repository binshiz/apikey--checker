"""OpenAI key checker — tier via TPM/RPM headers + dynamic model discovery."""
import asyncio
import re
import time
import httpx

BASE = "https://api.openai.com"

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

RL_HEADERS_RPM = [
    "x-ratelimit-limit-requests",
    "x-ratelimit-limit-requests-per-minute",
]
RL_HEADERS_TPM = [
    "x-ratelimit-limit-tokens",
    "x-ratelimit-limit-tokens-per-minute",
]

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


def _extract_rl(h: httpx.Headers) -> tuple[int | None, int | None]:
    rpm = next((_parse_int(h.get(x)) for x in RL_HEADERS_RPM if h.get(x)), None)
    tpm = next((_parse_int(h.get(x)) for x in RL_HEADERS_TPM if h.get(x)), None)
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


def _guess_tier_from_rpm(rpm: int | None) -> str | None:
    if rpm is None:
        return None
    if rpm <= 10:
        return "Free"
    if rpm <= 500:
        return "Tier 1"
    if rpm <= 5000:
        return "Tier 2-3"
    if rpm <= 10000:
        return "Tier 4"
    return "Tier 5"


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


async def _detect_tier(client: httpx.AsyncClient, key: str) -> tuple[str | None, int | None, int | None]:
    """Return (tier, rpm, tpm). tier='no_quota' if all probes hit insufficient_quota."""
    hdrs = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    collected_rpm = None
    collected_tpm = None
    all_no_quota = True

    for probe in PROBE_MODELS:
        model = probe["model"]
        payload = {
            "model": model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "."}],
        }
        try:
            resp = await client.post(f"{BASE}/v1/chat/completions", headers=hdrs, json=payload, timeout=30.0)
        except Exception:
            continue

        status = resp.status_code
        if status == 401:
            return None, None, None
        if status in (403, 404):
            all_no_quota = False
            continue
        if status == 429:
            try:
                body = resp.json()
                if body.get("error", {}).get("code") == "insufficient_quota":
                    continue
            except Exception:
                pass

        all_no_quota = False
        rpm, tpm = _extract_rl(resp.headers)
        if rpm is not None:
            collected_rpm = rpm
        if tpm is not None:
            collected_tpm = tpm
        if tpm is not None:
            tier = _tpm_to_tier(tpm, probe["tpm_to_tier"])
            if tier:
                return tier, rpm, tpm

    if all_no_quota:
        return "no_quota", None, None
    if collected_tpm is not None:
        t = _tpm_to_tier(collected_tpm, PROBE_MODELS[0]["tpm_to_tier"])
        if t:
            return t, collected_rpm, collected_tpm
    if collected_rpm is not None:
        return _guess_tier_from_rpm(collected_rpm), collected_rpm, collected_tpm
    return None, collected_rpm, collected_tpm


async def _list_models(client: httpx.AsyncClient, key: str):
    """Return (alive, model_ids, org_id, error, rpm_header, tpm_header)."""
    try:
        resp = await client.get(
            f"{BASE}/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=25.0
        )
    except httpx.TimeoutException:
        return False, [], None, "timeout", None, None
    except Exception as e:
        return False, [], None, f"{type(e).__name__}", None, None

    if resp.status_code == 401:
        return False, [], None, "invalid/revoked", None, None
    if resp.status_code == 429:
        rpm, tpm = _extract_rl(resp.headers)
        return True, [], resp.headers.get("openai-organization"), "rate_limited", rpm, tpm
    if resp.status_code != 200:
        return False, [], None, f"HTTP {resp.status_code}", None, None
    try:
        data = resp.json().get("data", [])
        ids = sorted([m["id"] for m in data if "id" in m])
    except Exception:
        ids = []
    rpm, tpm = _extract_rl(resp.headers)
    return True, ids, resp.headers.get("openai-organization"), None, rpm, tpm


async def _burst_rpm_probe(client: httpx.AsyncClient, key: str, cap: int = 60) -> dict:
    """Conservative RPM burst: fire `cap` minimal requests in parallel, classify by 429 timing.

    Returns:
      {ok, hit_429, no_quota, model_blocked, elapsed, rpm_estimate}
    """
    hdrs = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    # Find a model the key can actually call. Try cheap models in order.
    for model in ("gpt-4o-mini", "gpt-3.5-turbo", "gpt-4.1-mini"):
        payload = {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "."}]}
        try:
            r0 = await client.post(f"{BASE}/v1/chat/completions", headers=hdrs, json=payload, timeout=15)
        except Exception:
            continue
        if r0.status_code in (200, 429):
            picked = model
            break
        # 403/404/etc — try next
    else:
        return {"ok": 0, "hit_429": False, "no_quota": False, "model_blocked": True, "elapsed": 0, "rpm_estimate": None}

    payload = {"model": picked, "max_tokens": 1, "messages": [{"role": "user", "content": "."}]}
    ok = 0
    hit_429 = False
    no_quota = False

    async def one():
        nonlocal ok, hit_429, no_quota
        try:
            r = await client.post(f"{BASE}/v1/chat/completions", headers=hdrs, json=payload, timeout=15)
            if r.status_code == 200:
                ok += 1
            elif r.status_code == 429:
                try:
                    code = r.json().get("error", {}).get("code", "")
                    if code == "insufficient_quota":
                        no_quota = True
                        return
                except Exception:
                    pass
                hit_429 = True
        except Exception:
            pass

    start = time.monotonic()
    await asyncio.gather(*(one() for _ in range(cap)))
    elapsed = time.monotonic() - start

    # If we hit 429, `ok` is the count that landed in the current 1-min window
    # before the limit kicked in → that's roughly the RPM ceiling.
    # If no 429, the account handled `cap` in `elapsed`s; project to 60s.
    if hit_429:
        rpm_estimate = ok
    else:
        # Project to per-minute, but never below ok.
        rpm_estimate = max(ok, int(ok / max(elapsed, 0.5) * 60))

    return {
        "ok": ok, "hit_429": hit_429, "no_quota": no_quota,
        "model_blocked": False, "elapsed": round(elapsed, 2),
        "rpm_estimate": rpm_estimate, "model": picked,
    }


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
        limits=httpx.Limits(max_connections=80),
    )
    if proxy:
        client_kw["proxy"] = proxy
    async with httpx.AsyncClient(**client_kw) as client:
        alive, models, org_id, err, list_rpm, list_tpm = await _list_models(client, key)
        if not alive:
            result["status"] = "invalid"
            result["error"] = err or "dead"
            return result

        supported = build_supported_models(models)
        result["extra"]["supported_models"] = supported
        result["extra"]["models_count"] = supported["all_count"]
        result["extra"]["org_id"] = org_id
        result["extra"]["models_preview"] = _sorted_models(models)[:30]

        tier, rpm, tpm = await _detect_tier(client, key)
        # Prefer /v1/models headers if probes returned nothing
        if rpm is None and list_rpm is not None:
            rpm = list_rpm
        if tpm is None and list_tpm is not None:
            tpm = list_tpm
        # If probes gave no tier but /v1/models headers exist, try mapping from those.
        if tier is None and list_tpm is not None:
            tier = _tpm_to_tier(list_tpm, PROBE_MODELS[0]["tpm_to_tier"])
        if tier is None and rpm is not None:
            tier = _guess_tier_from_rpm(rpm)
        result["rpm"] = rpm
        result["tpm"] = tpm

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

        if tier == "no_quota":
            result["status"] = "no_quota"
            result["tier"] = None
            return result

        # ── Burst RPM probe as last-resort tier signal ──
        if tier is None:
            burst = await _burst_rpm_probe(client, key, cap=60)
            result["extra"]["burst_probe"] = burst
            if burst["no_quota"]:
                result["status"] = "no_quota"
                result["tier"] = None
                return result
            if burst["model_blocked"]:
                result["status"] = "valid"
                result["tier"] = "Unknown"
                result["extra"]["tier_reason"] = "no probe model accessible"
                return result
            rpm_est = burst["rpm_estimate"] or 0
            if rpm_est > 0:
                result["rpm"] = result["rpm"] or rpm_est
                tier = _guess_tier_from_rpm(rpm_est)
            if tier is None and burst["ok"] > 0:
                tier = "Tier 1"  # something works, conservatively assume lowest paid tier
                result["extra"]["tier_reason"] = "burst succeeded, low confidence"

        if tier is None:
            result["status"] = "valid"
            result["tier"] = "Unknown"
            result["extra"].setdefault("tier_reason", "no rate-limit signal from any source")
        else:
            result["status"] = "valid"
            result["tier"] = tier

    return result
