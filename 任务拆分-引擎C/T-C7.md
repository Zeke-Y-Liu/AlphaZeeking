# 引擎C-T7 · 前端卡片：PortfolioDetailView 引擎C卡片三态 + types + e2e 夹具

## 目标
Web 端 `PortfolioDetailView.vue` 渲染引擎C宏观流动性卡片：消费 T-C6 冻结契约
`runs.summary.macro_liquidity`，三态呈现，不引入任何决策语义。

## 核心交付
1. **types**：`MacroLiquiditySummary` 类型定义与冻结契约逐字段对齐
   （`state` / `global` / `markets{A,HK,US}` / `global_score` / `as_of`，向后兼容只增不改）
2. **卡片三态**：
   - 正常态：温度计五档（-2→+2）中文档位映射，三市场 A/HK/US 分档 + global_score（如 0.458）+ global 档 + as_of
   - `unavailable` 态：灰条降级提示「宏观数据缺失，评估继续」（区别于行情数据门禁的失败语义）
   - `error` 态：单测覆盖渲染分支（真实验链路不可达，透明降级兜底）
3. **视觉边界**：卡片置于评估报告区域，文案不出现任何买卖/调仓暗示（红线 1 的前端延伸）
4. **e2e 夹具**：fixture 补 `macro_liquidity` 基线，e2e 7 passed 复核（含 unavailable 分支截图级断言）

## DoD 验收
- [ ] 前端单测：三态渲染 + 字段映射 + 档位中文映射全绿（web 100 基准内）
- [ ] e2e：含 macro_liquidity 夹具 7 passed，unavailable 灰条分支可复现
- [ ] types 与冻结契约逐字段 diff 一致（无自创字段）
- [ ] 卡片文案 grep 无决策性动词（买入/卖出/调仓）

## 红线
不决策（前端只呈现不引导）；消费冻结契约（字段改动须走 T-C6 契约修订）；
unavailable 必须可见但不阻断页面（透明降级语义）。
