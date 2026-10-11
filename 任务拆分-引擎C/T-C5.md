# 引擎C-T5 · core 契约：web core 消费链接入

## 目标
web core（FastAPI）评估链路消费引擎C：从 α 内核产物到 core API 契约。

## 核心交付
1. `core/app/services/review.py`（core 侧评估服务）接入：评估 run 的 summary 携带 macro_liquidity（结构来自 T-C2 冻结契约）
2. core 不重复实现计算：只消费 α 内核（applib/factorlib）产物——单一事实源
3. OpenAPI 契约：如新增/扩展端点，OpenAPI 快照冻结同步更新
4. PG 落库链路验证：最新 review run 的 `summary.macro_liquidity` 结构实测落库正确（HK 当时缺数据 → 透明降级，同步后恢复——该现象即总验收 #5 的实测样本）

## DoD 验收
- [ ] core 重启加载后 healthz 全依赖项 ok
- [ ] PG 实查 summary:macro_liquidity 结构符合冻结契约（总验收 #5）
- [ ] core 侧冒烟零失败

## 红线
core 零重复计算（消费不实现）；不进 data_risk；不进门禁
