"""Gemini key checker — burst test gemini-2.5-pro to detect RPM tier.

Tiers (per user spec):
  RPM <  300         → T1
  300 <= RPM <= 1300 → T2
  RPM > 1300         → T3

Strategy:
  Send small concurrent requests in ramping waves. Count successful responses
  in the current minute. Stop on first 429. The number of successful 200s
  before the 429 is the measured RPM ceiling.
"""
import asyncio
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


def rpm_to_tier(rpm: int) -> str:
    if rpm < 300:
        return "T1"
    if rpm <= 1300:
        return "T2"
    return "T3"


async def _validate_key(client: httpx.AsyncClient, key: str) -> tuple[bool, str | None, list[str]]:
    """Verify key with a single ListModels call. Return (valid, error, model_ids)."""
    try:
        resp = await client.get(
            f"{BASE}/models",
            params={"key": key},
            timeout=20.0,
        )
    except httpx.TimeoutException:
        return False, "timeout", []
    except Exception as e:
        return False, f"{type(e).__name__}", []

    if resp.status_code in (400, 401, 403):
        try:
            body = resp.json()
            msg = body.get("error", {}).get("message", "")
            if "API key not valid" in msg or "invalid" in msg.lower():
                return False, "invalid", []
            return False, msg[:200] or f"HTTP {resp.status_code}", []
        except Exception:
            return False, f"HTTP {resp.status_code}", []
    if resp.status_code == 429:
        # Already rate-limited at list-models — still valid
        return True, None, []
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}", []
    try:
        data = resp.json().get("models", [])
        ids = sorted([m["name"].replace("models/", "") for m in data if "name" in m])
    except Exception:
        ids = []
    return True, None, ids


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
        # Throttle so we don't blow past the cap.
        wave = min(WAVE_SIZE, cap - sent)
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
            elif s in (400, 404) and ok == 0:
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
        valid, err, models = await _validate_key(client, key)
        if not valid:
            result["status"] = "invalid"
            result["error"] = err
            return result

        result["extra"]["models_count"] = len(models)
        result["extra"]["models_preview"] = models[:30]
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
