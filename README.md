# API Key Tier Detector

A web tool that batch-checks OpenAI / Anthropic / Gemini API keys, auto-detects the provider from key format, probes for usability and tier, and stores results in SQLite.

## Features

- **Auto-detect provider** from key string (`sk-proj-…`, `sk-ant-…`, `AIza…`).
- **OpenAI**: tier inferred from `x-ratelimit-limit-tokens` header; additionally probes access to `gpt-image-2` and `sora-2`.
- **Gemini**: burst-tests `gemini-2.5-pro` until the first `429` — records measured RPM and maps to T1/T2/T3 (per user spec: <300 → T1, 300–1300 → T2, >1300 → T3).
- **Anthropic**: reads `anthropic-ratelimit-requests-limit` header when present, otherwise burst-tests `claude-haiku-4-5`.
- **Batch processing** with configurable concurrency, background job tracking, live progress bar.
- **Bulk operations** in the UI: filter, multi-select, bulk copy, bulk re-test, bulk delete, single re-test.
- **SQLite** storage of all results with provider/status/tier indexes.

## Run

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

Open <http://127.0.0.1:8787/>.

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
  anthropic.py      Anthropic tier (header + burst fallback)
  gemini.py         Gemini RPM burst test
templates/index.html
static/style.css    Neo-Brutalism theme
static/app.js
```
