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
- [ ] scorer 表逐指标单测（方向/触发/跳平/回退四路径）
- [ ] 缺值归一化：构造 HK 缺指标用例 → HK 档位由剩余维度决定并标注
- [ ] 输出结构符合冻结契约（幂等重算一致）
- [ ] 全量回归零失败

## 红线
纯函数（不读库不读网）；不决策；as_of 用组合 K 线实现（不取未来）
