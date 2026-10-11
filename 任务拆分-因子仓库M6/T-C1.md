# T-C1 · DSL 端：Strategy YAML 加载校验 + 池准入 + `ap strategy validate`

## 目标
新增 `factorlib` 侧 `strategy.py`：Strategy YAML 的加载、校验、规范化，作为 M6 全链的入口（合成/撮合/wf/报告均消费其产物）。

## 核心交付
1. **YAML 加载与 schema 校验**：`id`（kebab-case）/ `universe` / `factors[]`（id+weight）/ `composite`（top_n + method 四法）/ `rebalance`（monthly|quarterly）/ `exec`（next_day_vwap）/ `constraints` / `benchmark`；未知字段拒绝（不静默忽略）
2. **universe 三过滤器**（数据缺失时 degraded 警示，不静默）：
   - `flt_st`：剔除 ST/*ST
   - `flt_listed_days`：上市天数门槛（默认 500d 与 constraints.min_history 对齐）
   - `flt_avg_amount_20d`：20 日日均成交额门槛
3. **池准入**：`factors[].id` 必须 ∈ M5 `active_pool`（lifecycle active 状态因子）；不在池 → 拒绝加载并给出池内可用清单
4. **CLI**：`ap strategy validate <yaml>`——一次跑全量校验（schema + universe 数据源可达 + 池准入），输出可读报告

## 契约要点
- 校验产物为规范化 `StrategySpec` dict，下游 T-C2/T-C3/T-C4 只消费该结构，不再触碰原始 YAML
- 语义冻结：`weight: equal|ic_12m`；`composite.method: [equal, ic_12m, ic_ir, lasso]` 四法并列（T-C2 实现计算）
- 近似版策略的 `id` 命名带 `-v1` 后缀，M7 替换正式因子时升 v2

## DoD 验收
- [ ] 单测 21+ 条：schema 各字段合法/非法、三过滤器各自命中与豁免、池准入拒绝与清单提示、degraded 警示路径
- [ ] `ap strategy validate` 对三份内置 YAML 全绿
- [ ] 全量回归零失败（M5 基线 407 起步）

## 依赖与红线
- 依赖：M5 active_pool（fl 侧 lifecycle）；M1 registry（因子 id 解析）
- 红线：不决策（DSL 只定义，不产生买卖信号）；universe 过滤数据缺失一律 degraded 警示不静默；不取未来截面
