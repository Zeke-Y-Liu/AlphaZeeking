# T-C5 · 报告端：三块报告渲染 + factor_backtest 扩展 + CLI 对接 + 三份内置策略 YAML

## 目标
策略回测报告升级为三块结构，扩展 factor_backtest.py 字段，CLI 打通，内置三策略落盘。

## 核心交付
1. **三块报告渲染**（backtest.py 报告层）：
   - 年度收益分布（逐策略逐年）
   - 因子暴露（factor_exposure 字段）
   - 含/不含成本双列卡玛（口径：max_dd 为负值，断言方向不得写反——旧会话修复 #3）
2. **factor_backtest.py 扩展**：新增 `factor_exposure` / `turnover` 字段（T-C3 ledger 消费端）
3. **CLI 对接**（cli.py）：`ap strategy backtest <yaml>`——加载→合成→撮合→报告一条链；修复 `fl_refs` → `fl_duckdb` 路径（spduckdb 同文件双配置冲突，M5 已修，此处回归保持）
4. **三份内置策略 YAML**（kebab-case，近似版披露）：
   - 低波红利近似版（池内因子）
   - PE-ROE 近似版（池内 3 因子：`mom_20d_fl` / `sig_mom20_rank_fl` / `val_pe_ttm`）
   - 多因子共振近似版（四法合成联测）
   - YAML 头部注释：近似版声明 + M7 替换清单指针（`low_vol_252d` / `div_payout_streak` / `gro_np_yoy`）
5. **T-C5.md 契约书**：同步实际落盘口径（排版表格化）

## DoD 验收
- [ ] 单测 16 条：报告渲染快照、双列卡玛口径（含成本/不含成本）、factor_exposure/turnover 字段、CLI 端到端
- [ ] 三份 YAML `ap strategy validate` + `ap strategy backtest` 全跑通；跑不通的如实登记失效清单（不得改入参凑数）
- [ ] 全量回归零失败（407 → 519 的增量主体在本任务与 T-C4）

## 依赖与红线
- 依赖：T-C1~T-C4 全链
- 红线：近似版必须显式披露（YAML 注释 + 报告页脚）；报告数字与 ledger 逐位对得上（不做二次加工）
