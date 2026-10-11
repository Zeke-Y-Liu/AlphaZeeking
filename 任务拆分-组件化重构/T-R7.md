# 组件化重构-T7 · merge_policy 六改单收敛

## 目标
六处散落的合并实现收敛为单一 merge_policy 参数化机制。

## 核心交付
1. **六处清单与语义提取**：逐一登记位置与行为（含已知策略：增量合并转增股 / 尾部豁免三分支 /
   refresh 替换最近 5 日 / carry_old 填基保留旧值等）
2. **merge_policy 参数化实现**：策略=显式参数（enum/表驱动），单一入口单一实现
3. **行为等价对照**：每旧实现→新机制逐行为 diff（同输入同输出，含边界：空窗口/失败重试/转增股）

## DoD 验收
- [x] 六旧语义逐项等价 diff 全绿（零回归红线 4 的主证据）
- [x] 每策略至少 1 条单测（含边界分支），test_datasource_merge 基线不降
- [x] grep 旧合并实现无残留（收敛=物理删除旧路径）

## 本会话实测（2026-10-11，批次 2 实施记录）

**六处口径对齐**（旧会话六处 vs 本会话代码库实际——如实登记，不虚构）：

| 旧会话散落点 | 本会话处置 |
| --- | --- |
| restore_guard 内联 if/elif（registry/portfolio/bars 三分派） | **T-R7 收敛**：spec.merge_policy 单一机制（`GUARDED_RELS` 反查 spec → `merge_by_policy`） |
| fetch_all `_reg` 覆盖写 | T-R6 已收敛（spec 驱动登记） |
| 引擎C carry_old 内联 | T-R6 已物理迁移（datasets.carry_old_merge 单点） |
| data_sync DuckDB 重建 / refresh 最近 5 日 / 增量合并转增股 | **本会话代码库无对应模块**（旧会话 data_status/refresh 未重建；qfq 序列上游直出复权价，无独立转增股合并语义）→ 不适用登记，未来重建时按本机制接入（新策略只能注册 MERGE_POLICY_FN） |

| 交付 | 实测结果 |
| --- | --- |
| 注册表单一事实源 | [datasets/\_\_init\_\_.py](file:///workspace/alphapocket/applib/datasets/__init__.py)：`MERGE_POLICY_FN` 补 overwrite(None=新内容即最终态)+carry_old；`KNOWN_MERGE_POLICIES = tuple(MERGE_POLICY_FN)`（新增策略只注册本表，校验自动跟随）；签名统一 `(old,new)->(merged,info)`（registry/portfolio 适配器摘出 `_merge`）；`MERGE_POLICY_FORMAT` 落盘格式声明化 |
| restore_guard spec 驱动 | [writeguard.py](file:///workspace/alphapocket/applib/writeguard.py)：内联 if/elif 消除；落盘格式/summary 组装查表（等价锚点：registry/portfolio 缩进 2+info 进 summary、bars 紧凑+dt_total、`_merge` 不落盘）；**无声明守护面文件 → 显式登记 unspecified 跳过不静默**；延迟 import 防循环 |
| 行为等价对照 | **真实端到端**：注入用户改动（mom_12_1 paused+owner）+ orphan 假因子 → 增量 fetch：①保留+orphan 标记（orphaned_at=2026-10-11）✓ ②`_merge` 不落盘+缩进保持 ✓ ③K线 12534→12550 不丢 ✓ |
| 单测（每策略边界） | union_bars：历史保留/同 dt 覆盖+pct 接链/未拉 secid 保留/**空窗口全保留**（新补）；registry_merge：用户优先+orphan/无冲突=新/**机器字段新胜**（新补）；portfolio_merge：条目+顶层；carry_old：拉取失败/空窗口/全失败 3 条；overwrite：直通；未注册：显式拒绝 |
| grep 无残留 | restore_guard 内联分派已物理删除；`PENDING_MIGRATION` 兼容别名清理（引用方统一 `MIGRATION_LOG`） |
| 回归 | α **124 passed**（115+9）零失败（T-R4 三语义 8 条+T-R5/T-R6 spec 链路全保持=行为等价） |

## 红线
零行为回归；新策略只能通过 merge_policy 参数新增（不得再写第二实现）。
