# API Key Tier Detector

A web tool that batch-checks OpenAI / Anthropic / Gemini / AWS Bedrock credentials, auto-detects the provider from key format, probes for usability and capabilities, and stores results in SQLite.

## Direction

This project is evolving from a detector into a lightweight API key inventory system. The current checker flow acts as the quality-control layer: imported keys are checked, valid keys are vaulted, and the database now keeps inventory, stock movement, supplier/batch, and check-run foundations for future inbound, outbound, sales, and reporting workflows.

## Features

- **Auto-detect provider** from key string (`sk-proj-…`, `sk-ant-…`, `AIza…`, `AKIA…|SecretAccessKey`).
- **AWS Bedrock**: validates long-term AWS credentials, proves Claude Opus access with a minimal `Converse` call, and offers an explicit multi-region deep check for model and quota details.
- **OpenAI**: tier inferred from `x-ratelimit-limit-tokens` header; additionally probes access to `gpt-image-2` and `sora-2`.
- **Gemini**: reads every page of model metadata, distinguishes callable capabilities via `supportedGenerationMethods`, then burst-tests `gemini-2.5-pro` (with Flash fallback) until the first `429` — records measured RPM and maps to T1/T2/T3 (per user spec: <300 → T1, 300–1300 → T2, >1300 → T3).
- **Anthropic**: reads `anthropic-ratelimit-requests-limit` header when present, lists `/v1/models` for target model availability, otherwise burst-tests `claude-haiku-4-5`.
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

## File layout

```
app.py              FastAPI app + background job runner
db.py               SQLite schema + CRUD
detector.py         Provider auto-detect from key string
checkers/
  openai.py         OpenAI tier + image/video probe
  anthropic.py      Anthropic tier (header + model list + burst fallback)
  gemini.py         Gemini RPM burst test
templates/index.html
static/style.css    Neo-Brutalism theme
static/app.js
```
