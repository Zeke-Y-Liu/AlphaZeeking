# 组件化重构-T6 · 存量数据集声明式迁移

## 目标
全部存量数据集硬编码定义迁至 DatasetSpec，落盘产物零变化。

## 核心交付
1. **存量清单→spec 逐项迁移**（行情/财务/宏观等全部数据集）
2. **迁移等价性**：迁移前后落盘产物逐文件 diff 等价（零回归红线 4）
3. **`_MACRO_` 派生语义**：宏观数据集由 datasets/ 声明式派生——引擎C T-C1 引用此机制
   （`macro_liquidity` 的 **carry_old** 合并策略在 spec 中声明：空窗口/拉取失败→旧数据完整保留）

## DoD 验收
- [x] 迁移前后产物 md5/diff 等价（抽样全量核对）
- [x] 全部 spec 加载校验通过（无遗留硬编码定义，grep 验证）
- [x] 引擎C 宏观数据集 spec 可被 T-C1 直接消费（衔接验证）

## 本会话实测（2026-10-11，批次 2 实施记录）

| 交付 | 实测结果 |
| --- | --- |
| 单点声明文件 | [registry.json](file:///workspace/alphapocket/applib/datasets/registry.json) 15 项逐字迁移（dataset_id/interface/upstream/path/fetch_params/validation + macro `indicators` 14 指标扩展键）；`registry_specs()` 单点加载（schema 校验+缓存） |
| fetch_all spec 驱动 | `_reg(ds_id, rows, params, mkt)`：dataset/interface/upstream/path 全部来自声明（调用点零定义字面量）；params 运行时值与声明 fetch_params 合并（显式覆盖默认）；模板项 `dwd_trade_calendar_mkt`（mkt 参数化）；直接落盘 5 处（dim_security/portfolio×2/registry/fx）路径改 spec 引用（行为不变） |
| `_MACRO_` 派生语义 | `_MACRO_DATASET` 常量**物理删除**（不留双轨）→ `_macro_dataset_def()` 从 registry.json 派生（id/path/merge_policy + indicators 逐字等价）；fetch_macro_liquidity 合并段（失败保留/成功 union ~16 行）迁出 → [datasets.carry_old_merge](file:///workspace/alphapocket/applib/datasets/__init__.py) **单点**（fetch 函数只产 raw+占位，合并全走策略函数）；MERGE_POLICY_FN["carry_old"] 注册 |
| **等价性实测**（生产 repo 副本+真实增量 fetch） | provenance：**新增条目 0**，dataset/interface/upstream/file **逐字节零差异**；3 处 params diff 均为运行时漂移（`end=today` 快照 / ggt 雪球→静态快照降级，非迁移差异，如实登记）；K 线 **12534→12550 不丢历史**；macro 重拉 **11 ok/0 carried/3 unavailable 行数零回归** + payload 五键齐 |
| grep 红线 | datasource.py 零遗留数据集路径/接口字面量（仅 docstring 历史名 2 处，非代码） |
| 单测 | 15 passed 项中新增/改造 2 条：迁移清单全覆盖断言（MIGRATION_LOG ⊆ registry_specs）+ `_macro_dataset_def` 派生等价（14 指标/carry_old）；T-R5「carry_old 未注册拒绝」测试按 T-R6 收编更新（现断言已注册且行为正确） |
| 回归 | α **115 passed**（114+1）零失败 |

## 红线
迁移不改变落盘产物（一行 diff 都是回归）；旧定义代码物理删除（不留双轨）。
