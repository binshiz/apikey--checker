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
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import db

ADMIN_KEY = os.environ.get("ADMIN_KEY", "bingxujingAb")


def require_auth(request: Request):
    token = request.headers.get("X-Admin-Key", "")
    if token != ADMIN_KEY:
        raise HTTPException(401, "unauthorized")
from detector import detect_provider, short_key
from checkers import openai as openai_checker
from checkers import anthropic as anthropic_checker
from checkers import gemini as gemini_checker
from proxy_pool import get_pool, ProxyPool


CHECKERS = {
    "openai": openai_checker.check,
    "anthropic": anthropic_checker.check,
    "gemini": gemini_checker.check,
}

ROOT = os.path.dirname(os.path.abspath(__file__))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
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

async def check_one_key(key_id: int, job_id: int | None, sem: asyncio.Semaphore, use_proxy: bool = False):
    async with sem:
        info = db.get_key(key_id)
        if not info:
            if job_id:
                db.bump_job(job_id)
            return
        provider = info["provider"]
        if not provider or provider not in CHECKERS:
            db.save_result(key_id, {
                "status": "invalid",
                "error": f"unknown provider format",
                "extra": {},
            })
            if job_id:
                db.bump_job(job_id)
            return

        db.set_key_status(key_id, "checking")

        # Pick a proxy from the pool if enabled
        proxy = None
        if use_proxy:
            proxy = await get_pool().get_round_robin()

        try:
            if proxy:
                result = await CHECKERS[provider](info["api_key"], proxy=proxy)
            else:
                result = await CHECKERS[provider](info["api_key"])
        except Exception as e:
            result = {"status": "error", "error": f"{type(e).__name__}: {e}", "extra": {}}
            if proxy:
                await get_pool().mark_dead(proxy)
                result["extra"]["proxy_dead"] = proxy

        # If the error looks like a proxy/connection failure, mark it dead
        err_str = (result.get("error") or "").lower()
        if proxy and (result.get("status") in ("error",) or "proxyerror" in err_str or "connect" in err_str or "timeout" in err_str):
            await get_pool().mark_dead(proxy)
            result.setdefault("extra", {})["proxy"] = proxy
            result["extra"]["proxy_dead"] = proxy

        db.save_result(key_id, result)
        if job_id:
            db.bump_job(job_id)


async def run_job(key_ids: list[int], concurrency: int, job_id: int, use_proxy: bool = False):
    sem = asyncio.Semaphore(concurrency)
    tasks = [check_one_key(kid, job_id, sem, use_proxy=use_proxy) for kid in key_ids]
    await asyncio.gather(*tasks, return_exceptions=True)
    db.finish_job(job_id)


def _parse_keys_text(text: str) -> tuple[list[str], dict[str, str | None]]:
    keys = []
    providers = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s in providers:
            continue
        prov = detect_provider(s)
        providers[s] = prov
        keys.append(s)
    return keys, providers


# ────────────────────────────────────────────────────────────────────
# Models
# ────────────────────────────────────────────────────────────────────

class ImportPayload(BaseModel):
    text: str
    concurrency: int = 4
    use_proxy: bool = False


class IdsPayload(BaseModel):
    ids: list[int]
    concurrency: int = 4
    use_proxy: bool = False


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
    keys, providers = _parse_keys_text(payload.text)
    if not keys:
        raise HTTPException(400, "no keys found")

    ids = db.upsert_keys(keys, providers)
    valid_ids = [i for i in ids if i is not None]
    job_id = db.create_job(len(valid_ids), payload.concurrency)
    asyncio.create_task(run_job(valid_ids, max(1, payload.concurrency), job_id, use_proxy=payload.use_proxy))

    breakdown = {}
    for p in providers.values():
        breakdown[p or "unknown"] = breakdown.get(p or "unknown", 0) + 1

    return {"job_id": job_id, "imported": len(valid_ids), "breakdown": breakdown}


@app.post("/api/keys/recheck", dependencies=[Depends(require_auth)])
async def recheck_keys(payload: IdsPayload):
    if not payload.ids:
        raise HTTPException(400, "no ids")
    # Reset status to pending for visibility.
    for kid in payload.ids:
        db.set_key_status(kid, "pending")
    job_id = db.create_job(len(payload.ids), payload.concurrency)
    asyncio.create_task(run_job(payload.ids, max(1, payload.concurrency), job_id, use_proxy=payload.use_proxy))
    return {"job_id": job_id, "queued": len(payload.ids)}


@app.post("/api/keys/delete", dependencies=[Depends(require_auth)])
async def delete_keys(payload: IdsPayload):
    n = db.delete_keys(payload.ids)
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
        r["api_key_short"] = short_key(r["api_key"])
    return {"keys": rows, "count": len(rows)}


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_auth)])
async def get_job(job_id: int):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@app.get("/api/jobs/running", dependencies=[Depends(require_auth)])
async def running_jobs():
    return {"jobs": db.get_running_jobs()}


@app.get("/api/export", dependencies=[Depends(require_auth)])
async def export_keys(format: str = "txt", provider: str | None = None, tier: str | None = None):
    rows = db.list_keys(provider=provider, tier=tier, status="valid")
    if format == "json":
        return JSONResponse(rows)
    body = "\n".join(r["api_key"] for r in rows)
    return PlainTextResponse(body)


# ────────────────────────────────────────────────────────────────────
# Vault — persistent store of verified-valid keys
# ────────────────────────────────────────────────────────────────────

class NotePayload(BaseModel):
    note: str


@app.get("/api/vault", dependencies=[Depends(require_auth)])
async def vault_list(provider: str | None = None, tier: str | None = None):
    rows = db.list_vault(provider=provider, tier=tier)
    for r in rows:
        if r.get("extra"):
            try:
                r["extra"] = json.loads(r["extra"])
            except Exception:
                r["extra"] = {}
        else:
            r["extra"] = {}
        r["api_key_short"] = short_key(r["api_key"])
    return {"keys": rows, "count": len(rows), "stats": db.vault_stats()}


@app.post("/api/vault/delete", dependencies=[Depends(require_auth)])
async def vault_delete(payload: IdsPayload):
    n = db.delete_vault(payload.ids)
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
    providers = {e["api_key"]: e["provider"] for e in entries}
    key_ids = db.upsert_keys([e["api_key"] for e in entries], providers)
    # Reset to pending so the UI shows them as queued.
    for kid in key_ids:
        db.set_key_status(kid, "pending")
    job_id = db.create_job(len(key_ids), payload.concurrency)
    asyncio.create_task(run_job(key_ids, max(1, payload.concurrency), job_id, use_proxy=payload.use_proxy))
    return {"job_id": job_id, "queued": len(key_ids)}


@app.post("/api/vault/note/{vault_id}", dependencies=[Depends(require_auth)])
async def vault_note(vault_id: int, payload: NotePayload):
    db.update_vault_note(vault_id, payload.note)
    return {"ok": True}


@app.post("/api/vault/inbound", dependencies=[Depends(require_auth)])
async def vault_inbound(payload: VaultInboundPayload):
    if not payload.ids:
        raise HTTPException(400, "no ids")
    if not payload.supplier_name.strip():
        raise HTTPException(400, "supplier_name required")
    try:
        return db.inbound_from_vault(
            payload.ids,
            payload.supplier_name,
            total_cost=payload.total_cost,
            tags=payload.tags,
            note=payload.note,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/vault/export")
async def vault_export(format: str = "txt", provider: str | None = None, tier: str | None = None, token: str | None = None, request: Request = None):
    header_key = request.headers.get("X-Admin-Key", "") if request else ""
    if (token or "") != ADMIN_KEY and header_key != ADMIN_KEY:
        raise HTTPException(401, "unauthorized")
    rows = db.list_vault(provider=provider, tier=tier)
    if format == "json":
        return JSONResponse(rows)
    body = "\n".join(r["api_key"] for r in rows)
    return PlainTextResponse(body)


# ────────────────────────────────────────────────────────────────────
# Inventory — formally inbounded keys
# ────────────────────────────────────────────────────────────────────

def _parse_json_field(value: str | None) -> Any:
    if not value:
        return {}
    try:
        return json.loads(value)
    except Exception:
        return {}


def _safe_inventory_row(row: dict, *, include_full_key: bool = False) -> dict:
    row["extra"] = _parse_json_field(row.get("extra"))
    row["api_key_short"] = short_key(row.get("api_key") or "")
    if not include_full_key:
        row.pop("api_key", None)
    return row


def _safe_check_run(row: dict) -> dict:
    row["extra"] = _parse_json_field(row.get("extra"))
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
):
    rows = db.list_inventory(
        provider=provider,
        stock_status=stock_status,
        tier=tier,
        supplier_id=supplier_id,
        batch_id=batch_id,
        risk_flag=risk_flag,
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
    return {"item": item, "check_runs": check_runs, "movements": movements}


@app.post("/api/inventory/recheck", dependencies=[Depends(require_auth)])
async def inventory_recheck(payload: IdsPayload):
    if not payload.ids:
        raise HTTPException(400, "no ids")
    prepared = db.prepare_inventory_recheck(payload.ids)
    key_ids = prepared["key_ids"]
    if not key_ids:
        return {"job_id": None, "queued": 0, "skipped": prepared["skipped"]}
    job_id = db.create_job(len(key_ids), payload.concurrency)
    asyncio.create_task(run_job(key_ids, max(1, payload.concurrency), job_id, use_proxy=payload.use_proxy))
    return {"job_id": job_id, "queued": len(key_ids), "skipped": prepared["skipped"]}


@app.post("/api/inventory/status", dependencies=[Depends(require_auth)])
async def inventory_status(payload: InventoryStatusPayload):
    if not payload.ids:
        raise HTTPException(400, "no ids")
    try:
        return db.update_inventory_status(payload.ids, payload.action, payload.reason)
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
async def inventory_export(payload: InventoryExportPayload):
    if not payload.ids:
        raise HTTPException(400, "no ids")
    rows = db.export_inventory_entries(payload.ids)
    if payload.format == "json":
        return JSONResponse([_safe_inventory_row(dict(r), include_full_key=True) for r in rows])
    if payload.format != "txt":
        raise HTTPException(400, "unsupported format")
    body = "\n".join(r["api_key"] for r in rows)
    return PlainTextResponse(body)


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
