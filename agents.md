# agents.md

## Project Direction

这个项目当前是一个 API Key 检测工具，但长期目标不是只做“可用性检测”。后续应演进为一个围绕 API Key 的进销存管理系统，覆盖：

- 进货/入库：记录 Key 来源、供应商、批次、成本、采购时间、采购备注。
- 库存：维护 Key 的可用状态、服务商、等级、额度、模型权限、健康检查、库存分层和风险标记。
- 销售/出库：记录客户、订单、交付、售出状态、出库时间、售价、售后/退换/禁用。
- 复检/风控：持续检测 Key 是否有效、是否降级、是否无额度、是否被封禁。
- 报表：统计库存数量、有效率、采购成本、销售收入、利润、供应商质量。

当前已有的检测能力是未来库存系统的“质检/验货”模块。不要把 `vault` 简单理解为最终产品形态，它更像现阶段的“有效 Key 临时库”，以后应逐步升级为正式库存表和库存流水。

## Current Tech Stack

- Backend: Python 3.12, FastAPI, Uvicorn, Pydantic, httpx.
- Frontend: plain HTML/CSS/JavaScript, served by FastAPI.
- Storage: SQLite at `data/keys.db`, initialized by `db.py`.
- Async jobs: `asyncio` background tasks with configurable concurrency.
- Proxy support: optional SOCKS5 proxy pool from `data/proxies.txt`.
- Tests: Python `unittest` under `tests/`.
- Deployment: Dockerfile and docker-compose, app port `8787`.

The app has no frontend build tool, no JS package manager, and no ORM.

## Run Commands

Local development:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

Open:

```text
http://127.0.0.1:8787/
```

Docker:

```bash
docker compose up --build
```

Run tests:

```bash
.venv/bin/python -m unittest
```

Check SOCKS5 proxies:

```bash
.venv/bin/python scripts/check_socks5_proxies.py
```

## Current Project Structure

```text
.
├── app.py                         FastAPI app, auth, routes, async job runner
├── db.py                          SQLite schema, CRUD, vault/job persistence
├── detector.py                    Provider detection and short key display helper
├── proxy_pool.py                  SOCKS5 proxy parsing, round-robin pool, health checks
├── requirements.txt               Python runtime dependencies
├── Dockerfile                     Python 3.12 slim image, uvicorn entrypoint
├── docker-compose.yml             Local container service on port 8787
├── checkers/
│   ├── openai.py                  OpenAI key validation, model summary, tier inference
│   ├── anthropic.py               Anthropic validation, model summary, tier inference
│   └── gemini.py                  Gemini validation and RPM burst tier check
├── templates/
│   └── index.html                 Single-page UI shell
├── static/
│   ├── app.js                     Browser-side state, API calls, table/vault UI
│   └── style.css                  Neo-brutalism UI styling
├── scripts/
│   └── check_socks5_proxies.py    CLI for validating proxy lists
├── tests/
│   ├── test_openai_models.py      Unit tests for OpenAI model summary behavior
│   └── test_anthropic_models.py   Unit tests for Anthropic model summary behavior
└── data/                          Runtime data; ignored because it may contain secrets
```

## Current Backend Flow

`app.py` is the central coordinator:

- Serves the UI at `GET /`.
- Protects API routes with `X-Admin-Key`.
- Imports pasted keys through `POST /api/keys/import`.
- Uses `detector.detect_provider()` to classify each key.
- Stores keys and creates jobs through `db.py`.
- Runs background checks with `asyncio.create_task()`.
- Dispatches each key to the right checker via the `CHECKERS` map.
- Optionally pulls a SOCKS5 proxy from `proxy_pool.get_pool()`.
- Saves results with `db.save_result()`.
- Auto-vaults valid keys through database logic.

Main API groups:

- `/api/keys/*`: import, list, recheck, delete, export checked keys.
- `/api/jobs/*`: poll running/background job progress.
- `/api/vault/*`: list, recheck, delete, note, export verified valid keys.
- `/api/proxy/*`: inspect and reload the SOCKS5 proxy pool.

## Current Data Model

`db.py` currently creates three SQLite tables:

- `keys`: all imported API keys and latest check result.
- `vault`: persistent store of verified-valid keys, refreshed on every valid check.
- `jobs`: background job status, total count, completed count, concurrency.

The database path is:

```text
data/keys.db
```

`data/` is runtime state and may contain API keys, proxy credentials, and generated SQLite files. Do not commit or casually print its contents.

## Future Domain Model

Long-term inventory work should introduce explicit business entities instead of overloading `keys` and `vault` forever:

- `suppliers`: supplier/source records, contact method, quality notes.
- `purchase_batches`: inbound batch, supplier, cost, quantity, purchase time, operator note.
- `api_key_inventory`: one row per actual Key, provider, tier, health, ownership, stock state.
- `check_runs`: every quality-check/recheck attempt and its raw summarized result.
- `stock_movements`: append-only inventory ledger for inbound, reserve, sale, return, disable, delete.
- `customers`: buyer/client records.
- `orders`: sales orders, status, revenue, payment note, delivery note.
- `order_items`: delivered API keys or key allocations under an order.
- `audit_logs`: sensitive operations, actor, timestamp, action, target.

Suggested inventory states:

- `pending_check`: imported but not checked.
- `in_stock`: valid and available for sale/use.
- `reserved`: temporarily locked for an order.
- `sold`: delivered to a customer.
- `returned`: returned or reversed after sale.
- `no_quota`: valid format/account but currently no quota.
- `invalid`: revoked, malformed, or unusable.
- `quarantined`: suspicious, unstable, duplicate, or needs manual review.
- `archived`: no longer active but kept for audit.

Any future schema migration must preserve existing `keys`, `vault`, and `jobs` data.

## Provider Checkers

Each checker exports:

```python
async def check(key: str, proxy: str | None = None) -> dict:
    ...
```

The returned dict is consumed by `db.save_result()` and should use this shape:

```python
{
    "status": "valid|invalid|no_quota|error",
    "tier": str | None,
    "rpm": int | None,
    "tpm": int | None,
    "error": str | None,
    "extra": dict,
}
```

Checker responsibilities:

- `checkers/openai.py`
  - Lists `/v1/models`.
  - Builds a UI-friendly supported-model summary.
  - Reads OpenAI rate-limit headers when available.
  - Maps TPM/RPM signals to tiers.
  - Uses a conservative burst probe only as a fallback.

- `checkers/anthropic.py`
  - Validates via `/v1/messages`.
  - Reads Anthropic rate-limit headers when available.
  - Lists `/v1/models` for target model availability.
  - Falls back to burst testing when headers are missing.

- `checkers/gemini.py`
  - Validates via the Gemini model list endpoint.
  - Burst-tests `gemini-2.5-pro`, with `gemini-2.5-flash` fallback.
  - Maps measured RPM to T1/T2/T3.

The checker layer should remain provider-specific and should not contain purchasing, customer, order, or accounting logic.

## Frontend Flow

The frontend is a single browser page:

- `templates/index.html` defines the auth overlay, check view, vault view, filters, tables, and controls.
- `static/app.js` owns all client state and uses `fetch()` against `/api/*`.
- Admin auth is stored in `sessionStorage` and sent as `X-Admin-Key`.
- Job progress is polled every 1.5 seconds while a job is running.
- The UI has two current tabs:
  - Check: import keys, filter results, copy/recheck/delete selected keys.
  - Vault: manage verified valid keys, notes, export, and recheck.
- `static/style.css` uses a neo-brutalism visual style with CSS custom properties.

Future UI navigation should likely become:

- Dashboard: key metrics, inventory health, alerts.
- Inbound: purchase batches, import, supplier/cost metadata.
- Inventory: stock list, status, tier, provider, health, reserve/sell operations.
- Sales: customers, orders, delivery/export, returns.
- Checks: jobs, recheck history, proxy status, provider diagnostics.
- Reports: supplier quality, profit, inventory aging, valid-rate trends.

Keep the frontend framework-free unless the project intentionally migrates to a richer UI stack.

## Security And Secret Handling

- Treat API keys and proxy credentials as sensitive.
- Do not inspect, print, or commit `data/keys.db`, `data/proxies.txt`, `data/working_proxies.txt`, or `data/dead_proxies.txt`.
- Prefer environment variable `ADMIN_KEY` for deployment instead of relying on source defaults.
- Do not add dependencies or scripts that log full keys. Use `detector.short_key()` or masking when displaying keys.
- Proxy passwords should stay masked in terminal output; `scripts/check_socks5_proxies.py` already does this.
- Future sales/inventory features must add audit trails before exposing destructive or bulk actions.
- Before this becomes a real business system, add key encryption-at-rest or a dedicated secret store.

## Development Notes For Agents

- Keep the public checker contract stable: `check(key, proxy=None) -> result dict`.
- Keep database writes centralized in `db.py` until a deliberate data-layer refactor exists.
- Keep route orchestration in `app.py`; provider-specific logic belongs in `checkers/`.
- Do not mix provider checking logic with supplier/customer/order logic.
- Preserve `asyncio` concurrency controls when changing job/checker behavior.
- Avoid network-heavy probes unless they are explicitly needed for tier detection.
- Prefer append-only inventory movement records for future stock changes.
- Never lose historical check, inbound, outbound, or audit data when changing state.
- When changing model summary behavior, update or add focused unit tests in `tests/`.
- When changing SQLite schema, make the migration safe for existing `data/keys.db` files.
- Prefer `rg` for searching and `.venv/bin/python -m unittest` for quick verification.
