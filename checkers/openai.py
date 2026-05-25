"""OpenAI key checker — tier via TPM/RPM headers + burst RPM fallback +
gpt-5.5 / gpt-image-2 / sora-2 access probe."""
import asyncio
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

# Candidate model IDs to probe access for image/video/chat.
IMAGE_PROBE_IDS = ["gpt-image-2", "gpt-image-1"]
VIDEO_PROBE_IDS = ["sora-2", "sora-2-pro", "sora-1.0", "sora"]
CHAT_PROBE_IDS = ["gpt-5.5", "gpt-5.5-mini", "gpt-5", "gpt-5-mini"]


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


async def _probe_model_access(client: httpx.AsyncClient, key: str, candidates: list[str], available: set[str]) -> dict:
    """For each candidate, test access. Prefer the list response, then a minimal probe call."""
    out = {}
    hdrs = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    for mid in candidates:
        if mid in available:
            out[mid] = True
        else:
            # cheapest probe: retrieve model
            try:
                resp = await client.get(f"{BASE}/v1/models/{mid}", headers=hdrs, timeout=15.0)
                if resp.status_code == 200:
                    out[mid] = True
                elif resp.status_code in (404, 403):
                    out[mid] = False
                elif resp.status_code == 401:
                    out[mid] = False
                else:
                    out[mid] = False
            except Exception:
                out[mid] = False
    return out


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

        result["extra"]["models_count"] = len(models)
        result["extra"]["org_id"] = org_id
        result["extra"]["models_preview"] = models[:30]

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

        # Probe image + video + chat access concurrently (use known models list when possible)
        available = set(models)
        img_task = _probe_model_access(client, key, IMAGE_PROBE_IDS, available)
        vid_task = _probe_model_access(client, key, VIDEO_PROBE_IDS, available)
        chat_task = _probe_model_access(client, key, CHAT_PROBE_IDS, available)
        img_access, vid_access, chat_access = await asyncio.gather(img_task, vid_task, chat_task)
        result["extra"]["image_access"] = img_access
        result["extra"]["video_access"] = vid_access
        result["extra"]["chat_access"] = chat_access
        result["extra"]["has_gpt_image_2"] = bool(img_access.get("gpt-image-2"))
        result["extra"]["has_sora_2"] = bool(vid_access.get("sora-2") or vid_access.get("sora-2-pro"))
        result["extra"]["has_gpt_5_5"] = bool(chat_access.get("gpt-5.5") or chat_access.get("gpt-5.5-mini"))

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
