# 组件化重构-T9 · MCP engine.metrics 四工具

## 目标
引擎指标（因子/信号/宏观指标元数据）的 MCP 工具面：list / add / update / remove。

## 核心交付
1. **四工具实现**：`engine.metrics.list / add / update / remove`（MCP 协议暴露）
2. **写操作走写保护**（T-R4 语义）：remove→orphan 标记不物理删；add/update→用户登记字段优先
3. **权限与校验**：非法入参拒绝、操作留痕（谁/何时/改了什么）
4. **工具文档**：参数/返回/错误码登记（供 Flash 模型与 MCP 消费方使用）

## DoD 验收
- [x] 四工具单测/冒烟全绿（list 空态 / add 幂等 / update 字段级 / remove orphan）
- [x] 写保护联动实测：工具写操作不覆盖用户改动、不物理删
- [x] 工具文档与实现 diff 一致

## 本会话实测（2026-10-11，批次 2 实施记录）

| 交付 | 实测结果 |
| --- | --- |
| 四工具 | [tools.py](file:///workspace/alphapocket/applib/tools.py) `engine.metrics.{list,add,update,remove}`（TOOLS 注册表 MCP 暴露）：list=因子全景+DatasetSpec 声明派生（n_datasets≥15，空态不炸）；add=幂等（同 id 不覆盖既有条目→用户改动安全，默认 draft/v0.1）；update=字段级（id 不可改；`before` 旧值快照留痕）；remove=orphan 标记+`removed_at`（条目物理保留，M5 T-L3 接管） |
| 写保护联动 | `test_write_protection_pipeline_after_tool_ops`：工具改动后模拟拉取覆盖写→`merge_registry`（T-R4）：owner/note 用户字段保留 + version 机器字段新胜；remove 物理条目断言保留 |
| 权限与校验 | 非法入参显式拒绝（缺 id / type 非法 alpha·signal·macro / update id 不可修改 / 不存在 id → `updated:False` 不炸）；**操作留痕不可关闭**：`registry.operations` 链逐次登记 `{op,id,ts,actor,fields,before}` |
| 工具文档 | 参数/返回/错误码见工具 docstring + [T-R9-工具文档.md](file:///workspace/任务拆分-组件化重构/T-R9-工具文档.md)（与实现逐字段 diff 一致） |
| 单测 | 新增 [test_engine_metrics.py](file:///workspace/alphapocket/tests/test_engine_metrics.py) 6 条（list 空态+全量/add 幂等+留痕/非法入参/update 字段级+before/remove orphan+幂等/写保护管线联动） |
| 回归 | α **139 passed**（133+6）零失败 |

## 红线
工具写操作不得绕过写保护（工具=写保护之上的薄层）；操作留痕不可关闭。
