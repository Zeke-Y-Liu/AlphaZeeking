# T-L3 · 版本收敛：materialize/水位版本化 + lineage 守卫 + 周期 pin + refs 影响面

## 目标
版本管理闭环：口径变更 = 新 version，旧版本冻结可追溯；修复两处存量缺陷；被引用关系可查。

## 核心交付
1. **接管两处存量缺陷**：
   - `materialize` 全量 DELETE 收敛到版本维度：新版本物化只清本版本水位，旧版本行完整保留（升级不再抹掉旧版行）
   - `incremental` 水位带版本：每版本独立水位，升级后老版本不丢历史
2. **版本冻结**：版本一经发布（active），其物化行 md5 指纹冻结；`mom_20d_fl` 1.0.0→1.1.0 实测：12354 行差异属新版，v1.0.0 行 md5 bit 级不变
3. **lineage 强化守卫**：物化任务携带 lineage（dt/factor_id/version/git sha），消费端（M6 合成/回测）校验 lineage 完整性，缺 lineage 拒绝消费
4. **周期 pin**：回测/评估引用因子时 pin `(factor_id, version)`；版本升级后 pin 住的老报告重跑结果 bit 级一致（pin 规则回归 diff 仅版本元数据字段，数值全一致）
5. **refs 影响面**：`fl_refs` 登记被引用关系（哪些策略/回测/报告在用哪个版本）；降级/退役前强制影响面评估（输出引用清单）

## DoD 验收
- [ ] 版本冻结测试：引用 v1.0.0 的旧回测报告重跑 → 结果 bit 级一致（实施计划 DoD）
- [ ] 升级演练：1.0.0→1.1.0 全量重跑逐行一致性校验（仅新版行变更）
- [ ] refs 影响面：`time_robustness` 演练命中宽 5572 行（引用清单输出正确）
- [ ] 全量回归零失败

## 依赖与红线
- 依赖：T-L1（FactorMeta/version）；M1 materialize/incremental 通道
- 红线：旧版本永不物理删除（孤儿 orphan 标记由组件化 T-R4 写保护语义承接）；lineage 缺失一律拒绝消费
