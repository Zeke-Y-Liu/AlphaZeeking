# T-R9 engine.metrics 四工具 · MCP 工具文档

> 与 [tools.py](file:///workspace/alphapocket/applib/tools.py) 实现逐字段对齐（T-R9 DoD 3）。MCP 消费方（Flash agent / 宿主 IDE）按此调用。
> 作用域：`sandbox/repo/factors/registry.json`（local_repo 工作区；push 走 `repo.push` CR 门禁）。
> 红线：写保护之上的薄层——add 幂等不覆盖 / remove orphan 不物理删 / 操作留痕不可关闭。

## engine.metrics.list

| 项 | 内容 |
| --- | --- |
| 入参 | 无（可选 `{}`） |
| 返回 | `{factors: [条目…], n_factors, datasets: {id: {path, merge_policy}}, n_datasets}` |
| 空态 | registry 无 factors 键 → `{factors: [], n_factors: 0}` 不炸 |
| datasets | 由 DatasetSpec（T-R5 registry.json 声明）派生；spec 加载异常 → `{}`（不影响因子列表） |

## engine.metrics.add

| 项 | 内容 |
| --- | --- |
| 入参 | `{id: str(必填), type: "alpha"\|"signal"\|"macro"(必填), name?, universe?, …任意元数据字段, actor?}` |
| 返回 | `{added: true, id, status: "draft", operation}` 或 `{added: false, reason: "id 已存在…", existing}` |
| 默认值 | `status="draft"`、`version="v0.1"`（显式传入则用显式值） |
| 幂等 | 同 id 已存在 → **不覆盖既有条目**（用户改动安全），返回既有 `status/version/name` |
| 错误码 | `ValueError("add 须提供 id")` / `ValueError("type 非法…（须 alpha / signal / macro）")` |

## engine.metrics.update

| 项 | 内容 |
| --- | --- |
| 入参 | `{id: str(必填), fields: {字段: 新值}(必填非空), actor?}` |
| 返回 | `{updated: true, id, changed: {字段: 旧值}, operation}` 或 `{updated: false, reason: "id 不存在（先 add）"}` |
| 留痕 | `operations` 追加 `{op:"update", id, ts, actor, fields:[改的字段], before:{变动字段:旧值}}` |
| 错误码 | `ValueError("update 须提供 id 与 fields{字段:值}")` / `ValueError("id 不可修改（remove 重建）")` |
| 语义 | 仅显式列出的字段变动；未列字段保持不变（字段级，非整条覆盖） |

## engine.metrics.remove

| 项 | 内容 |
| --- | --- |
| 入参 | `{id: str(必填), actor?}` |
| 返回 | `{removed: true, id, status: "orphan", physical: "条目保留…", operation}` 或 `{removed: false, reason}` |
| 语义 | **orphan 标记不物理删**（T-R4 三语义 ③）：status→orphan + removed_at；后续处置 M5 T-L3 生命周期接管 |
| 幂等 | 已是 orphan → `{removed: false, reason: "已是 orphan（幂等）"}` |
| 留痕 | `operations` 追加 `{op:"remove", …, before:{status: 旧状态}}` |
| 错误码 | `ValueError("remove 须提供 id")` |

## 通用约定

- `actor`：操作发起方标识（默认 `"mcp"`），仅入留痕不参与逻辑。
- 所有写操作同步登记 `registry.operations`（**不可关闭**）；审计链随 registry.json 进 git。
- 与拉取合并的边界：工具写在 local_repo；`ap fetch` 覆盖写后由 writeguard.registry_merge
  合并（机器字段新胜 / 用户字段保留）——单测 `test_write_protection_pipeline_after_tool_ops` 锁定。
