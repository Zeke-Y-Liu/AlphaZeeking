# 引擎C-T6 · summary 冻结：_macro_summary 纯函数 + 字段冻结

## 目标
`runs.summary.macro_liquidity` 字段冻结：结构定型、纯函数产出、边界声明。

## 核心交付
1. `review.py` 新增 `_macro_summary` **纯函数**：从 T-C2 产物提炼冻结字段子集
2. **冻结契约**（字段级，向后兼容只增不改）：
   ```json
   {"state": "neutral", "global": 1,
    "markets": {"A": 1, "HK": 0, "US": 1},
    "global_score": 0.458, "as_of": "2026-10-09"}
   ```
3. **明确不进**：不进 data_risk（不参与数据风险判定）、不进六项验证门禁（透明降级语义）
4. 纯函数单测 2 条（结构/幂等）——本任务测试面小，但契约面最大（下游 T-C7 前端与 API.md 全部引用此结构）

## DoD 验收
- [ ] `_macro_summary` 单测 2 passed（结构断言 + 幂等）
- [ ] PG 实测落库结构逐字段符合冻结契约
- [ ] core 冒烟零失败

## 红线
字段冻结（改名/删字段 = 破坏性变更须走契约修订）；不进 data_risk/门禁（写进 API.md 注释）
