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
- [ ] 三模式实测 diff：同数据下三种行为差异符合语义声明
- [ ] 增量不丢历史：同步前后条数逐项 diff 一致
- [ ] registry 写保护联动（T-R4）：三模式写库均不覆盖用户改动

## 红线
refresh 永不整体覆盖（红线：增量防丢）；无旁路写库（所有 fetch 路径必经 mode 显式）。
