# 引擎C-T5 · core 契约：web core 消费链接入

## 目标
web core（FastAPI）评估链路消费引擎C：从 α 内核产物到 core API 契约。

## 核心交付
1. `core/app/services/review.py`（core 侧评估服务）接入：评估 run 的 summary 携带 macro_liquidity（结构来自 T-C2 冻结契约）
2. core 不重复实现计算：只消费 α 内核（applib/factorlib）产物——单一事实源
3. OpenAPI 契约：如新增/扩展端点，OpenAPI 快照冻结同步更新
4. PG 落库链路验证：最新 review run 的 `summary.macro_liquidity` 结构实测落库正确（HK 当时缺数据 → 透明降级，同步后恢复——该现象即总验收 #5 的实测样本）

## DoD 验收
- [x] core 重启加载后 healthz 全依赖项 ok
- [x] PG 实查 summary:macro_liquidity 结构符合冻结契约（总验收 #5）
- [x] core 侧冒烟零失败

## 本会话实测（2026-10-11，批次 1 实施记录）

| DoD | 实测结果 |
| --- | --- |
| healthz 全依赖 ok | 真启动 uvicorn（alembic head 0002）→ `/healthz` = `{"status":"ok","deps":{"pg":ok,"redis":ok,"minio":ok}}`（沙箱补装 PG16 + redis-server；minio 9000 沙箱既有） |
| PG 实查 | 用例 2（ok 态）+ 用例 6（降级态）双样本：`run.summary.macro_liquidity` 冻结契约五键 + status 落库正确；`"signals" not in pg_ml` 不决策 grep；unavailable 透传不阻断评估（job done） |
| core 冒烟 | **178 passed + 12 skipped（外网标记）+ 0 failed**（含 test_openapi_snapshot） |

**实施要点**：
- 消费不实现（红线）：`_build_summary` 透传 `review["macro_liquidity"]`（结构 = T-C2 冻结契约），缺失归一 `{"status":"unavailable"}`（兼容旧工具输出）；core 侧零计算逻辑
- OpenAPI 快照（交付 3）：零变更——runs 响应为自由 dict、summary 内部 JSON 自由结构，无端点/模型变更；test_openapi_snapshot 通过
- 总验收 #5 实测样本：用例 6（HK 缺数据 → unavailable 降级落库）+ 用例 2（恢复后 ok 自动回填）

**环境重建（本会话新增，供后续 core 任务复用）**：PG16（ap/ap@127.0.0.1:5432，alembic 0002）、redis-server（6379）、`core/.venv`（Python 3.12.3 + 全依赖 + duckdb，uv 创建）。测试跑法：`cd core && PYTHONPATH=/workspace/alphapocket .venv/bin/pytest -q`。

**测试加固**：cleanup fixture 加兜底（按 job_ids 关联清 runs），防用例中断残留污染下游（test_gate/test_data_status 曾受其害，修复后全量稳定）。

## 红线
core 零重复计算（消费不实现）；不进 data_risk；不进门禁
