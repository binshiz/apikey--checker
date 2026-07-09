# task.md

## Long-Term Goal

把当前 API Key Tier Detector 演进为 API Key 进销存管理系统。

当前项目已经具备“检测、复检、有效 Key 入库、导出”的基础能力。下一步不是推翻重写，而是沿着现有 FastAPI + SQLite + 静态前端结构，逐步补齐业务实体、库存状态、库存流水、订单出库和报表能力。

## Product Modules

### 1. 入库 Inbound

目标：把“粘贴 Key 检测”升级为正式入库流程。

- 记录供应商/来源。
- 记录采购批次、采购数量、采购成本、采购时间。
- 支持批次备注、标签、渠道、付款状态。
- 入库后自动进入检测队列。
- 检测通过的 Key 进入可售库存。
- 检测失败的 Key 进入异常区，不直接删除。

### 2. 库存 Inventory

目标：让每个 Key 都有完整生命周期，而不是只有检测结果。

- 建立正式库存状态：待检测、可售、已预留、已售出、无额度、失效、隔离、归档。
- 支持按厂商、等级、RPM、TPM、模型权限、供应商、批次、状态筛选。
- 支持库存备注、内部标签、风险标记。
- 支持批量复检、批量隔离、批量归档。
- 保留每次检测历史，不只保存最后一次结果。

### 3. 销售/出库 Sales And Outbound

目标：管理客户订单和 Key 交付。

- 建立客户表。
- 建立订单表。
- 支持从库存中选择或自动分配可售 Key。
- 出库时把 Key 状态从 `in_stock` 改为 `sold`。
- 记录售价、订单备注、交付时间、交付内容。
- 支持退回、换货、售后标记。
- 支持导出订单交付 Key，但默认继续脱敏展示。

### 4. 复检 And Quality Control

目标：让检测能力成为库存质量系统。

- 支持按批次、供应商、客户订单、库存状态发起复检。
- 保留复检历史：时间、结果、错误、代理、模型能力、RPM/TPM。
- 统计供应商有效率、无额度率、失效率。
- 识别重复 Key、降级 Key、异常波动 Key。
- 对售出 Key 可选择是否继续复检，避免误操作。

### 5. 报表 Reports

目标：提供经营视角。

- 库存总量、可售数量、异常数量。
- 按厂商和等级统计库存。
- 按供应商统计有效率和质量。
- 采购成本、销售收入、毛利。
- 库龄分析：长时间未售出、长时间未复检。
- 最近入库、最近出库、最近失效趋势。

### 6. 安全 Security

目标：让系统可以安全保存和交付敏感 Key。

- Key 默认脱敏展示。
- 导出和查看完整 Key 需要更强权限或二次确认。
- 添加操作审计：谁在什么时候导入、导出、删除、出库。
- 后续引入 Key 加密存储或专用 secret store。
- 区分管理员、库存操作员、销售/只读角色。
- 支持数据库备份和恢复策略。

## Proposed Schema Roadmap

不要一次性大改。推荐分阶段添加表，保留现有 `keys`、`vault`、`jobs` 兼容。

### Phase 1: Inventory Foundation

新增或迁移目标：

- `suppliers`
- `purchase_batches`
- `api_key_inventory`
- `stock_movements`
- `check_runs`

关键点：

- `api_key_inventory` 成为正式库存主表。
- `stock_movements` 记录每次状态变化，尽量 append-only。
- `check_runs` 保存每次检测结果，当前 `keys.extra` 的内容可以作为摘要迁移。
- 现有 `vault` 可先继续存在，等库存主表稳定后再弱化。

### Phase 2: Sales Foundation

新增目标：

- `customers`
- `orders`
- `order_items`

关键点：

- 出库必须写入 `orders`、`order_items`、`stock_movements`。
- 已售 Key 不应出现在可售库存中。
- 退回/换货不要物理删除原记录，应写反向库存流水。

### Phase 3: Audit And Permissions

新增目标：

- `users` or local operator records
- `audit_logs`
- role/permission checks

关键点：

- 完整 Key 查看、导出、批量删除、批量出库都需要审计。
- 当前 `ADMIN_KEY` 可以作为临时管理员入口，但不是长期权限模型。

## Near-Term Task List

### P0: Documentation And Baseline

- [x] Create `agents.md`.
- [x] Update `agents.md` with the long-term inventory-management direction.
- [x] Create this `task.md`.
- [ ] Add a short README section explaining the future进销存 direction.
- [ ] Confirm Python 3.12 local environment and dependency install path.
- [ ] Run current unit tests before each code change.

### P1: Data Model Preparation

- [ ] Design a migration helper in `db.py` for additive schema changes.
- [ ] Add `suppliers` table.
- [ ] Add `purchase_batches` table.
- [ ] Add `api_key_inventory` table.
- [ ] Add `stock_movements` table.
- [ ] Add `check_runs` table.
- [ ] Write migration code that keeps existing `keys` and `vault` working.
- [ ] Add tests for schema initialization on an empty database.
- [ ] Add tests for schema initialization on a pre-existing old database.

### P2: Inbound Workflow

- [ ] Extend import payload to accept supplier, batch name, cost, note, and tags.
- [ ] Create inbound batch on import.
- [ ] Link imported keys to inventory records.
- [ ] Write stock movement rows for inbound events.
- [ ] Keep current simple paste flow working as a default batch.
- [ ] Add UI controls for supplier and batch metadata.

### P3: Inventory UI

- [ ] Replace or extend Vault tab into Inventory tab.
- [ ] Show inventory status, supplier, batch, cost, latest check result, and latest check time.
- [ ] Add filters for provider, tier, status, supplier, batch.
- [ ] Add bulk actions: recheck, reserve, quarantine, archive.
- [ ] Keep full key hidden unless copied/exported with confirmation.

### P4: Sales/Outbound Workflow

- [ ] Add customer creation/listing.
- [ ] Add order creation.
- [ ] Support selecting available inventory for an order.
- [ ] Support automatic allocation by provider/tier/count.
- [ ] Write `order_items`.
- [ ] Mark keys as sold through stock movement.
- [ ] Add order export/delivery action.
- [ ] Add return/exchange flow.

### P5: Quality And Recheck History

- [ ] Save every check result into `check_runs`.
- [ ] Show check history per Key.
- [ ] Add supplier quality summary.
- [ ] Add batch quality summary.
- [ ] Add stale-check warning.
- [ ] Add duplicate-key detection.

### P6: Reports

- [ ] Add dashboard metrics.
- [ ] Add inventory value report.
- [ ] Add supplier quality report.
- [ ] Add sales revenue and gross profit report.
- [ ] Add inventory aging report.

### P7: Security Hardening

- [ ] Move away from source-code default `ADMIN_KEY`.
- [ ] Add audit logs for sensitive operations.
- [ ] Add role-aware auth.
- [ ] Encrypt stored API keys or integrate a secret store.
- [ ] Add backup/restore workflow for SQLite data.

## Design Rules

- Keep current detection behavior working while adding business features.
- Prefer additive schema migrations over destructive changes.
- Do not delete sensitive historical records unless explicitly required.
- Use stock movement records for business state changes.
- Do not put supplier/order/customer logic inside provider checkers.
- Keep checker result format stable until a planned refactor exists.
- Avoid printing full API keys in logs, tests, or UI.
- Add tests around migration and state transitions before large workflow changes.

## First Implementation Recommendation

Start with P1 before changing the UI heavily. The clean first step is:

1. Add additive schema migration support in `db.py`.
2. Add `suppliers`, `purchase_batches`, `api_key_inventory`, `stock_movements`, and `check_runs`.
3. Mirror existing valid `vault` records into `api_key_inventory`.
4. Keep the current UI unchanged until the new tables are stable.

This gives the project a real inventory spine without breaking the working detector.
