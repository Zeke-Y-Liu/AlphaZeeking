# 引擎C-T3 · 工具层：factor.macro 工具 + CLI

## 目标
MCP 工具面与 CLI 面暴露引擎C查询能力（只读，regime 查询，不触发任何动作）。

## 核心交付
1. `tools.py` 新增 `factor.macro` 工具：返回完整三市场 dim/level 结构（HK 经 `hk_rates.json` 参与聚合）；缺数据 → `{"status":"unavailable"}` 透明降级
2. `cli.py` 新增对应命令（建议 `ap factor macro`，以本会话 CLI 既有子命令命名风格自定；命令名实现时登记入 RUNBOOK）：同一计算内核，双入口不重复实现
3. 输出含口径/版本注记（scorer 表版本、as_of、来源机构）

## DoD 验收
- [ ] `factor.macro` 三市场 dim/level 结构完整（总验收 #3：A/HK/US = 1/0/1、neutral、global=1 基准）
- [ ] 工具与 CLI 输出一致（同 as_of 逐字段相等）
- [ ] 降级路径：删数据再调用 → unavailable，不抛异常
- [ ] 全量回归零失败

## 红线
只读工具（不写任何状态）；不进 signals 白名单（不决策 grep 红线）
