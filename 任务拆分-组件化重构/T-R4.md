# 组件化重构-T4 · registry 写保护：覆盖→merge / 用户优先 / orphan 标记

## 目标
终结 fetch_all 覆盖式写库：registry 合并语义三件套。

## 核心交付
1. **覆盖→merge**：fetch_all / 数据同步改写库时走合并，字段级区分机器产出与用户登记
2. **用户改动优先**：merge 冲突时用户改动胜出，机器数据不得覆盖人工（参数/名称/启停等人工字段）
3. **orphan 标记不物理删**：上游消失的条目标记 orphan，保留可审计（M5 T-L3 旧版本 orphan 语义承接此机制）

## DoD 验收
- [x] 覆盖→merge 行为单测：同步前后用户字段逐项保留
- [x] orphan 标记实测：物理文件/记录仍在，标记可查询
- [x] 增量同步前后条数 diff 不丢历史

## 本会话实测（2026-10-11，批次 2 实施记录）

| 交付 | 实测结果 |
| --- | --- |
| 写保护守护层 | [writeguard.py](file:///workspace/alphapocket/applib/writeguard.py) 快照-恢复模式：`snapshot_guard`（rmtree 前抢救 6 守护面）+ `restore_guard`（拉取后合并回写）——fetch_all 主体零改动（架构决策：守护层加契约而非重写数据流），返回体新增 `write_guard` 审计块 |
| 三语义 | ①覆盖→merge：`merge_registry`/`merge_portfolio`（条目级字段合并）+ `merge_bars`（union by dt，同 dt 新覆盖旧=点位修正）；②用户改动优先：old≠new 且非 MACHINE_FIELDS → 保 old（红线 2），`_merge.user_fields_kept` 逐项登记；③orphan 标记不物理删：消失条目 `orphan:true`+`orphaned_at`（红线 3，M5 T-L3 承接此机制） |
| fetch_all 接线 | [datasource.py](file:///workspace/alphapocket/applib/datasource.py) 两处最小侵入（L781 快照 / L1188 恢复）；守护面：registry + portfolio×2 + daily_bars/bars_raw/cross_check_tx（财务/分红/日历为全量重拉不入守护面） |
| 单测 | [test_writeguard.py](file:///workspace/alphapocket/tests/test_writeguard.py) 8 用例：registry 用户字段胜出+机器字段新胜+自定义字段保留+orphan 可查询 / 无冲突等价 / portfolio 顶层+positions / bars union 不丢历史 / 同 dt 新覆盖 / pct 接链 resume / 未拉 secid 整段保留 / 快照-恢复端到端 / 损坏文件不炸；过程修复 1 处实现侧（`seq.update(oldmap)` 方向写反致新行被旧覆盖） |
| **真实端到端**（生产 repo 副本+真实增量 fetch 21s） | 用户改动（mom_12_1 status=paused+owner）**真实增量后保留**且 guard 逐项登记；K 线 **12534→12552 不丢历史**（601058 增量 2 根 2026-09-30→2026-10-09，历史 1393→1395）；**未增量 8 secid 全保留**（修复旧逻辑下未列出 secid K 线全丢 bug）；pct 接链 resume 登记生效 |
| 回归 | α **107 passed**（99+8）零失败 |

## 红线
用户改动优先（红线 2）；不物理删（红线 3）；merge 行为单测覆盖冲突/无冲突/orphan 三分支。
