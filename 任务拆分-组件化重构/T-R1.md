# 组件化重构-T1 · 架构决策与 engines/ 骨架

## 目标
定案组件化架构：公式不搬家原则、engines/ 包装层契约冻结、现状盘点。

## 核心交付
1. **现状盘点清单**（落文档）：合并策略散落 6 处的位置、数据集硬编码点、fetch_all 覆盖式写库点、公式与引擎耦合点
2. **engines/ 目录骨架**：包装器基类 + 契约冻结（id / 版本 / 入参 schema / 输出位范围 / provenance）
3. **架构决策记录**：为什么不搬家（factors.compute_x 纯函数已是单测/seed 可复现资产，搬家=回归面爆炸）

## DoD 验收
- [x] 盘点清单逐项可定位（文件+函数级）
- [x] 骨架空实现 + 契约单测通过
- [x] 旧路径全量回归不动（重构前基线先固化）

## 本会话实测（2026-10-11，批次 2 实施记录 · 基线 009624d）

| 交付 | 实测结果 |
| --- | --- |
| 盘点清单 | [T-R1-现状盘点与架构决策.md](file:///workspace/任务拆分-组件化重构/T-R1-现状盘点与架构决策.md) §1：合并策略散落 **6 处**（fetch_all rmtree L781 / _reg L879 / _start_for L833 / macro carry_old L1387 / data_sync 重建 / repo blob union）+ 数据集硬编码 **5 点**（fetch_all 序号块 / CN4·MULTI5 / _MACRO_DATASET / PIT 窗口 / DuckDB 表结构）+ 覆盖式实锤路径（clone→人工改→ap fetch 全量重建=改动丢失）+ 公式耦合调用点（tools 6 处 / sensitivity 2 处）逐行登记 |
| 骨架 | [engines/base.py](file:///workspace/alphapocket/applib/engines/base.py)：`EngineSpec`（frozen dataclass：id/版本/schema/required/output_range/range_fields）+ `EngineWrapper`（validate_input → 委托 → check_output_range → `_range_warnings` 告警不截断 + `_engine` provenance）+ `validate_input`/`check_output_range` 独立函数 |
| 契约单测 | [test_engines_base.py](file:///workspace/alphapocket/tests/test_engines_base.py) 6 用例全绿：校验四分支/位范围四分支/委托透传/拦截不进计算/越界不截断/无状态 |
| 基线固化 | α **89 passed**（83 + 骨架 6）零失败——重构前回归基线；factors.py 零改动（grep 验证） |
| 决策记录 | §2 四条（回归面/可测性/耦合解法=包装层而非搬家/收编而非重写）+ §3 契约五条冻结（T-R2 起不得推翻，修订须登记）+ §4 旧会话基线对齐说明 |

## 红线
不动 factors.compute_x 任何一行；决策记录入库可追溯（后续任务不得推翻既定契约，修订须登记）。
