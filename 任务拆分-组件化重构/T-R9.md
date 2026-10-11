# 组件化重构-T9 · MCP engine.metrics 四工具

## 目标
引擎指标（因子/信号/宏观指标元数据）的 MCP 工具面：list / add / update / remove。

## 核心交付
1. **四工具实现**：`engine.metrics.list / add / update / remove`（MCP 协议暴露）
2. **写操作走写保护**（T-R4 语义）：remove→orphan 标记不物理删；add/update→用户登记字段优先
3. **权限与校验**：非法入参拒绝、操作留痕（谁/何时/改了什么）
4. **工具文档**：参数/返回/错误码登记（供 Flash 模型与 MCP 消费方使用）

## DoD 验收
- [ ] 四工具单测/冒烟全绿（list 空态 / add 幂等 / update 字段级 / remove orphan）
- [ ] 写保护联动实测：工具写操作不覆盖用户改动、不物理删
- [ ] 工具文档与实现 diff 一致

## 红线
工具写操作不得绕过写保护（工具=写保护之上的薄层）；操作留痕不可关闭。
