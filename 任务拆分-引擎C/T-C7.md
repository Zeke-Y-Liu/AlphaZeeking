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
- [x] 前端单测：三态渲染 + 字段映射 + 档位中文映射全绿（web 100 基准内）
- [ ] e2e：含 macro_liquidity 夹具 7 passed，unavailable 灰条分支可复现（**夹具与断言已就位，实跑受阻**——playwright chromium CDN 下载被沙箱网络阻断，6 分钟零进展；待有网环境 `pnpm exec playwright install chromium` 后复核）
- [x] types 与冻结契约逐字段 diff 一致（无自创字段）
- [x] 卡片文案 grep 无决策性动词（买入/卖出/调仓）

## 本会话实测（2026-10-11，批次 1 实施记录）

| 交付 | 实测结果 |
| --- | --- |
| types | [types.ts](file:///workspace/alphapocket-web/web/src/api/types.ts) 新增 `MacroLiquiditySummary`（冻结五键逐字段对齐 T-C6 `_macro_summary` 输出）+ `MacroLiquidityBlock`（review.json 全量块扩展键索引签名）；`RunSummary.summary` 与 `ReviewPayload` 分别挂五键/全量块类型 |
| 卡片三态 | [PortfolioDetailView.vue](file:///workspace/alphapocket-web/web/src/views/PortfolioDetailView.vue) 「引擎C · 宏观流动性」卡片（引擎B 之后、指引之前）：ok → 全局档位徽章（五档中文+tone：宽松 success/偏紧 danger/紧缩 danger…）+ score(3 位小数) + 三市场徽章 + as_of；unavailable → 「宏观数据缺失，评估继续」灰条；error → 结构异常兜底「不阻断评估」；旧 run 缺字段 → unavailable（向后兼容） |
| 视觉边界 | 注记「市场条件参考 · 不参与决策、不参与权重」；单测断言卡片文本不含 买入/卖出/调仓 |
| 单测 | spec 新增 4 用例：ok 态字段映射（偏松/0.458/A股 偏松/港股 中性/美股 偏松/as_of）+ unavailable 态不阻断（体检卡仍在）+ error 态兜底 + 决策动词 grep + 旧 run 兼容；**web 全量 99 passed 零失败**；`vue-tsc --noEmit` 零错误 |
| e2e 夹具 | [api-mock.ts](file:///workspace/alphapocket-web/e2e/fixtures/api-mock.ts) 补 `MACRO_BLOCK_OK/NA` + `MACRO_SUMMARY_OK/NA` 两形态（T-C2 全量块 + T-C6 五键），buildReview/upsertRun 按 pid 分流：**multi5（剧本 A）= 正常态温度计断言、cn4（剧本 B seed 回归）= unavailable 灰条断言**——两分支 e2e 可复现断言已写入 scenario-a/b.spec |

**环境备注**：web `.venv`/node_modules 本会话重建（pnpm 13.4s）；e2e 依赖已装、chromium 下载被网络阻断（ms-playwright cache 卡 16K）。e2e 断言与单测覆盖同一渲染分支，实跑复核不阻塞后续任务。

## 红线
不决策（前端只呈现不引导）；消费冻结契约（字段改动须走 T-C6 契约修订）；
unavailable 必须可见但不阻断页面（透明降级语义）。
