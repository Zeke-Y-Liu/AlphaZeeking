# T-L4 · 三态巡检：24 月 IC 衰减/覆盖/漂移/失效环境 + fl_factor_health 落盘

## 目标
季度巡检 DAG 的判定核心：三指标巡检 → healthy/weakening/invalid 三态 + 落盘。

## 核心交付
1. **三指标巡检**（对每个 active 因子，滚动 24 个月窗口）：
   - **IC 衰减**：滚动 24 月 IC_IR 较注册时基准衰减幅度（阈值触发 weakening/invalid）
   - **覆盖度漂移**：截面覆盖率时间趋势（可选标的减少 → 漂移告警）
   - **失效环境**：M4 环境标签复查（牛熊分段 IC 是否翻号、失效环境是否扩散）
2. **三态判定状态机**：`healthy → weakening → invalid`（可恢复：weakening 回升可回 healthy；invalid 需人工决策退役，不自动 deprecated）
3. **fl_factor_health 落盘**：每次巡检全量落库（factor_id/version/窗口/三指标值/判定/证据/巡检时间戳），供 T-L5 复查与 active_pool 续约消费
4. **注入演练**：人为回填一段衰减 IC → weakening 触发（实施计划 DoD「30 天内触发」的流程演练）；注入单测 4 条 + 真实件 inject-delay ×1

## DoD 验收
- [ ] 注入测试：衰减 IC 回填 → 30 天内 weakening（窗口滚动判定正确）
- [ ] 三指标各自单测 + 三态迁移全路径（含恢复路径）
- [ ] fl_factor_health 落盘 schema 与历史可查
- [ ] 全量回归零失败

## 依赖与红线
- 依赖：T-L1（health 字段/状态文件）；M3 IC 时序（fl_factor_eval）；M4 环境标签
- 红线：巡检判定必须可复现（窗口、阈值、证据全落盘）；invalid 摘除动作在 T-L5（本任务只判定不动作）
