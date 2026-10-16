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
- [x] `_macro_summary` 单测 2 passed（结构断言 + 幂等）
- [x] PG 实测落库结构逐字段符合冻结契约
- [x] core 冒烟零失败

## 本会话实测（2026-10-11，批次 1 实施记录）

| DoD | 实测结果 |
| --- | --- |
| 纯函数单测 | 2 条全绿：结构（ok 块提炼五键全等 + status/detail/notes 不进 + 4 类降级输入归一 unavailable 形态）+ 幂等（f(f(x))=f(x)，ok/NA 两形态自洽） |
| PG 实查 | 用例 2（ok）/用例 6（降级）落库 `summary.macro_liquidity` 与 `MACRO_FROZEN_OK/NA` **逐字段全等**；`set(ml)==五键` 无扩展键；`"signals" not in` 不决策 grep |
| core 冒烟 | **180 passed + 12 skipped(外网) 零失败**（178 + 2） |

**冻结契约落地形态**（review.py 顶部 `_MACRO_SUMMARY_KEYS` 常量，向后兼容只增不改）：

```json
{"state": "neutral", "global": 0,
 "markets": {"A": -1, "HK": "unavailable", "US": 0},
 "global_score": -0.149, "as_of": "2026-01-06"}
```

降级归一：`state="unavailable"、global="unavailable"、global_score=null、as_of=null、三市场 "unavailable"`（块内 status/reason/detail/notes **不进** summary——reason 留在 §2.5 报告层呈现）。

**与 T-C5 的关系**：T-C5 全量透传（含扩展键）→ T-C6 收紧为纯函数提炼五键子集；用例断言同步从逐键检查升级为全等检查。

**红线落位**：不进 data_risk/门禁已写进 [API.md](file:///workspace/alphapocket-web/docs/API.md)（§4.4 引擎C 注释）与 `_macro_summary` docstring；改名/删字段 = 破坏性变更须走契约修订。

## 红线
字段冻结（改名/删字段 = 破坏性变更须走契约修订）；不进 data_risk/门禁（写进 API.md 注释）
