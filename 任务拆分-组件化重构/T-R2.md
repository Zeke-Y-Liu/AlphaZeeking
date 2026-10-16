# 组件化重构-T2 · engines/ 包装层落地：三引擎包装器

## 目标
三引擎（A 微观因子 / B 信号层 / C 宏观流动性）包装器实现：公式原位、包装层只做契约。

## 核心交付
1. 包装器**委托** factors.compute_x 纯函数（禁止复制公式体）
2. **输出位范围约束**：如引擎C -2→+2 五档、引擎A 因子值域声明（越界→显式告警不静默截断）
3. **入参校验**：schema 校验失败在包装层拦截（不进计算函数）
4. 引擎C `engines/macro.py` 即本层消费者（引擎C T-C2 引用此语义）

## DoD 验收
- [x] 包装层单测：委托 / 入参校验 / 位约束三面全绿
- [x] grep engines/ 无公式复制（公式体仍只存在于 factors.py）
- [x] α 套件回归不红

## 本会话实测（2026-10-11，批次 2 实施记录）

| 交付 | 实测结果 |
| --- | --- |
| 三包装器 | [a_factors.py](file:///workspace/alphapocket/applib/engines/a_factors.py) `alpha_engine`（委托 compute_alpha；composite ∈ [0,100]）/ [b_signals.py](file:///workspace/alphapocket/applib/engines/b_signals.py) `beta_engine`（委托 compute_beta；state 枚举位域）/ [c_macro.py](file:///workspace/alphapocket/applib/engines/c_macro.py) `macro_engine`（委托 compute_macro_liquidity；五档 [-2,+2]，"unavailable" 自动跳过数值检查）——绑定 compute_x **本体 identity 断言**（`eng._fn is F.compute_x`） |
| 契约扩展 | EngineSpec 新增可选 `enum_fields`（引擎B state 字符串位域）——**不推翻 T-R1 五条**，已登记 T-R2.md 与 base.py docstring |
| 单测三面 | [test_engines_wrappers.py](file:///workspace/alphapocket/tests/test_engines_wrappers.py) 4 用例：委托 identity + **grep 红线自动化**（engines/ 源码静态检查 10 个公式特征片段，CI 内每次执行）；校验拦截（缺参/类型错 spy 证明公式零调用）；**真实公式冒烟位内性质**（260 根合成 K 线跑引擎A / 30 根跑引擎B / 合成指标跑引擎C，输出键与位域断言 + provenance `_engine`）；越界告警不截断 |
| 过程修复 | 2 处测试侧修正（公式零改动）：合成 series 键名 `v`→`value`（对齐 T-C1 落盘格式）；全缺指标 `global_score=None` 契约形态断言放行 |
| 回归 | α **93 passed**（89+4）零失败 |

## 红线
公式不搬家（T-R1 决策）；包装层无状态（可独立单测）；越界告警不阻断产出。
