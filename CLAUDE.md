# CLAUDE.md

本文件为 AI 编码代理（Claude Code / Cowork）提供本仓库的架构与开发约定。与 `README.md`（面向使用者）、`agents.md`（方向与未来规划）、`task.md`（路线图与任务清单）配合阅读；三者若有冲突，以代码与本文件的“当前实现”为准（`agents.md` 的 checker 列表偏旧，只列了 openai/anthropic/gemini）。

---

## 1. 项目定位

**API Key Tier Detector → API Key 进销存管理系统。**

一个 Web 工具，批量检测多家厂商的 API 凭证：自动识别厂商、探测可用性与能力/额度/等级，把结果存入 SQLite。当前正沿着“检测”能力向“进销存（入库 / 库存 / 出库销售 / 复检质检 / 报表）”演进——检测层是整个库存系统的“质检/验货”模块。

支持的厂商（7 个 checker）：**OpenAI、Azure OpenAI、Anthropic、Google Gemini、AWS Bedrock、GCP Service Account、OpenRouter。**

---

## 2. 技术栈

- **后端**：Python 3.12、FastAPI、Uvicorn、Pydantic v2、httpx（`[socks]`）。
- **前端**：原生 HTML/CSS/JavaScript，由 FastAPI 直接托管；无构建工具、无 JS 包管理器、无框架。
- **存储**：SQLite，位于 `data/keys.db`，由 `db.py` 初始化与迁移；无 ORM，全部手写 SQL。
- **异步任务**：`asyncio` 后台任务 + `Semaphore` 并发控制。
- **代理**：可选 SOCKS5 代理池，来源 `data/proxies.txt`。
- **AWS**：`boto3`（Bedrock）；**GCP**：`google-auth`（Service Account OAuth）。
- **测试**：Python `unittest`，位于 `tests/`（当前约 20 个测试文件）。
- **部署**：`Dockerfile` + `docker-compose.yml`，服务端口 **8787**。

依赖见 `requirements.txt`：`fastapi`、`uvicorn[standard]`、`httpx[socks]`、`jinja2`、`python-multipart`、`pydantic`、`boto3`、`PySocks`、`google-auth[requests]`。

---

## 3. 运行与开发命令

本地开发：

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py          # 监听 http://127.0.0.1:8787/
```

Docker：

```bash
docker compose up --build        # 映射 8787，挂载 ./data，TZ=Asia/Shanghai
```

测试（改动前后都应运行）：

```bash
.venv/bin/python -m unittest
```

检查 SOCKS5 代理：

```bash
.venv/bin/python scripts/check_socks5_proxies.py
# 常用参数：--limit 10 -c 10 -t 5 -o data/working_proxies.txt --dead-output data/dead_proxies.txt
```

搜索建议用 `rg`；快速验证用 `.venv/bin/python -m unittest`。

---

## 4. 目录结构

```text
app.py                          FastAPI 应用：鉴权、路由、后台任务调度、导出/脱敏
db.py                           SQLite 全部 schema + 迁移 + CRUD + 库存/销售/审计逻辑（唯一数据层）
detector.py                     厂商识别、凭证归一化(normalize_key)、脱敏短展示(short_key)
proxy_pool.py                   SOCKS5 代理解析、轮询池、健康检查
checkers/
  openai.py                     OpenAI 校验 + 模型摘要 + TPM/RPM 等级 + gpt-image/sora 探测
  azure_openai.py               Azure endpoint/deployment 校验（模型列表 / 单次 Chat Completions）
  anthropic.py                  Anthropic 校验 + 限速头 + 模型列表 + Opus 5 最小付费探测 + burst 兜底
  gemini.py                     Gemini 模型列表 + RPM burst 测试映射 T1/T2/T3
  bedrock.py                    Bedrock API Key(ABSK) 与 Access Key(AKIA) 校验、区域发现、网关映射（最大文件）
  gcp_service_account.py        GCP OAuth 交换 + Vertex Gemini 逐模型 generateContent 探测
  openrouter.py                 OpenRouter /key /credits + Claude Opus/Fable 5 与 openrouter/free 兜底探测
config/
  new-api-bedrock-mappings/     Bedrock 网关模型映射参考数据
templates/index.html            单页 UI 外壳（鉴权浮层 + 5 个 Tab）
static/app.js                   全部前端状态与 /api 调用（约 111KB，单文件）
static/style.css                Neo-Brutalism 主题
scripts/check_socks5_proxies.py 代理校验 CLI
tests/                          unittest（checker、detector、db 库存、app 集成、导出/销售安全）
data/                           运行期数据：keys.db、proxies.txt 等——含明文凭证，勿提交/勿打印
```

---

## 5. 后端流程（`app.py`）

`app.py` 是中枢协调者，本身不写业务 SQL（数据层集中在 `db.py`）：

1. `GET /` 返回 UI。
2. 除 `POST /api/auth` 外，全部 `/api/*` 通过 `Depends(require_auth)` 校验请求头 `X-Admin-Key`。
3. `POST /api/keys/import`：用 `_parse_import_credentials()` 解析粘贴文本 / GCP JSON / Azure `*_KEY=`+`*_ENDPOINT=` 环境变量行，`detector.detect_provider()` 归类，`db.upsert_keys()` 入库。
4. 创建 job（`db.create_job`），`asyncio.create_task(run_job(...))` 后台检测。
5. `run_job` 用 `asyncio.Semaphore(concurrency)` 调度 `check_one_key`；后者按 `CHECKERS` 映射分发到对应 checker，可选从 `proxy_pool.get_pool()` 取 SOCKS5 代理。
6. 结果经 `_sanitize_result_extra` / `_redact_proxy_urls`（去除代理地址与凭证）后由 `db.save_result()` 落库；有效 key 自动进 `vault`，同时写入 `check_runs` 并联动库存状态。

**关键实现细节**
- **检测模式** `CheckMode = "quick" | "bedrock_deep"`。`quick` 只做只读/最小探测；`bedrock_deep` 仅对 `aws_bedrock` 生效，做 `InvokeModel` 深度探测。
- **并发上限**：`_effective_concurrency()` → quick 最多 **32**，bedrock_deep 最多 **2**。
- **进度**：Bedrock 深检通过 `progress_callback` + `db.bump_job_detail()` 上报逐区域细粒度进度（`detail_total/detail_done/detail_label`）。
- **代理失活**：错误串命中 `proxyerror/connecterror/timeout` 等标记时 `mark_dead(proxy)`。
- **重启恢复**：`lifespan` 启动时 `db.init_db()` + `db.cancel_running_jobs()`（中止上次未完成 job），并预加载代理池（惰性健康检查）。
- **导出**：`_secret_export_response` 支持 `txt / json / bundle`。Bedrock 会展开成 `凭证|region` 行并附带网关模型映射 JSON；Azure 导出完整 Chat Completions URL + `|key`；GCP 用单行 JSONL。**导出完整 key 会写审计日志。**

---

## 6. 数据模型（`db.py`）

SQLite，路径 `data/keys.db`。schema 通过“遗留建表 + 版本化迁移”管理：`init_db()` 先建遗留表，再跑 `schema_migrations` 记录的迁移。

**迁移**（`_run_migrations`）：
1. `inventory_foundation` — 建库存基础表 + 从遗留数据回填。
2. `inventory_sales_and_check_state` — 销售/审计/检测状态；**每次启动都重跑**（所有操作幂等，可自愈半应用状态）。
3. `job_detail_progress` — job 细粒度进度列。

**表清单**

| 表 | 作用 |
|----|----|
| `keys` | 所有导入 key 与最近一次检测结果（`status: pending/checking/valid/invalid/no_quota/error`，tier/rpm/tpm/extra JSON）。 |
| `vault` | 已验证有效 key 的持久库，每次有效检测刷新（含 `check_count`、`note`）。当前定位为“有效 key 临时库”，未来弱化。 |
| `jobs` | 后台任务：status、total/done、concurrency、mode、detail_* 进度。 |
| `schema_migrations` | 迁移版本记录。 |
| `suppliers` | 供应商/来源。 |
| `purchase_batches` | 采购批次：供应商、数量、成本、渠道、付款状态、标签、采购时间。 |
| `api_key_inventory` | **正式库存主表**：一行一个 key，含 `stock_status`、`current_check_status`、供应商/批次、tier、风险标记、`latest_check_run_id`。 |
| `stock_movements` | **append-only 库存流水**：每次状态变化一条（入库/预留/售出/退回/隔离/归档），可关联 `sale_id`。 |
| `check_runs` | 每次检测/复检的完整历史（不止最后一次），含 source（checker / bedrock_deep）。 |
| `sales` | 销售记录：买家、单价（`unit_price_minor` 分）、货币 CNY、状态 sold/returned；`idx_sales_one_active_sold` 保证一个库存同时只有一条 sold。 |
| `audit_logs` | 敏感操作审计：actor、ip、action、target、metadata、时间。 |

**库存状态**（`stock_status`）：`pending_check`、`in_stock`、`reserved`、`sold`、`returned`、`no_quota`、`invalid`、`quarantined`、`archived`。

> ⚠️ 迁移必须 **additive/幂等**，保证既有 `keys`/`vault`/`jobs` 与线上 `data/keys.db` 不被破坏；改 schema 前后都要加/跑测试。

---

## 7. Checker 契约

`checkers/` 下每个模块导出：

```python
async def check(key: str, proxy: str | None = None, **kwargs) -> dict
```

返回 dict（由 `db.save_result()` 消费）：

```python
{
    "status": "valid|invalid|no_quota|error",
    "tier": str | None,
    "rpm": int | None,
    "tpm": int | None,
    "error": str | None,
    "extra": dict,   # 厂商专属信息（模型摘要、区域结果、额度等）
}
```

约定：checker 层只放厂商相关逻辑，**不得**混入供应商/客户/订单/库存/记账逻辑；除非等级检测确需，否则避免高开销/付费探测；返回格式在有计划的重构前保持稳定。

**各厂商要点**（等级阈值见下）
- **OpenAI**：`x-ratelimit-limit-tokens` 头推断 tier；额外探测 `gpt-image-2` / `sora-2`。
- **Azure OpenAI**：接受 `*.openai.azure.com` / `*.services.ai.azure.com` / `*.services.azure.com` 及显式 `cognitiveservices.azure.com/openai/deployments/{d}/chat/completions?api-version=...`；resource 级先用模型列表校验再做最小 GPT 调用，deployment URL 直接一次最小 Chat Completions。“目录可见”不等于“运行可用”。
- **Anthropic**：读 `anthropic-ratelimit-requests-limit`，列 `/v1/models`，发一次最小付费 `claude-opus-5`（`max_tokens=1`）区分 callable/no-credit/rate-limit/permission/unavailable/transport；无头且 Opus 失败时用 `claude-haiku-4-5` burst 兜底。
- **Gemini**：分页读模型元数据，按 `supportedGenerationMethods` 区分能力；burst 测 `gemini-2.5-pro`（Flash 兜底）到首个 429，测得 RPM 映射 T1/T2/T3。
- **AWS Bedrock**：`ABSK…` bearer token 用只读 `ListFoundationModels` 跑遍所有 API-key 区域记录授权区域；`AKIA…|Secret` 长期凭证发现各区 Claude Fable 5 / Opus 模型与 inference profile，用最小 `InvokeModel` 证明可用，按能力分组导出并附网关映射。深检不改账户 data-retention 设置。
- **GCP Service Account**：整段 JSON 或最多 100 个 `.json`（每个 ≤64KiB），固定用 Google 官方 OAuth token 端点换 token，对每个目标 Vertex Gemini 模型经 `global` 端点发一次最小 `generateContent`，仅成功响应才算支持。Access Token / 响应体 / 私钥绝不入结果或日志。
- **OpenRouter**：`/api/v1/key` 校验 + `/api/v1/credits` 取额度；普通推理 key 对 `anthropic/claude-opus-5`、`anthropic/claude-fable-5` 各发最小请求，都不可用时用一次 `openrouter/free` 兜底证明可推理。管理/供应 key 不进 vault。

**等级阈值**

| 厂商 | T1 | T2 | T3 | T4+ |
|----|----|----|----|----|
| Gemini | <300 RPM | 300–1300 | >1300 | — |
| Anthropic | ≤60 RPM | ≤1100 | ≤2200 | >2200 |
| OpenAI | 按模型 TPM 映射（见 `checkers/openai.py`） | | | |
| OpenRouter | `Free` / `Paid`（运行时证明） | | | |
| Azure OpenAI | resource/deployment 相关，报 `Unknown` | | | |
| GCP SA | 不适用；OAuth + 逐模型运行证明 | | | |

---

## 8. API 路由概览

全部 `/api/*`（除 `/api/auth`）需 `X-Admin-Key`。

- **Keys**：`POST /api/keys/import`、`/api/keys/recheck`、`/api/keys/delete`、`/api/keys/export`；`GET /api/keys`（按 provider/status/tier 过滤）。
- **Jobs**：`GET /api/jobs/running`、`GET /api/jobs/{id}`。
- **Vault**：`GET /api/vault`；`POST /api/vault/delete`、`/api/vault/recheck`、`/api/vault/note/{id}`、`/api/vault/inbound`（从 vault 建批次入正式库存）、`/api/vault/export`。
- **Inventory**：`GET /api/inventory`、`GET /api/inventory/{id}`（含 check_runs/movements/sales）；`POST /api/inventory/recheck`、`/api/inventory/status`、`/api/inventory/meta/{id}`、`/api/inventory/export`、`/api/inventory/sell`、`/api/inventory/return`。
- **Sales**：`POST /api/sales/export`。
- **Proxy**：`GET /api/proxy/status`、`POST /api/proxy/reload`。
- **Auth**：`POST /api/auth`。

返回给前端的 key 一律脱敏（`api_key_short`），完整 key 只在带审计的导出接口出现。

---

## 9. 前端

单页应用：`templates/index.html` 定义鉴权浮层与 5 个 Tab（`data-tab`）：**检测 check、vault、inventory、sellable、sold**。`static/app.js` 用 `fetch()` 调 `/api/*` 管理全部状态；管理鉴权存 `sessionStorage` 并作为 `X-Admin-Key` 发送；job 运行时每 1.5s 轮询进度。`static/style.css` 为 Neo-Brutalism（CSS 变量）。

保持前端 framework-free，除非项目有意迁移到更重的 UI 栈。未来导航可演进为 Dashboard / Inbound / Inventory / Sales / Checks / Reports。

---

## 10. 安全与密钥处理

- **鉴权**：`ADMIN_KEY` 从环境变量读取，`app.py` 内有开发默认值；**部署务必用环境变量覆盖**，不要依赖源码默认（这是临时管理员入口，非长期权限模型）。
- **明文存储警告**：库存设计当前把完整凭证（含 GCP 私钥）**明文**存于 `data/keys.db`。列表/表格脱敏、完整导出需鉴权且写审计，但**不是静态加密**。把 `data/` 及其备份当高敏感机密对待，勿提交、勿随意打印；生产前引入 key 加密存储或专用 secret store。
- **禁止入库/入日志/入 UI 的完整 key**：展示一律用 `detector.short_key()` 或掩码；`_redact_proxy_urls` / `_sanitize_result_extra` 会移除代理地址与凭证。
- **勿读取/打印/提交**：`data/keys.db`、`data/proxies.txt`、`data/working_proxies.txt`、`data/dead_proxies.txt`。代理密码在 CLI 输出保持掩码。
- 破坏性/批量操作（删除、导出、出库）暴露前需有审计轨迹（已有 `audit_logs` + `_audit_request`）。

---

## 11. 给代理的开发约定

- 保持 checker 公共契约稳定：`check(key, proxy=None) -> result dict`。
- 数据库写入集中在 `db.py`；路由编排留在 `app.py`；厂商逻辑留在 `checkers/`。不要交叉混入。
- 不要把供应商/客户/订单逻辑放进 checker。
- 改 job/checker 行为时保留 `asyncio` 并发控制。
- 业务状态变化优先写 **append-only** 的 `stock_movements`；**永不**丢失历史检测/入库/出库/审计数据；敏感历史记录非明确要求不物理删除。
- 优先 additive 迁移而非破坏性变更；改 schema 要对既有 `data/keys.db` 安全；**改迁移/状态流转前先加测试**。
- 改模型摘要行为时更新/新增 `tests/` 下聚焦单测。
- 在保持现有检测能力可用的前提下，逐步补齐业务能力（先 schema 稳定，再动 UI）。

---

## 12. 开发路线图（摘自 `task.md`）

长期目标：把检测器演进为进销存系统。分阶段、可兼容地加表，不推翻重写。

- **P0 文档与基线**：README/agents/task 已建；补充进销存方向说明；确认 3.12 环境；每次改动前跑单测。
- **P1 数据模型**：additive 迁移助手；`suppliers`/`purchase_batches`/`api_key_inventory`/`stock_movements`/`check_runs`；空库与旧库初始化测试。—— *基础表已落地，见 §6。*
- **P2 入库**：import 携带供应商/批次/成本/备注/标签；入库建批次并写流水；保留简单粘贴流为默认批次；UI 加供应商/批次元数据。
- **P3 库存 UI**：Vault Tab 扩展为 Inventory；展示状态/供应商/批次/成本/最近检测；按 provider/tier/status/供应商/批次过滤；批量复检/预留/隔离/归档；完整 key 需确认后才复制/导出。
- **P4 销售/出库**：客户与订单；从库存选/自动分配可售 key；写 `order_items`；出库经流水置 `sold`；订单导出交付；退换流程。—— *sell/return + sales 表已部分落地。*
- **P5 质检与复检历史**：每次检测入 `check_runs`；每 key 检测历史；供应商/批次质量汇总；过期未检提醒；重复 key 检测。
- **P6 报表**：仪表盘指标；库存价值；供应商质量；销售收入与毛利；库龄分析。
- **P7 安全加固**：脱离源码默认 `ADMIN_KEY`；敏感操作审计；角色鉴权；key 静态加密/ secret store；SQLite 备份恢复。

> 说明：`customers`/`orders`/`order_items` 尚未建表——当前销售走 `sales`（buyer 文本 + 库存直接标记）。若推进 P4 的正式订单模型，按“additive 迁移 + 保留 sales/流水”推进。

---

## 13. 测试

`tests/` 使用 `unittest`，覆盖：checker（openai/anthropic/gemini/azure/bedrock/gcp/openrouter 模型摘要）、detector（各厂商识别）、db（库存/清点/vault 售出状态）、app 集成（导出、GCP/Azure/OpenRouter、销售安全）。改动相关模块时更新或新增聚焦单测，提交前跑 `.venv/bin/python -m unittest`。
