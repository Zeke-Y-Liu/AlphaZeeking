# T-L6 · 调度与收尾：季度 cron + RUNBOOK + DoD 实测 + CP-0025

## 目标
M5 总收口：巡检调度登记、运维文档、DoD 三项实测留证、检查点 CP-0025 落盘。

## 核心交付
1. **季度 cron 登记**：巡检 DAG（T-L4 判定 → T-L5 动作）季度触发；α 阶段以 CLI 显式触发等价物登记（`ap factor health-check`?），生产 cron 表达式写入 RUNBOOK
2. **RUNBOOK 全量**：巡检流程、降级处置、版本升级流程（T-L3 两缺陷修复后的新流程）、active_pool 续约口径、review_items_fl 待答复例
3. **DoD 三项实测**（见 00-总览 §5）：状态机全路径/门禁拒绝、衰减注入触发 weakening、版本冻结 bit 级一致
4. **M6 入口盘点**（notes 六条之一）：active 池 4 因子 vs ≥6 硬条件 → 偏差如实登记，M6 以近似版推进的决策记录
5. **CP-0025 登记**：checkpoints.json `TP-0005`（`ms_lifecycle_milestone`，verify 五项 + notes 六条，`next=26`）
6. **文档双登记**：架构文档**附录 D 八小节**（M6 附录 E 依此先例）+ 00-总览.md §6 留证

## 实测基准（旧会话）
- α 407 passed 零回归；core 冒烟 89 passed / 113 skipped / 0 failed
- 注入单测 4 条 + 真实件 inject-delay ×1
- 版本冻结：`mom_20d_fl` 1.0.0→1.1.0，v1.0.0 行 md5 bit 级不变，pin diff 仅 3 个版本元数据字段
- 降级：review 必备 4、pool 摘除 1（`ops_review_fl`）
- 存量修复：`cli.py` fl_refs→fl_duckdb（spduckdb 双配置冲突）→ 407 全绿

## 依赖与红线
- 依赖：T-L1~T-L5 全部收口
- 红线：DoD 未全绿不得登记 CP-0025；M6 入口偏差必须登记在 notes（不得静默放行）
