"""引擎C · 宏观流动性温度计包装器（组件化 T-R2）。

引擎C T-C2 引用此语义：本层为 compute_macro_liquidity 的包装消费者——
契约：委托 factors.compute_macro_liquidity（公式零复制，grep 红线）；入参 schema 校验；
输出位范围五档 global/global_score ∈ [-2,+2]（"unavailable" 字符串自动跳过数值检查）。
纪律：市场条件变量——不决策、不参与权重（包装层只做契约，不做任何决策翻译）。
"""
from __future__ import annotations

from .. import factors as F
from .base import EngineSpec, EngineWrapper

MACRO_SPEC = EngineSpec(
    engine_id="engine_c_macro",
    version="v1 (compute_macro_liquidity @ factors.py, T-C2/T-R2)",
    input_schema={"indicators": dict, "as_of": str},
    required=("indicators", "as_of"),
    output_range=(-2.0, 2.0), range_fields=("global", "global_score"),
    note="引擎C 三市场温度计：冻结五键 + detail 非冻结扩展；缺失指标 insufficient 透明降级",
)

macro_engine = EngineWrapper(MACRO_SPEC, F.compute_macro_liquidity)   # 委托（公式体只在 factors.py）
