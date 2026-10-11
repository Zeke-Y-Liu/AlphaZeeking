# 引擎C-T3 · 工具层：factor.macro 工具 + CLI

## 目标
MCP 工具面与 CLI 面暴露引擎C查询能力（只读，regime 查询，不触发任何动作）。

## 核心交付
1. `tools.py` 新增 `factor.macro` 工具：返回完整三市场 dim/level 结构（HK 经 `hk_rates.json` 参与聚合）；缺数据 → `{"status":"unavailable"}` 透明降级
2. `cli.py` 新增对应命令（建议 `ap factor macro`，以本会话 CLI 既有子命令命名风格自定；命令名实现时登记入 RUNBOOK）：同一计算内核，双入口不重复实现
3. 输出含口径/版本注记（scorer 表版本、as_of、来源机构）

## DoD 验收
- [x] `factor.macro` 三市场 dim/level 结构完整（总验收 #3：A/HK/US = 1/0/1、neutral、global=1 基准）
- [x] 工具与 CLI 输出一致（同 as_of 逐字段相等）
- [x] 降级路径：删数据再调用 → unavailable，不抛异常
- [x] 全量回归零失败

## 本会话实测（2026-10-11，批次 1 实施记录）

| DoD | 实测结果 |
| --- | --- |
| dim/level 结构 | `factor.macro` 返回冻结五键 + status + notes + detail（逐指标 market/dim/level/score、market_scores、scorer 口径注记）；真实链路冒烟 `markets={A:-1, HK:"unavailable", US:0}, global_score=-0.136`（结构对齐总验收 #3；数值随真实数据漂移，旧会话冻结基准值不可复现——诚实原则，基准值待 T-C5/T-C6 报告层按实测回填） |
| 工具=CLI | CLI `ap macro` 直接 call_tool("factor.macro")（同一内核零重复实现）；capSys 测试逐字段相等 |
| 降级路径 | 密封测试 6 条：缺文件/indicators 空/全 no_data → status=unavailable 不抛异常；只读红线（sandbox 文件快照前后相等）；缺省 as_of 链 args→payload→indicators 最新 dt 兜底 |
| 全量回归 | 79 passed 零失败（73 + 6） |

**命令名登记（RUNBOOK 体例，正式 RUNBOOK 于 T-C8 汇入）**：
`ap macro [--as-of YYYY-MM-DD]`——引擎C 宏观流动性温度计（三市场 regime，只读不决策）；
MCP 工具面 `factor.macro`（同口径）。前置：`ap fetch`（宏观采集独立入口，fetch_macro_liquidity 显式调用）+ `ap clone`。

**冒烟中修复 2 处**：
1. T-C1 payload 缺顶层 `as_of` → datasource 补自描述（各指标最新 dt，T-C3 工具缺省口径），修复后重拉落盘 as_of=2026-10-10
2. 工具端旧文件兼容兜底链（args → payload.as_of → indicators 最新 dt，as_of_source 透明标注）——真实旧产物上验证生效

## 红线
只读工具（不写任何状态）；不进 signals 白名单（不决策 grep 红线）
