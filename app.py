"""FastAPI app — batch API key tier checker.

Routes:
  GET  /                       → UI
  POST /api/keys/import        → bulk import + start checking job
  POST /api/keys/recheck       → re-check selected keys
  POST /api/keys/delete        → delete selected keys
  GET  /api/keys               → list keys (filterable)
  GET  /api/jobs/{id}          → job progress
  GET  /api/jobs/running       → running jobs
"""
import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any, Literal

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import db

ADMIN_KEY = os.environ.get("ADMIN_KEY", "bingxujingAb")


def require_auth(request: Request):
    token = request.headers.get("X-Admin-Key", "")
    if token != ADMIN_KEY:
        raise HTTPException(401, "unauthorized")
from detector import (
    azure_openai_chat_completions_url,
    canonicalize_gcp_service_account,
    detect_provider,
    normalize_key,
    parse_azure_openai_key,
    parse_bedrock_api_key,
    parse_bedrock_key,
    parse_gcp_service_account,
    short_key,
)
from checkers import openai as openai_checker
from checkers import azure_openai as azure_openai_checker
from checkers import anthropic as anthropic_checker
from checkers import gemini as gemini_checker
from checkers import bedrock as bedrock_checker
from checkers import gcp_service_account as gcp_service_account_checker
from checkers import openrouter as openrouter_checker
from proxy_pool import get_pool, ProxyPool


CHECKERS = {
    "openai": openai_checker.check,
    "azure_openai": azure_openai_checker.check,
    "anthropic": anthropic_checker.check,
    "gemini": gemini_checker.check,
    "aws_bedrock": bedrock_checker.check,
    "gcp_service_account": gcp_service_account_checker.check,
    "openrouter": openrouter_checker.check,
}

CHECKER_SUPPORTS_PROXY = {
    "openai": True,
    "azure_openai": True,
    "anthropic": True,
    "gemini": True,
    "aws_bedrock": True,
    "gcp_service_account": True,
    "openrouter": True,
}


def _with_bedrock_gateway_metadata(provider: str | None, extra: Any) -> Any:
    """Materialize mappings for older results without requiring another paid check."""
    if provider != "aws_bedrock" or not isinstance(extra, dict):
        return extra
    mapping, by_region = bedrock_checker.build_gateway_mappings(
        extra.get("region_results")
    )
    if mapping:
        extra["gateway_mapping"] = mapping
        extra["gateway_mappings_by_region"] = by_region
        extra["gateway_mapping_basis"] = "invoke_model_success"
        (
            extra["gateway_primary_model"],
            extra["gateway_primary_regions"],
        ) = bedrock_checker.select_gateway_primary_model(by_region)
        extra["gateway_region_groups"] = (
            bedrock_checker.build_gateway_region_groups(by_region)
        )
    return extra


CheckMode = Literal["quick", "bedrock_deep"]

ROOT = os.path.dirname(os.path.abspath(__file__))


_SOCKS_URL_RE = re.compile(r"\bsocks5h?://[^\s\"']+", re.IGNORECASE)


def _redact_proxy_urls(value: str | None) -> str | None:
    """Remove SOCKS endpoints (including credentials) from persisted/public text."""
    if not value:
        return value
    return _SOCKS_URL_RE.sub("[proxy redacted]", str(value))


def _sanitize_result_extra(value: Any) -> Any:
    """Recursively strip proxy locations while preserving non-secret status flags."""
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            key_name = str(key)
            lowered = key_name.lower()
            if lowered in {"proxy", "proxy_url", "proxy_uri"}:
                continue
            if lowered == "proxy_dead" and isinstance(item, str):
                sanitized[key_name] = True
                continue
            sanitized[key_name] = _sanitize_result_extra(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_result_extra(item) for item in value]
    if isinstance(value, str):
        return _redact_proxy_urls(value)
    return value


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    interrupted = db.cancel_running_jobs()
    if interrupted:
        print(f"[jobs] cancelled {interrupted} interrupted job(s) after restart")
    pool = get_pool()
    if pool.count > 0:
        print(f"[proxy pool] {pool.count} proxies loaded (lazy health check — tested on use)")
    yield


app = FastAPI(title="API Key Tier Detector", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(ROOT, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(ROOT, "templates"))


# ────────────────────────────────────────────────────────────────────
# Background checker
# ────────────────────────────────────────────────────────────────────

def _create_check_job(
    key_ids: list[int],
    concurrency: int,
    mode: CheckMode,
) -> int:
    detail_total = 0
    for key_id in key_ids:
        info = db.get_key(key_id)
        if info and info.get("provider") == "aws_bedrock":
            if parse_bedrock_api_key(info.get("api_key") or "") is not None:
                detail_total += len(bedrock_checker.configured_api_key_regions())
            else:
                detail_total += len(bedrock_checker.configured_regions()) + 1
        else:
            detail_total += 1
    return db.create_job(
        len(key_ids),
        concurrency,
        mode=mode,
        detail_total=detail_total,
        detail_label="准备中" if detail_total else None,
    )


async def check_one_key(
    key_id: int,
    job_id: int | None,
    sem: asyncio.Semaphore,
    use_proxy: bool = False,
    mode: CheckMode = "quick",
):
    async with sem:
        info = db.get_key(key_id)
        if not info:
            if job_id:
                db.bump_job_detail(job_id, "记录不存在")
                db.bump_job(job_id)
            return
        provider = info["provider"]
        if not provider or provider not in CHECKERS:
            db.save_result(key_id, {
                "status": "invalid",
                "error": f"unknown provider format",
                "extra": {},
            }, source="checker")
            if job_id:
                db.bump_job_detail(job_id, "无法识别")
                db.bump_job(job_id)
            return

        db.set_key_status(key_id, "checking")

        # Pick a proxy from the pool if enabled
        proxy = None
        if use_proxy and CHECKER_SUPPORTS_PROXY.get(provider, False):
            proxy = await get_pool().get_round_robin()

        checker = (
            bedrock_checker.deep_check
            if provider == "aws_bedrock" and mode == "bedrock_deep"
            else CHECKERS[provider]
        )
        if use_proxy and CHECKER_SUPPORTS_PROXY.get(provider, False) and not proxy:
            result = {
                "status": "error",
                "error": "SOCKS5 proxy pool has no available proxy",
                "extra": {
                    "proxy_requested": True,
                    "proxy_unavailable": True,
                },
            }
        else:
            try:
                checker_kwargs = {}
                if proxy:
                    checker_kwargs["proxy"] = proxy
                if job_id and provider == "aws_bedrock":
                    async def report_progress(label: str):
                        db.bump_job_detail(job_id, label)

                    checker_kwargs["progress_callback"] = report_progress
                result = await checker(info["api_key"], **checker_kwargs)
            except Exception as e:
                result = {
                    "status": "error",
                    "error": _redact_proxy_urls(f"{type(e).__name__}: {e}"),
                    "extra": {},
                }
                if proxy:
                    await get_pool().mark_dead(proxy)
                    result["extra"]["proxy_dead"] = True

        # If the error looks like a proxy/connection failure, mark it dead
        err_str = (result.get("error") or "").lower()
        proxy_failure = any(
            marker in err_str
            for marker in (
                "proxyerror",
                "proxyconnectionerror",
                "endpointconnectionerror",
                "connecterror",
                "connecttimeout",
                "readtimeout",
                "networkerror",
            )
        )
        if proxy and proxy_failure:
            await get_pool().mark_dead(proxy)
            result.setdefault("extra", {})["proxy_dead"] = True

        result["error"] = _redact_proxy_urls(result.get("error"))
        result["extra"] = _sanitize_result_extra(result.get("extra") or {})

        source = "bedrock_deep" if provider == "aws_bedrock" and mode == "bedrock_deep" else "checker"
        db.save_result(key_id, result, source=source)
        if job_id:
            if provider != "aws_bedrock":
                db.bump_job_detail(job_id, provider or "完成")
            db.bump_job(job_id)


async def run_job(
    key_ids: list[int],
    concurrency: int,
    job_id: int,
    use_proxy: bool = False,
    mode: CheckMode = "quick",
):
    sem = asyncio.Semaphore(concurrency)
    tasks = [
        check_one_key(kid, job_id, sem, use_proxy=use_proxy, mode=mode)
        for kid in key_ids
    ]
    await asyncio.gather(*tasks, return_exceptions=True)
    db.finish_job(job_id)


MAX_GCP_SERVICE_ACCOUNT_FILES = 100


def _gcp_credentials_from_json(value: Any) -> list[str]:
    if isinstance(value, dict):
        documents = [value]
    elif isinstance(value, list):
        if len(value) > MAX_GCP_SERVICE_ACCOUNT_FILES:
            raise ValueError("at most 100 GCP service account credentials are allowed")
        documents = value
    else:
        raise ValueError("GCP service account input must be a JSON object or array")

    credentials = []
    for index, document in enumerate(documents, start=1):
        if not isinstance(document, dict):
            raise ValueError(f"GCP service account item {index} must be a JSON object")
        try:
            credentials.append(canonicalize_gcp_service_account(document))
        except ValueError as exc:
            raise ValueError(f"GCP service account item {index}: {exc}") from exc
    return credentials


def _parse_keys_text(text: str) -> tuple[list[str], dict[str, str | None]]:
    keys = []
    providers = {}
    stripped_text = text.strip()
    if stripped_text.startswith(("{", "[")):
        try:
            json_value = json.loads(stripped_text)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid pasted GCP service account JSON") from exc
        for credential in _gcp_credentials_from_json(json_value):
            if credential in providers:
                continue
            providers[credential] = "gcp_service_account"
            keys.append(credential)
        return keys, providers

    azure_env: dict[str, dict[str, str]] = {}
    ordinary_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        env_match = re.fullmatch(
            r"([A-Za-z][A-Za-z0-9_]*)_(KEY|ENDPOINT)\s*=\s*(.+)",
            stripped,
            re.IGNORECASE,
        )
        if env_match:
            prefix, kind, value = env_match.groups()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1].strip()
            azure_env.setdefault(prefix.upper(), {})[kind.upper()] = value
            continue
        ordinary_lines.append(line)

    for values in azure_env.values():
        if not values.get("ENDPOINT") or not values.get("KEY"):
            continue
        ordinary_lines.append(f'{values["ENDPOINT"]}|{values["KEY"]}')

    for line in ordinary_lines:
        s = normalize_key(line)
        if not s or s.startswith("#"):
            continue
        if s in providers:
            continue
        prov = detect_provider(s)
        providers[s] = prov
        keys.append(s)
    return keys, providers


def _parse_import_credentials(
    text: str,
    service_accounts: list[dict[str, Any]],
) -> tuple[list[str], dict[str, str | None]]:
    if len(service_accounts) > MAX_GCP_SERVICE_ACCOUNT_FILES:
        raise ValueError("at most 100 GCP service account files are allowed")
    keys, providers = _parse_keys_text(text)
    for credential in _gcp_credentials_from_json(service_accounts):
        if credential in providers:
            continue
        providers[credential] = "gcp_service_account"
        keys.append(credential)
    return keys, providers


def _filter_key_ids_for_mode(ids: list[int], mode: CheckMode) -> tuple[list[int], int]:
    unique_ids = list(dict.fromkeys(ids))
    if mode == "quick":
        return unique_ids, 0
    accepted = []
    for key_id in unique_ids:
        row = db.get_key(key_id)
        if row and row.get("provider") == "aws_bedrock":
            accepted.append(key_id)
    return accepted, len(unique_ids) - len(accepted)


def _effective_concurrency(requested: int, mode: CheckMode) -> int:
    upper_bound = 2 if mode == "bedrock_deep" else 32
    return min(max(1, requested), upper_bound)


# ────────────────────────────────────────────────────────────────────
# Models
# ────────────────────────────────────────────────────────────────────

class ImportPayload(BaseModel):
    text: str = ""
    service_accounts: list[dict[str, Any]] = Field(default_factory=list)
    concurrency: int = 4
    use_proxy: bool = False


class IdsPayload(BaseModel):
    ids: list[int]
    concurrency: int = 4
    use_proxy: bool = False
    mode: CheckMode = "quick"


class VaultInboundPayload(BaseModel):
    ids: list[int]
    supplier_name: str
    total_cost: float | None = None
    tags: str | None = None
    note: str | None = None


class InventoryStatusPayload(BaseModel):
    ids: list[int]
    action: str
    reason: str | None = None


class InventoryMetaPayload(BaseModel):
    note: str | None = None
    tags: str | None = None
    risk_flag: str | None = None


class InventoryExportPayload(BaseModel):
    ids: list[int]
    format: str = "txt"


class InventorySalePayload(BaseModel):
    ids: list[int]
    buyer: str
    unit_price: Decimal | None = None
    currency: Literal["CNY"] = "CNY"
    external_ref: str | None = None
    note: str | None = None


class InventoryReturnPayload(BaseModel):
    ids: list[int]
    reason: str


# ────────────────────────────────────────────────────────────────────
# Routes
# ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.post("/api/auth")
async def auth_check(request: Request):
    body = await request.json()
    if body.get("key") == ADMIN_KEY:
        return {"ok": True}
    raise HTTPException(401, "wrong key")


@app.post("/api/keys/import", dependencies=[Depends(require_auth)])
async def import_keys(payload: ImportPayload):
    try:
        keys, providers = _parse_import_credentials(
            payload.text,
            payload.service_accounts,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not keys:
        raise HTTPException(400, "no keys found")

    ids = db.upsert_keys(keys, providers)
    valid_ids = [i for i in ids if i is not None]
    concurrency = _effective_concurrency(payload.concurrency, "quick")
    job_id = _create_check_job(valid_ids, concurrency, mode="quick")
    asyncio.create_task(
        run_job(
            valid_ids,
            concurrency,
            job_id,
            use_proxy=payload.use_proxy,
            mode="quick",
        )
    )

    breakdown = {}
    for p in providers.values():
        breakdown[p or "unknown"] = breakdown.get(p or "unknown", 0) + 1

    return {"job_id": job_id, "imported": len(valid_ids), "breakdown": breakdown}


@app.post("/api/keys/recheck", dependencies=[Depends(require_auth)])
async def recheck_keys(payload: IdsPayload):
    if not payload.ids:
        raise HTTPException(400, "no ids")
    key_ids, skipped = _filter_key_ids_for_mode(payload.ids, payload.mode)
    if not key_ids:
        return {"job_id": None, "queued": 0, "skipped": skipped}
    # Reset status to pending for visibility.
    for kid in key_ids:
        db.set_key_status(kid, "pending")
    concurrency = _effective_concurrency(payload.concurrency, payload.mode)
    job_id = _create_check_job(key_ids, concurrency, mode=payload.mode)
    asyncio.create_task(
        run_job(
            key_ids,
            concurrency,
            job_id,
            use_proxy=payload.use_proxy,
            mode=payload.mode,
        )
    )
    return {"job_id": job_id, "queued": len(key_ids), "skipped": skipped}


@app.post("/api/keys/delete", dependencies=[Depends(require_auth)])
async def delete_keys(payload: IdsPayload, request: Request):
    n = db.delete_keys(payload.ids)
    _audit_request(
        request,
        action="bulk_delete",
        target_type="keys",
        metadata={"requested": len(payload.ids), "deleted": n},
    )
    return {"deleted": n}


@app.get("/api/keys", dependencies=[Depends(require_auth)])
async def get_keys(provider: str | None = None, status: str | None = None, tier: str | None = None):
    rows = db.list_keys(provider=provider, status=status, tier=tier)
    for r in rows:
        if r.get("extra"):
            try:
                r["extra"] = json.loads(r["extra"])
            except Exception:
                r["extra"] = {}
        else:
            r["extra"] = {}
        r["extra"] = _with_bedrock_gateway_metadata(
            r.get("provider"),
            _sanitize_result_extra(r["extra"]),
        )
        r["error"] = _redact_proxy_urls(r.get("error"))
        api_key = r.pop("api_key", "")
        r["api_key_short"] = short_key(api_key)
    return {"keys": rows, "count": len(rows)}


@app.get("/api/jobs/running", dependencies=[Depends(require_auth)])
async def running_jobs():
    return {"jobs": db.get_running_jobs()}


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_auth)])
async def get_job(job_id: int):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@app.post("/api/keys/export", dependencies=[Depends(require_auth)])
async def export_keys(payload: InventoryExportPayload, request: Request):
    if not payload.ids:
        raise HTTPException(400, "no ids")
    rows = db.get_key_entries(payload.ids)
    _audit_request(
        request,
        action="full_key_export",
        target_type="keys",
        metadata={"count": len(rows), "format": payload.format},
    )
    return _secret_export_response(rows, payload.format)


# ────────────────────────────────────────────────────────────────────
# Vault — persistent store of verified-valid keys
# ────────────────────────────────────────────────────────────────────

class NotePayload(BaseModel):
    note: str


@app.get("/api/vault", dependencies=[Depends(require_auth)])
async def vault_list(
    provider: str | None = None,
    tier: str | None = None,
    legacy_sale_candidate: bool = False,
):
    rows = db.list_vault(
        provider=provider,
        tier=tier,
        legacy_sale_candidate=legacy_sale_candidate,
    )
    for r in rows:
        if r.get("extra"):
            try:
                r["extra"] = json.loads(r["extra"])
            except Exception:
                r["extra"] = {}
        else:
            r["extra"] = {}
        r["extra"] = _with_bedrock_gateway_metadata(
            r.get("provider"),
            _sanitize_result_extra(r["extra"]),
        )
        api_key = r.pop("api_key", "")
        r["api_key_short"] = short_key(api_key)
    return {"keys": rows, "count": len(rows), "stats": db.vault_stats()}


@app.post("/api/vault/delete", dependencies=[Depends(require_auth)])
async def vault_delete(payload: IdsPayload, request: Request):
    n = db.delete_vault(payload.ids)
    _audit_request(
        request,
        action="bulk_delete",
        target_type="vault",
        metadata={"requested": len(payload.ids), "deleted": n},
    )
    return {"deleted": n}


@app.post("/api/vault/recheck", dependencies=[Depends(require_auth)])
async def vault_recheck(payload: IdsPayload):
    """Recheck vault entries: insert into main keys table (if missing), then run job.
    The auto-vault on valid status will refresh vault entries."""
    if not payload.ids:
        raise HTTPException(400, "no ids")
    entries = db.get_vault_entries(payload.ids)
    if not entries:
        raise HTTPException(404, "no vault entries")
    skipped = 0
    if payload.mode == "bedrock_deep":
        accepted = [entry for entry in entries if entry.get("provider") == "aws_bedrock"]
        skipped = len(entries) - len(accepted)
        entries = accepted
    if not entries:
        return {"job_id": None, "queued": 0, "skipped": skipped}
    providers = {e["api_key"]: e["provider"] for e in entries}
    key_ids = db.upsert_keys([e["api_key"] for e in entries], providers)
    key_ids, post_skipped = _filter_key_ids_for_mode(key_ids, payload.mode)
    skipped += post_skipped
    if not key_ids:
        return {"job_id": None, "queued": 0, "skipped": skipped}
    # Reset to pending so the UI shows them as queued.
    for kid in key_ids:
        db.set_key_status(kid, "pending")
    concurrency = _effective_concurrency(payload.concurrency, payload.mode)
    job_id = _create_check_job(key_ids, concurrency, mode=payload.mode)
    asyncio.create_task(
        run_job(
            key_ids,
            concurrency,
            job_id,
            use_proxy=payload.use_proxy,
            mode=payload.mode,
        )
    )
    return {"job_id": job_id, "queued": len(key_ids), "skipped": skipped}


@app.post("/api/vault/note/{vault_id}", dependencies=[Depends(require_auth)])
async def vault_note(vault_id: int, payload: NotePayload):
    db.update_vault_note(vault_id, payload.note)
    return {"ok": True}


@app.post("/api/vault/inbound", dependencies=[Depends(require_auth)])
async def vault_inbound(payload: VaultInboundPayload, request: Request):
    if not payload.ids:
        raise HTTPException(400, "no ids")
    if not payload.supplier_name.strip():
        raise HTTPException(400, "supplier_name required")
    try:
        result = db.inbound_from_vault(
            payload.ids,
            payload.supplier_name,
            total_cost=payload.total_cost,
            tags=payload.tags,
            note=payload.note,
        )
        _audit_request(
            request,
            action="inventory_inbound",
            target_type="inventory",
            metadata={
                "requested": len(payload.ids),
                "inbounded": result.get("inbounded", 0),
                "skipped": result.get("skipped", 0),
                "batch_id": (result.get("batch") or {}).get("id"),
            },
        )
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/vault/export", dependencies=[Depends(require_auth)])
async def vault_export(payload: InventoryExportPayload, request: Request):
    if not payload.ids:
        raise HTTPException(400, "no ids")
    rows = db.get_vault_entries(payload.ids)
    _audit_request(
        request,
        action="full_key_export",
        target_type="vault",
        metadata={"count": len(rows), "format": payload.format},
    )
    return _secret_export_response(rows, payload.format)


# ────────────────────────────────────────────────────────────────────
# Inventory — formally inbounded keys
# ────────────────────────────────────────────────────────────────────

def _audit_request(
    request: Request,
    *,
    action: str,
    target_type: str,
    target_id: int | None = None,
    metadata: dict | None = None,
):
    client_host = request.client.host if request.client else None
    db.record_audit(
        action=action,
        target_type=target_type,
        target_id=target_id,
        metadata=metadata,
        actor="admin",
        ip_address=client_host,
    )


_AWS_REGION_EXPORT_RE = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+)+-\d+$")


def _bedrock_export_regions(row: dict) -> list[str]:
    """Read verified regions for the row's specific Bedrock credential type.

    SigV4 exports list Fable 5 regions first, then regions where another Claude
    model was successfully invoked.
    """
    raw_extra = row.get("extra")
    extra = raw_extra if isinstance(raw_extra, dict) else _parse_json_field(raw_extra)
    if not isinstance(extra, dict):
        return []

    summary = extra.get("model_summary")
    summary = summary if isinstance(summary, dict) else {}
    api_key = str(row.get("api_key") or "")
    is_bearer_api_key = parse_bedrock_api_key(api_key) is not None
    if is_bearer_api_key:
        values = summary.get("authorized_regions")
    else:
        _, by_region, _, _ = _bedrock_gateway_support(row)
        groups = bedrock_checker.build_gateway_region_groups(by_region)
        values = [
            region
            for group in groups
            for region in group["regions"]
        ]
        if not values:
            fable_summary = summary.get("fable_5")
            fable_regions = (
                fable_summary.get("successful_regions")
                if isinstance(fable_summary, dict)
                else None
            )
            values = (
                fable_regions
                if isinstance(fable_regions, list) and fable_regions
                else summary.get("successful_regions")
            )

    # Compatibility fallback for older persisted results that recorded
    # successful invocations but did not yet materialize the summary field.
    if not is_bearer_api_key and (not isinstance(values, list) or not values):
        region_results = extra.get("region_results")
        if isinstance(region_results, dict):
            values = [
                region
                for region, result in region_results.items()
                if isinstance(result, dict)
                and any(
                    invocation.get("status") == "success"
                    for invocation in result.get("invocations", [])
                    if isinstance(invocation, dict)
                )
            ]

    regions: list[str] = []
    seen: set[str] = set()
    if not isinstance(values, list):
        return regions
    for value in values:
        region = str(value).strip().lower()
        if region in seen or not _AWS_REGION_EXPORT_RE.fullmatch(region):
            continue
        seen.add(region)
        regions.append(region)
    return regions


def _secret_export_lines(row: dict) -> list[str]:
    api_key = str(row.get("api_key") or "")
    if row.get("provider") == "gcp_service_account":
        credential = parse_gcp_service_account(api_key)
        if credential is not None:
            return [canonicalize_gcp_service_account(credential)]
    if row.get("provider") == "azure_openai":
        azure_credential = parse_azure_openai_key(api_key)
        if azure_credential is not None:
            endpoint, secret = azure_credential
            url = azure_openai_chat_completions_url(endpoint)
            if url:
                return [f"{url}|{secret}"]
    if row.get("provider") != "aws_bedrock":
        return [api_key]

    bearer_api_key = parse_bedrock_api_key(api_key)
    regions = _bedrock_export_regions(row)
    if bearer_api_key is not None:
        if not regions:
            return [bearer_api_key]
        return [f"{bearer_api_key}|{region}" for region in regions]

    credentials = parse_bedrock_key(api_key)
    if credentials is None or not regions:
        return [api_key]
    canonical_key = "|".join(credentials)
    return [f"{canonical_key}|{region}" for region in regions]


def _bedrock_gateway_support(
    row: dict,
) -> tuple[dict[str, str], dict[str, dict[str, str]], str | None, list[str]]:
    """Return sanitized overall and per-region runtime-proven model support."""
    if row.get("provider") != "aws_bedrock":
        return {}, {}, None, []
    raw_extra = row.get("extra")
    extra = raw_extra if isinstance(raw_extra, dict) else _parse_json_field(raw_extra)
    if not isinstance(extra, dict):
        return {}, {}, None, []

    mapping, by_region = bedrock_checker.build_gateway_mappings(
        extra.get("region_results")
    )

    def safe_mapping(value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        sanitized: dict[str, str] = {}
        for alias, target in value.items():
            alias_value = str(alias).strip().lower()
            target_value = str(target).strip()
            if (
                re.fullmatch(
                    r"claude-(?:fable|opus)-[a-z0-9]+(?:-[a-z0-9]+)*",
                    alias_value,
                )
                and bedrock_checker.gateway_model_alias(target_value) == alias_value
            ):
                sanitized[alias_value] = target_value
        return dict(sorted(sanitized.items()))

    # Compatibility for compacted records that retain generated mappings but
    # no longer carry every invocation result.
    if not mapping:
        mapping = safe_mapping(extra.get("gateway_mapping"))
    if not by_region:
        stored_by_region = extra.get("gateway_mappings_by_region")
        if isinstance(stored_by_region, dict):
            for raw_region, raw_mapping in stored_by_region.items():
                region = str(raw_region).strip().lower()
                region_mapping = safe_mapping(raw_mapping)
                if (
                    _AWS_REGION_EXPORT_RE.fullmatch(region)
                    and region_mapping
                ):
                    by_region[region] = region_mapping

    primary_model, primary_regions = (
        bedrock_checker.select_gateway_primary_model(by_region)
    )
    return mapping, dict(sorted(by_region.items())), primary_model, primary_regions


def _bedrock_gateway_mapping(row: dict) -> dict[str, str]:
    """Return only model routes proven by successful Bedrock runtime calls."""
    mapping, _, _, _ = _bedrock_gateway_support(row)
    return mapping


def _secret_export_bundle_block(row: dict) -> str:
    key_text = "\n".join(_secret_export_lines(row))
    mapping, by_region, _, _ = _bedrock_gateway_support(row)
    if not mapping:
        return key_text

    # Group Fable 5 regions before all other callable regions. Each JSON object
    # contains only alias/target pairs proven identically across every
    # credential line immediately above it.
    if (
        row.get("provider") == "aws_bedrock"
        and parse_bedrock_api_key(str(row.get("api_key") or "")) is None
        and parse_bedrock_key(str(row.get("api_key") or "")) is not None
        and by_region
    ):
        credentials = parse_bedrock_key(str(row.get("api_key") or ""))
        canonical_key = "|".join(credentials) if credentials else ""
        groups = bedrock_checker.build_gateway_region_groups(by_region)
        blocks: list[str] = []
        for group in groups:
            regions = group["regions"]
            region_mapping = group["mapping"]
            credential_lines = [
                f"{canonical_key}|{region}"
                for region in regions
            ]
            credential_text = "\n".join(credential_lines)
            mapping_payload: dict | list = region_mapping
            if not region_mapping and group.get("route_groups"):
                mapping_payload = {
                    "route_groups": group["route_groups"],
                }
            mapping_json = json.dumps(
                mapping_payload,
                ensure_ascii=False,
                indent=2,
            )
            blocks.append(f"{credential_text}\n\n{mapping_json}")
        if blocks:
            return "\n\n".join(blocks)

    mapping_json = json.dumps(mapping, ensure_ascii=False, indent=2)
    return f"{key_text}\n\n{mapping_json}"


def _secret_export_response(rows: list[dict], format: str):
    if format == "json":
        return JSONResponse(rows)
    if format == "bundle":
        blocks = [_secret_export_bundle_block(row) for row in rows]
        return PlainTextResponse("\n\n".join(blocks))
    if format != "txt":
        raise HTTPException(400, "unsupported format")
    lines = [line for row in rows for line in _secret_export_lines(row)]
    return PlainTextResponse("\n".join(lines))


def _unit_price_to_minor(value: Decimal | None) -> int | None:
    if value is None:
        return None
    if not value.is_finite():
        raise HTTPException(400, "unit_price must be finite")
    if value < 0:
        raise HTTPException(400, "unit_price must be non-negative")
    cents = value * 100
    if cents != cents.to_integral_value():
        raise HTTPException(400, "unit_price supports at most 2 decimal places")
    if cents > 9_999_999_999_999:
        raise HTTPException(400, "unit_price is too large")
    return int(cents)

def _parse_json_field(value: str | None) -> Any:
    if not value:
        return {}
    try:
        return json.loads(value)
    except Exception:
        return {}


def _safe_inventory_row(row: dict, *, include_full_key: bool = False) -> dict:
    row["extra"] = _with_bedrock_gateway_metadata(
        row.get("provider"),
        _sanitize_result_extra(_parse_json_field(row.get("extra"))),
    )
    row["error"] = _redact_proxy_urls(row.get("error"))
    row["api_key_short"] = short_key(row.get("api_key") or "")
    if not include_full_key:
        row.pop("api_key", None)
    return row


def _safe_check_run(row: dict) -> dict:
    row["extra"] = _with_bedrock_gateway_metadata(
        row.get("provider"),
        _sanitize_result_extra(_parse_json_field(row.get("extra"))),
    )
    row["error"] = _redact_proxy_urls(row.get("error"))
    row.pop("proxy", None)
    return row


def _safe_movement(row: dict) -> dict:
    row["metadata"] = _parse_json_field(row.get("metadata"))
    return row


@app.get("/api/inventory", dependencies=[Depends(require_auth)])
async def inventory_list(
    provider: str | None = None,
    stock_status: str | None = None,
    tier: str | None = None,
    supplier_id: int | None = None,
    batch_id: int | None = None,
    risk_flag: str | None = None,
    sale_view: Literal["all", "sellable", "sold"] = "all",
):
    rows = db.list_inventory(
        provider=provider,
        stock_status=stock_status,
        tier=tier,
        supplier_id=supplier_id,
        batch_id=batch_id,
        risk_flag=risk_flag,
        sale_view=sale_view,
    )
    for r in rows:
        _safe_inventory_row(r)
    return {"keys": rows, "count": len(rows), "stats": db.inventory_stats()}


@app.get("/api/inventory/{inventory_id}", dependencies=[Depends(require_auth)])
async def inventory_detail(inventory_id: int):
    detail = db.get_inventory_detail(inventory_id)
    if not detail:
        raise HTTPException(404, "inventory item not found")
    item = _safe_inventory_row(detail["item"])
    check_runs = [_safe_check_run(r) for r in detail["check_runs"]]
    movements = [_safe_movement(r) for r in detail["movements"]]
    sales = detail.get("sales", [])
    return {"item": item, "check_runs": check_runs, "movements": movements, "sales": sales}


@app.post("/api/inventory/recheck", dependencies=[Depends(require_auth)])
async def inventory_recheck(payload: IdsPayload):
    if not payload.ids:
        raise HTTPException(400, "no ids")
    inventory_ids = list(dict.fromkeys(payload.ids))
    mode_skipped = 0
    if payload.mode == "bedrock_deep":
        rows = db.export_inventory_entries(inventory_ids)
        accepted_ids = [row["id"] for row in rows if row.get("provider") == "aws_bedrock"]
        mode_skipped = len(inventory_ids) - len(accepted_ids)
        inventory_ids = accepted_ids
    if not inventory_ids:
        return {"job_id": None, "queued": 0, "skipped": mode_skipped}
    prepared = db.prepare_inventory_recheck(inventory_ids)
    key_ids = prepared["key_ids"]
    if not key_ids:
        return {"job_id": None, "queued": 0, "skipped": prepared["skipped"] + mode_skipped}
    concurrency = _effective_concurrency(payload.concurrency, payload.mode)
    job_id = _create_check_job(key_ids, concurrency, mode=payload.mode)
    asyncio.create_task(
        run_job(
            key_ids,
            concurrency,
            job_id,
            use_proxy=payload.use_proxy,
            mode=payload.mode,
        )
    )
    return {
        "job_id": job_id,
        "queued": len(key_ids),
        "skipped": prepared["skipped"] + mode_skipped,
    }


@app.post("/api/inventory/status", dependencies=[Depends(require_auth)])
async def inventory_status(payload: InventoryStatusPayload, request: Request):
    if not payload.ids:
        raise HTTPException(400, "no ids")
    try:
        result = db.update_inventory_status(payload.ids, payload.action, payload.reason)
        _audit_request(
            request,
            action=f"inventory_{payload.action}",
            target_type="inventory",
            metadata={
                "requested": len(payload.ids),
                "updated": result.get("updated", 0),
                "skipped": result.get("skipped", 0),
            },
        )
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/inventory/meta/{inventory_id}", dependencies=[Depends(require_auth)])
async def inventory_meta(inventory_id: int, payload: InventoryMetaPayload):
    ok = db.update_inventory_meta(
        inventory_id,
        note=payload.note,
        tags=payload.tags,
        risk_flag=payload.risk_flag,
    )
    if not ok:
        raise HTTPException(404, "inventory item not found")
    return {"ok": True}


@app.post("/api/inventory/export", dependencies=[Depends(require_auth)])
async def inventory_export(payload: InventoryExportPayload, request: Request):
    if not payload.ids:
        raise HTTPException(400, "no ids")
    rows = db.export_inventory_entries(payload.ids)
    _audit_request(
        request,
        action="full_key_export",
        target_type="inventory",
        metadata={"count": len(rows), "format": payload.format},
    )
    if payload.format == "json":
        return JSONResponse([_safe_inventory_row(dict(r), include_full_key=True) for r in rows])
    return _secret_export_response(rows, payload.format)


@app.post("/api/inventory/sell", dependencies=[Depends(require_auth)])
async def inventory_sell(payload: InventorySalePayload, request: Request):
    if not payload.ids:
        raise HTTPException(400, "no ids")
    buyer = payload.buyer.strip()
    if not buyer:
        raise HTTPException(400, "buyer required")
    if len(buyer) > 200:
        raise HTTPException(400, "buyer is too long")
    if payload.external_ref and len(payload.external_ref.strip()) > 200:
        raise HTTPException(400, "external_ref is too long")
    if payload.note and len(payload.note.strip()) > 2000:
        raise HTTPException(400, "note is too long")
    try:
        result = db.sell_inventory(
            payload.ids,
            buyer=buyer,
            unit_price_minor=_unit_price_to_minor(payload.unit_price),
            currency=payload.currency,
            external_ref=(payload.external_ref or "").strip() or None,
            note=(payload.note or "").strip() or None,
        )
    except db.InventoryConflictError as exc:
        raise HTTPException(409, str(exc))
    _audit_request(
        request,
        action="inventory_sell",
        target_type="inventory",
        metadata={"count": result.get("sold", 0), "sale_ids": result.get("sale_ids", [])},
    )
    return result


@app.post("/api/inventory/return", dependencies=[Depends(require_auth)])
async def inventory_return(payload: InventoryReturnPayload, request: Request):
    if not payload.ids:
        raise HTTPException(400, "no ids")
    reason = payload.reason.strip()
    if not reason:
        raise HTTPException(400, "reason required")
    if len(reason) > 1000:
        raise HTTPException(400, "reason is too long")
    try:
        result = db.return_inventory(payload.ids, reason=reason)
    except db.InventoryConflictError as exc:
        raise HTTPException(409, str(exc))
    _audit_request(
        request,
        action="inventory_return",
        target_type="inventory",
        metadata={"count": result.get("returned", 0), "sale_ids": result.get("sale_ids", [])},
    )
    return result


@app.post("/api/sales/export", dependencies=[Depends(require_auth)])
async def sales_export(payload: InventoryExportPayload, request: Request):
    if not payload.ids:
        raise HTTPException(400, "no ids")
    rows = db.export_sold_entries(payload.ids)
    _audit_request(
        request,
        action="full_key_export",
        target_type="sales",
        metadata={"count": len(rows), "format": payload.format},
    )
    return _secret_export_response(rows, payload.format)


# ────────────────────────────────────────────────────────────────────
# Proxy pool status
# ────────────────────────────────────────────────────────────────────

@app.get("/api/proxy/status", dependencies=[Depends(require_auth)])
async def proxy_status():
    pool = get_pool()
    s = pool.stats()
    return {
        **s,
        "proxies_txt": "data/proxies.txt",
    }


@app.post("/api/proxy/reload", dependencies=[Depends(require_auth)])
async def proxy_reload():
    from proxy_pool import reload_pool
    reload_pool()
    pool = get_pool()
    await pool.health_check_all(concurrency=20)
    return {"reloaded": True, **pool.stats()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8787)
