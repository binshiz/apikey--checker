# API Key Tier Detector

A web tool that batch-checks OpenAI / OpenRouter / Azure OpenAI / Anthropic / Gemini / AWS Bedrock / GCP service-account credentials, auto-detects the provider from key format, probes for usability and capabilities, and stores results in SQLite.

## Direction

This project is evolving from a detector into a lightweight API key inventory system. The current checker flow acts as the quality-control layer: imported keys are checked, valid keys are vaulted, and the database now keeps inventory, stock movement, supplier/batch, and check-run foundations for future inbound, outbound, sales, and reporting workflows.

## Features

- **Auto-detect provider** from key string (`sk-proj-…`, OpenRouter `sk-or-v1-…`, Azure endpoint + resource key, `sk-ant-…`, `AIza…`, native Bedrock `ABSK…`, `AKIA…|SecretAccessKey`, and downloaded GCP Service Account JSON); region-suffixed AWS and formatting-only JSON differences are normalized to the same credential.
- **OpenRouter**: validates the credential through `/api/v1/key`, then queries `/api/v1/credits` for total account credits, total account usage, and the computed remaining balance. It also records Free/Paid account type, per-key spending limit and remaining amount, usage windows, reset policy, and expiry; account totals and per-key limits remain clearly distinguished. Ordinary inference keys receive minimal paid `anthropic/claude-opus-5` and `anthropic/claude-fable-5` requests (`max_tokens=1`) to prove access to both models, with distinct callable, no-credit, rate-limit, permission, unavailable, and transport outcomes. If neither paid target is callable, one `openrouter/free` request is used as the fallback proof that the key itself can still perform inference. A failed credits lookup is reported separately and does not override the inference-key validity result. Only a successful runtime response is eligible for the vault. Management keys query account credits without invoking a model or entering the vault. Provisioning keys are identified but are not treated as inference keys.
- **Azure OpenAI**: accepts `*.openai.azure.com` and `*.services.ai.azure.com` base URLs or full `/openai/v1/chat/completions` URLs, legacy and long opaque resource keys, plus paired `*_KEY=` / `*_ENDPOINT=` environment-variable lines. It validates the credential with the model-list API, then separately proves GPT-5.5/5.6 access with minimal Chat Completions calls against common and catalog-derived deployment names. Catalog visibility is never reported as runtime access. Fully custom Azure deployment names cannot be discovered from a resource key alone and remain explicitly unverified. TXT copy/export emits the complete HTTPS Chat Completions URL followed by `|key`.
- **AWS Bedrock API Key**: validates native `ABSK…` bearer tokens against every AWS-supported API-key region using the read-only `ListFoundationModels` endpoint, records authorized/denied regions without invoking a model, and expands TXT copy/export into `ApiKey|region` lines for verified regions.
- **AWS Bedrock Access Key**: validates long-term AWS credentials, discovers Claude Fable 5 and Opus models/inference profiles across all known Bedrock regions, proves access with minimal `InvokeModel` calls, and reports Fable 5 as callable, throttled, data-retention-gated, unavailable, or inconclusive. Copy/export groups credential lines by capability: all Fable 5 regions come first, followed by one mapping proven identically across that whole group; remaining Opus/other-Claude regions follow with their gateway data. If those remaining regions use different AWS geography routes (`global`, `eu`, `us`, or `au`), the output partitions them into explicitly associated route groups instead of inventing one unsafe flat mapping. The UI separately labels both export groups, the any-model region union, and a complete region-to-model JSON matrix. Fable 5 probing never changes the account's data-retention setting; when `provider_data_share` is already enabled, AWS may retain/share the minimal probe input (`.`) and model output under its Fable 5 policy.
- **GCP Service Account JSON**: accepts one complete pasted JSON document or up to 100 selected `.json` files (64 KiB each), fixes authentication to Google's official OAuth token endpoint, then sends one minimal `generateContent` request to each target Vertex AI Gemini model through the `global` endpoint. A model is reported as supported only after an actual successful response; throttling, permission denial, model unavailability, and transport/upstream failures remain distinct probe outcomes. The current target set is Gemini 3.6 Flash, 3.5 Flash, 3.5 Flash-Lite, 3.1 Flash-Lite, 3.1 Pro Preview, and 2.5 Pro. These calls can incur small Vertex AI charges and prove only those model invocations—not Google Play, IAM, or other GCP APIs. Access Tokens, model response bodies, and private keys never appear in check results or logs. TXT copy/export uses one canonical JSON object per line (JSONL).
- **OpenAI**: tier inferred from `x-ratelimit-limit-tokens` header; additionally probes access to `gpt-image-2` and `sora-2`.
- **Gemini**: reads every page of model metadata, distinguishes callable capabilities via `supportedGenerationMethods`, then burst-tests `gemini-2.5-pro` (with Flash fallback) until the first `429` — records measured RPM and maps to T1/T2/T3 (per user spec: <300 → T1, 300–1300 → T2, >1300 → T3).
- **Anthropic**: reads `anthropic-ratelimit-requests-limit` headers, lists `/v1/models`, and sends one minimal paid `claude-opus-5` request (`max_tokens=1`) to distinguish callable, no-credit, rate-limit, permission, unavailable, and transport outcomes. If no rate-limit header is available and Opus 5 did not succeed, it retains the existing `claude-haiku-4-5` burst fallback for tier inference.
- **Batch processing** with configurable concurrency, background job tracking, live progress bar.
- **Bulk operations** in the UI: filter, multi-select, bulk copy, bulk re-test, bulk delete, single re-test.
- **SQLite** storage of all results with provider/status/tier indexes.
- **Inventory sales state** with auditable per-key sell/return records, buyer text, unit price, and append-only stock movements.

## Run

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

Open <http://127.0.0.1:8787/>.

## Check SOCKS5 proxies

`data/proxies.txt` supports one proxy per line, including:

- `host:port@username:password`
- `username:password@host:port`
- `socks5://username:password@host:port`
- `host:port`

Run:

```bash
python3 scripts/check_socks5_proxies.py
```

Useful options:

```bash
python3 scripts/check_socks5_proxies.py --limit 10 -c 10 -t 5
python3 scripts/check_socks5_proxies.py -o data/working_proxies.txt --dead-output data/dead_proxies.txt
python3 scripts/check_socks5_proxies.py --target https://api.ipify.org?format=json
```

The terminal output masks proxy passwords. `data/working_proxies.txt` keeps the original working proxy lines so the app can reuse them directly.

## Tier thresholds

| Provider  | Tier 1 | Tier 2     | Tier 3      | Tier 4+ |
|-----------|--------|------------|-------------|---------|
| Gemini    | <300 RPM | 300–1300 | >1300       | —       |
| Anthropic | ≤60 RPM | ≤1100    | ≤2200       | >2200   |
| OpenAI    | TPM-mapped per model (see `checkers/openai.py`) | | | |
| OpenRouter | `Free` or `Paid`, with runtime proof through `openrouter/free` | | | |
| Azure OpenAI | Resource/deployment-specific; reported as `Unknown` | | | |
| GCP Service Account | Not applicable; OAuth plus per-model `generateContent` proof | | | |

## File layout

```
app.py              FastAPI app + background job runner
db.py               SQLite schema + CRUD
detector.py         Provider auto-detect from key string
checkers/
  openai.py         OpenAI tier + image/video probe
  openrouter.py     OpenRouter metadata + Claude Opus/Fable 5 and free fallback probes
  azure_openai.py   Azure endpoint/key validation via read-only model list
  anthropic.py      Anthropic tier (header + model list + burst fallback)
  gemini.py         Gemini RPM burst test
  gcp_service_account.py  GCP OAuth exchange + Vertex Gemini runtime probes
templates/index.html
static/style.css    Neo-Brutalism theme
static/app.js
```

## Credential storage warning

The current inventory design stores complete credentials, including GCP service-account private keys, in plaintext inside `data/keys.db`. List APIs and tables mask credentials, and full exports require admin authentication and create audit records, but this is not encryption at rest. Protect the `data/` directory and its backups as high-sensitivity secrets, never commit them, and use a dedicated secret store or encryption layer before production business use.
