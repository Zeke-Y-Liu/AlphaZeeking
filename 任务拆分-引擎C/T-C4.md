# 引擎C-T4 · 集成层：portfolio_review 接入 + 报告 §2.5

## 目标
引擎C 进入组合评估三产物（review JSON / HTML / MD），降级不阻断评估。

## 核心交付
1. `tools.py` `portfolio_review` 返回体新增 `macro_liquidity` 字段：缺数据时 `{"status":"unavailable"}`（**评估不失败**——宏观数据缺失区别于行情数据，不进门禁）
2. `report.py`：
   - HTML 新增 **§2.5 宏观流动性**章节：三市场温度计（-2→+2 五档、档位中文映射、unavailable 灰条）+ 全局概览 + 指标明细表 + 口径/版本注记
   - MD 对应小节同步渲染
3. 温度计 HTML 结构：三态 + icon 映射（复用 data_risk 视觉语言：极端状态醒目提示）
4. as_of 对齐：引擎C 的 as_of 用组合 K 线实现（与评估同日）

## DoD 验收
- [x] 三产物 §2.5 均含引擎C（总验收 #4）
- [x] 降级演练：删数据再评估 → unavailable 灰条，评估流程正常完成、已恢复后自动回填
- [x] 报告快照单测（HTML 结构、档位映射、注记）
- [x] 全量回归零失败

## 本会话实测（2026-10-11，批次 1 实施记录）

| DoD | 实测结果 |
| --- | --- |
| §2.5 三产物 | review JSON `macro_liquidity` 字段（`_macro_block` 三态集成）+ HTML §2.5（温度计 grid/指标明细表/口径注记）+ MD 同步小节；快照测试断言嵌入位置（② 与 ③ 之间）与其余章节完好 |
| 降级演练 | 密封测试 4 条：缺失/indicators 空/损坏 JSON → unavailable 不抛异常不阻断评估；HTML 灰条（flag gray）+ MD 透明降级行 + 「采集后自动回填」指引；恢复后 review 自动重算回填 |
| 报告快照 | 档位三态 icon 映射（▲▲/▼▼ 极端醒目、▲/▼ 次级、● 中性、◇ 灰条）复用 data_risk 视觉语言；口径注记 v1 (T-C2) + as_of + 来源机构；零外部依赖（无 http/script/img） |
| 全量回归 | 83 passed 零失败（79 + 4） |

真实链路验证（主 sandbox，review as_of=2026-09-30）：
`macro_liquidity = {status:"ok", state:"neutral", global:0, markets:{A:-1, HK:"unavailable", US:0}, global_score:-0.149, as_of:"2026-09-30"}`
—— as_of 与评估同日（组合 K 线最后交易日，交付 4）；三线并列（引擎A 评分 / 引擎B 信号 / 引擎C regime）不参与权重。

**重构 1 处**：口径注记构造下沉 `factors.macro_scorer_notes`（T-C3 工具面 / T-C4 报告 / 测试 fixture 共用单一来源，`MACRO_SCORER_VERSION` 同步挪 factors.py）。

## 红线
透明降级（红线 2）；不决策（红线 1）；HTML 单文件零外部依赖（报告工程惯例）
