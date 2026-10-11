# 引擎C-T2 · 计算层：compute_macro_liquidity 纯函数 + 三层聚合

## 目标
从 12 指标时序到三市场温度计（-2→+2 五档）与全局概览的确定性计算。

## 核心交付
1. `factors.py` 新增 `compute_macro_liquidity` **纯函数**（无 IO、可单测、seed 可复现）；另有输出位范围约束的 `engines/macro.py` 包装器（组件化 T-R2/R3 语义）
2. **三层聚合**：
   - **scorer 表**：每指标一张——分位点算方向 / 阈值触发 / 阈值跳平 / 回退；月度指标 12 月滚动、用前值站位（防前视）；新+旧双期排位
   - **维度等权**：D1 央行注入 / D2 货币总量 / D3 价格信号 / D4 跨境流动；HKG/US 缺值做归一化（缺维度不折损已知维度）
   - **检验状态机**：neutral / 偏松 / 偏紧 / unavailable（档位中文映射）
3. 输出结构（冻结）：`{state, global, markets: {A: level, HK: level, US: level}, global_score, as_of}`——如 `{state:"neutral", global:1, markets:{A:1,HK:0,US:1}, global_score:0.458, as_of:"2026-10-09"}`
4. 温度计五档：-2 → +2，含 unavailable

## DoD 验收
- [x] scorer 表逐指标单测（方向/触发/跳平/回退四路径）
- [x] 缺值归一化：构造 HK 缺指标用例 → HK 档位由剩余维度决定并标注
- [x] 输出结构符合冻结契约（幂等重算一致）
- [x] 全量回归零失败

## 本会话实测（2026-10-11，批次 1 实施记录）

| DoD | 实测结果 |
| --- | --- |
| scorer 四路径 | 密封测试 9 条全绿：方向（同分位 dir=±1 镜像）、触发（极端分位 → ±2）、跳平（中位 p=0.5 → 0 档）、回退（样本不足 → 0 档 insufficient，聚合排除）+ no_data；双期排位手工可复算（cur=75 p=2.5/12 / prev=85 p=4.5/12） |
| 缺值归一化 | HK partial（仅 D3 一项可用）→ HK 档位=D3 可用项档位，D4 no_data 标注；全缺 → unavailable（不虚报中性） |
| 冻结契约 | 五键 `{state, global, markets{A,HK,US}, global_score, as_of}`；幂等重算一致；档位 ∈ -2..2 ∪ unavailable、score clamp [-1,1]；无 signals 字段 |
| 全量回归 | 73 passed 零失败（T-C1 后 64 + T-C2 新增 9） |

真实产物冒烟（T-C1 拉取 11 指标，as_of=2026-10-10）：
`{state:"neutral", global:0, markets:{A:-1, HK:"unavailable", US:0}, global_score:-0.136}`
—— A 偏紧倾向（宽货币弱需求：Shibor/FR007 低分位 +2 对冲 M2/社融/中美利差 -2）、US 中性（Fed/非农/CPI 混合）、月度指标 carried_prev 站位标注正常。
组件化两步走：`engines/macro.py` 包装器语义由 factors.py 注释承载（输出位范围约束内建于函数契约），T-R2/R3 收编。

## 红线
纯函数（不读库不读网）；不决策；as_of 用组合 K 线实现（不取未来）
