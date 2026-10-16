# 组件化重构-T8 · fetch_all(mode=) 三模式显式

## 目标
fetch_all 三模式显式化：bootstrap / incremental / refresh，消灭隐式默认行为。

## 核心交付
1. **mode 参数三模式语义冻结**：
   - `bootstrap`：全量重建（首次落地/灾后重建）
   - `incremental`：增量合并（日常同步，走 merge_policy）
   - `refresh`：合并式刷新（**替换最近 5 日而非整体覆盖**——历史修复成果的固化）
2. **默认模式安全选择**：无显式 mode 时的默认值 + 告警（防止误触覆盖）
3. **与 merge_policy 联动**（T-R7）：三模式全部经单一合并机制，无旁路

## DoD 验收
- [x] 三模式实测 diff：同数据下三种行为差异符合语义声明
- [x] 增量不丢历史：同步前后条数逐项 diff 一致
- [x] registry 写保护联动（T-R4）：三模式写库均不覆盖用户改动

## 本会话实测（2026-10-11，批次 2 实施记录）

| 交付 | 实测结果 |
| --- | --- |
| mode 参数三模式 | [datasource.py](file:///workspace/alphapocket/applib/datasource.py) `fetch_all(…, mode=)`：bootstrap（rmtree 全量重建）/ incremental（**不 rmtree**——单数据集失败旧文件保留；起点缺省自动从库内最末 dt 推导 `incremental_auto`）/ refresh（**7 自然日窗口≈最近 5 交易日**重拉，同 dt 覆盖=点位修正） |
| 默认模式安全 | `_resolve_mode` 纯函数：mode=None 按参数推导 + `mode_warning` 告警（防误触覆盖）；非法 mode / bootstrap·refresh 同传 incremental_from → **ValueError 显式拒绝（无旁路）**；CLI `ap fetch --mode {bootstrap,incremental,refresh}`（choices 校验） |
| 与 merge_policy 联动 | 三模式全走 snapshot_guard→写库→restore_guard 单一机制（T-R7 spec 驱动），无旁路；`provenance.mode/mode_source` 登记可追溯 |
| **真实端到端**（副本+显式 refresh） | mode=refresh/explicit 零告警；窗口起点 2026-10-04（today-7d）✓；**历史零丢失 12534→12552**（窗口 2 根/标的 union 合入）✓；用户改动保留（paused+owner）✓；provenance.mode=refresh ✓；财务/市值 overwrite 数据集全量重拉=修正，符合语义 |
| 单测 | 新增 [test_fetch_modes.py](file:///workspace/alphapocket/tests/test_fetch_modes.py) 9 条：_resolve_mode 8 分支（3 显式/非法/2 冲突/2 推导+告警）+ _refresh_window 窗口值 + fetch_all 网络前拒绝 ×2 + refresh 语义 diff（历史不动+点位修正+新 dt+用户改动保留）+ incremental 不 rmtree 语义 |
| 回归 | α **133 passed**（124+9）零失败 |

**诚实登记**：真实 bootstrap/incremental 端到端证据沿用 T-C1（bootstrap 全量 12534 点）/T-R6·T-R7（incremental 12534→12550）实测，本轮未重复全量拉取；refresh 为本轮新增实跑。

## 红线
refresh 永不整体覆盖（红线：增量防丢）；无旁路写库（所有 fetch 路径必经 mode 显式）。
