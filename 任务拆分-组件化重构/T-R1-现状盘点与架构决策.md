# T-R1 · 现状盘点与架构决策（组件化重构 · 本会话批次 2 起点）

> 入库可追溯（T-R1 红线）：本文档为组件化重构的架构单一事实源；后续任务不得推翻既定契约，
> 修订须在本文档登记。盘点基线 = 提交 `009624d`（引擎C 8/8 收口后，α 83 / core 180+12s / web 99）。

## 1. 现状盘点（文件 + 函数级定位）

### 1.1 合并策略散落 6 处（T-R4/T-R7 收敛对象）

| # | 位置 | 现语义 | 问题 |
| --- | --- | --- | --- |
| 1 | datasource.py `fetch_all` L781-783 | `shutil.rmtree(repo)` **整体重建**云端 main | 用户人工登记（provenance 手工注记、组合等）被清空 |
| 2 | datasource.py `fetch_all._reg` L879-885 | 逐 dataset **覆盖写** JSON 文件 | 无 merge；同文件多源写互相覆盖 |
| 3 | datasource.py `fetch_all._start_for` L833-838 | 增量「次日起」拉取后整体写 | 增量窗口外旧点不保留（靠上游接口全量窗兜底） |
| 4 | datasource.py `fetch_macro_liquidity` L1387-1447 | carry_old + union 合并（引擎C 自带一套） | 合并语义第三实现；与 ①~③ 不共享 |
| 5 | data.py `data_sync` L15-85 | drop+create+insert（DuckDB 幂等重建） | 同职责异实现（数据面整体重建） |
| 6 | repo.py `_store_blob`/`make_commit` | blob 存在即跳过（union 语义之一） | 内容寻址幂等与文件合并语义混在版本层 |

### 1.2 数据集定义硬编码点（T-R5/T-R6 收敛对象）

| # | 位置 | 内容 |
| --- | --- | --- |
| 1 | datasource.py `fetch_all` ①~④ 序号块 L887 起 | 交易日历/A股/港股/美股/财务/分红/市值 采集编排行内声明 |
| 2 | datasource.py `CN4`/`MULTI5` L49/L58 | universe 常量硬编码 |
| 3 | datasource.py `_MACRO_DATASET` L1220 | 引擎C 12 指标（已声明「T-R6 迁 DatasetSpec 后收编」） |
| 4 | datasource.py `PIT_LAG_DAYS`/`FOREIGN_PIT_LAG_DAYS` L72/L101 | PIT 披露窗口硬编码（provenance 行内注记同源） |
| 5 | data.py `data_sync` L26-42 | DuckDB 表结构行内硬编码（DatasetSpec 验证联动位） |

### 1.3 fetch_all 覆盖式写库点

L781-783 `rmtree` + `_reg` 覆盖写 + `_start_for` 增量整体写（详见 1.1 #1~#3）。
**用户改动被吞的实锤路径**：clone 后人工修改 `sandbox/repo` 任何 JSON → 下次 `ap fetch` 全量重建 repo → 人工改动丢失。

### 1.4 公式与引擎耦合点（T-R2 包装层对象；公式本身不动）

调用点（compute_x 纯函数被直调，无契约层——无入参校验/无输出位范围/无版本绑定）：

| 调用方 | 位置 | 公式 |
| --- | --- | --- |
| tools.py | L43/L71/L115/L137-138/L505 | compute_beta / compute_alpha / compute_macro_liquidity |
| sensitivity.py | L157/L288 | compute_alpha（±30% 网格）/ compute_beta（快路径对拍） |
| report.py | 渲染层直读输出 dict 键 | 依赖输出结构但无契约声明 |

公式本体（**红线：一行不动**）：factors.py `compute_alpha` L160 / `compute_beta` L255 /
`compute_macro_liquidity` L515 / scorer 族 L422-510。

## 2. 架构决策（为什么不搬家）

1. **回归面**：compute_x 输出被 α 单测精确数值断言（如 T-C2 双期排位手工复算）与
   seed 快照双重锚定；搬家即改 import 路径 → 全部数值断言与快照重生成 = 回归面爆炸。
2. **可测性**：纯函数无 I/O，包装层无状态——两层各自独立单测（T-R2 DoD「包装层无状态」）。
3. **耦合的正确解法**：耦合问题不在「函数被调用」，在「调用无契约」。收敛手段 =
   包装层（schema 校验/位范围/版本绑定/provenance），而非物理移动公式。
4. **收编而非重写**：`_MACRO_DATASET`（T-C1 已单点声明）→ T-R6 等价迁移 DatasetSpec；
   引擎C carry_old → T-R7 merge_policy 单实现收编（行为 diff 等价验证）。

## 3. engines/ 契约冻结（T-R2 起不得推翻）

| 契约 | 内容 | 实现 |
| --- | --- | --- |
| 1 委托 | 包装器禁止复制公式体，只委托 factors.compute_x | `EngineWrapper._fn` |
| 2 入参校验 | required + schema 类型校验，失败拦截在包装层（不进计算函数） | `validate_input` |
| 3 输出位范围 | `range_fields` × `output_range`，越界 → `_range_warnings`（告警不截断不阻断） | `check_output_range` |
| 4 无状态 | 包装器实例零可变状态（同实例串行调用互不影响） | 单测 `test_wrapper_stateless` |
| 5 provenance | 每次调用登记 engine_id/version/status/elapsed_ms → 输出 `_engine` 键 | `EngineWrapper._prov` |

骨架产物：applib/engines/{__init__.py, base.py} + tests/test_engines_base.py（7 用例）。

## 4. 与旧会话基线的对齐说明

旧会话 T-R1 盘点的「6 处合并」为本会话等价定位（重构前旧会话代码面更大，但四处主锚点
`rmtree`/`_reg`/`_start_for`/carry_old 在本会话代码中逐行可复现）；DatasetSpec/merge_policy
收敛对象以 1.1/1.2 清单为准。基线差异（α 83 vs 旧 226）已在引擎C 总验收 §7.1 登记，不再重复。
