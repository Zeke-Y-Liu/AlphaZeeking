# 引擎C-T1 · 数据层：fetch_macro_liquidity + 合并策略登记

## 目标
12 宏观指标的可拉取、可落盘、可溯源、可增量（不丢历史）。

## 核心交付
1. `datasource.py` 新增 `fetch_macro_liquidity`：三市场 12 指标（A 股 6：M2/社融/Shibor/LPR/FR007/中美利差；港股 3：复用 H-0006 三件套；美股 5：Fed 利率/10Y-2Y/CPI/非农/GDP）
   - **本会话资产缺口（2026-10-11 盘点登记）**：`hk_rates.json`（H-0006 三件套产物）在本会话代码库不存在。实施时港股市场走 `unavailable` 透明降级（引擎C 设计内场景，旧会话 T-C5 实测同样出现过 HK 缺数据降级）；若需补齐港股指标，H-0006 数据获取任务前移，与本任务解耦
2. `_MACRO_` 数据集定义由 `datasets/` 声明式派生（组件化重构语义）；`macro_liquidity` 数据集登记 **carry_old 合并策略**（红线 4：空窗口/拉取失败 → 旧数据完整保留）
3. 落盘 `macro_liquidity.json` + provenance 机构名登记（中国人民银行/外汇交易中心/美联储等）
4. 增量语义：incremental 同步前后 12 指标条数逐项 diff 完全一致（1341/3860/6659/1435/64/1439/5667/5656 点基准）；冒烟窗口为空时自检保留验证
5. 接口契约速查：AKShare 列名/日期格式/排序方向全部实测后写死（18 探 12 中——探不通的 6 个登记不可用窗口黑名单）

## DoD 验收
- [x] 12 指标拉取落盘，provenance 齐全（总验收 #1）
- [x] 增量演练：断网/空窗口注入 → 旧数据零丢失（红线 4）
- [x] 全量回归零失败

## 本会话实测（2026-10-11，批次 1 实施记录）

| DoD | 实测结果 |
| --- | --- |
| 落盘与溯源 | **11 指标命中落盘**（A 6 + US 5）+ HK 3 件套按登记 unavailable 降级（`hk_rates.json` 缺失）；as_of=2026-10-10；provenance 机构名逐指标登记。行数：M2 224 / 社融 136 / Shibor 2380 / LPR 1538 / FR007 2933（240 天分页）/ 中美利差 5786 / Fed 291 / 美 10Y-2Y 8951 / CPI 223 / 非农 668 / GDP 209 |
| 增量防丢 | 密封测试 10 条全绿（carry_old 失败保留/空窗口保留/union 防丢/同 dt 新值覆盖/去重/降级不抛异常）；真实断网演练（死端口代理）：0 ok / **11 carried_old** / 逐项 diff = 0 零丢失 |
| 全量回归 | 64 passed 零失败（基线 54 + 宏观数据层 10） |

接口契约（AKShare 1.19.1 实测写死）：`macro_china_money_supply`（月份「2008年02月份」格式）/
`macro_china_shrzgm`（YYYYMM）/ `macro_china_shibor_all`（1W-定价列）/
`macro_china_lpr`（TRADE_DATE/LPR1Y）/ `repo_rate_hist`（date/FR007，窗口 ≤270 天须 240 天分页）/
`bond_zh_us_rate`（中美 10Y 双列求差 + 美 10Y-2Y 单列，同源共享缓存）/
`macro_bank_usa_interest_rate`（今值 dropna）/ `macro_usa_cpi_yoy`（时间/现值）/
`macro_usa_non_farm` / `macro_usa_gdp_monthly`（今值 dropna）。
组件化两步走：`_MACRO_DATASET` 模块常量单点声明（T-R6 迁移 DatasetSpec）。

## 红线
不决策 · 透明降级 · provenance 登记 · 增量防丢（见 00-总览 §6）
