# 组件化重构-T5 · DatasetSpec 声明式数据集规格

## 目标
applib/datasets/ 声明式数据集：新增数据集只写声明、不写代码。

## 核心交付
1. **DatasetSpec schema 冻结**（单点声明五要素）：
   - 路径（落盘位置）
   - 合并策略（merge_policy 引用，T-R7 收敛后的单一机制）
   - 入参（拉取参数 / 频率 / 窗口）
   - 多源（主源 / 备源 / 降级序）
   - 验证联动（data_missing 检测声明，T-R10 消费）
2. **加载与校验器**：spec 加载 → schema 校验 → 非法 spec 拒绝（错误信息可定位）
3. **spec→fetch 驱动**：fetch_all 按 spec 驱动，代码中不再出现数据集定义

## DoD 验收
- [x] schema 单测：合法 spec 通过 / 非法 spec 逐字段拒绝
- [x] 校验器错误信息含字段定位
- [x] 现有硬编码数据集定义已列入迁移清单（T-R6 执行）

## 本会话实测（2026-10-11，批次 2 实施记录）

| 交付 | 实测结果 |
| --- | --- |
| DatasetSpec schema 冻结 | [datasets/\_\_init\_\_.py](file:///workspace/alphapocket/applib/datasets/__init__.py)：五要素 `path / merge_policy / fetch_params / sources(SourceSpec 降级序) / validation(data_missing 声明)`；`KNOWN_MERGE_POLICIES` 冻结枚举（T-R7 收敛后降级为引用）；`SourceSpec{interface,upstream,role∈{primary,fallback,crosscheck},params}` |
| 校验器 | `validate_spec` 逐字段 raise `DatasetSpec.<field>: 原因`（可定位）；`load_specs` 文件加载双重定位（`DatasetSpec[bad]: DatasetSpec.path: ...`）；path 白名单（相对 repo / 禁 .. / 仅 .json） |
| 驱动通道 | `merge_by_policy`（策略→既有实现委托表 `MERGE_POLICY_FN`，**零第二实现**：union_bars/registry_merge/portfolio_merge 复用 writeguard；carry_old 显式 `NotImplementedError` 拒绝待 T-R6 收编）+ `persist_spec`（相对 repo 落盘） |
| 迁移清单 | `PENDING_MIGRATION` 15 项（fetch_all `_reg` 14 处 + macro_liquidity carry_old）+ seed.py 离线种子豁免登记（非拉取数据集不迁） |
| 单测 | [test_datasets_spec.py](file:///workspace/alphapocket/tests/test_datasets_spec.py) 7 用例：合法通过（全要素+最小 spec）/ 非法逐字段拒绝 14 分支 / 加载器双重定位 / 策略委托行为等价（含用户字段胜出回归）/ 未注册策略显式拒绝 / 相对落盘 / 迁移清单完整性 |
| 过程修复 | 2 处测试侧（merge_bars 接链语义实际生效——resume 断言方向修正、重算行定位）；1 处实现侧相对导入层级（`from .`→`from ..`） |
| 回归 | α **114 passed**（107+7）零失败 |

## 红线
单点声明（第二实现=违规）；schema 冻结（字段改动须走契约修订）。
