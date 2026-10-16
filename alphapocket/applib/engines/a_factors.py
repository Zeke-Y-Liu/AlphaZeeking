"""引擎A · 微观因子五维评分包装器（组件化 T-R2）。

契约：委托 factors.compute_alpha（公式零复制，grep 红线）；入参 schema 校验；
输出位范围 composite ∈ [0,100]（五维评分合成，越界告警不截断）。
调用点现状：tools.py factor_compute_alpha / portfolio_review / sensitivity.py 网格扫描
（T-R11 五面回归时评估是否统一切换入口，本层先行提供契约通道）。
"""
from __future__ import annotations

from .. import factors as F
from .base import EngineSpec, EngineWrapper

ALPHA_SPEC = EngineSpec(
    engine_id="engine_a_alpha",
    version="v1 (compute_alpha @ factors.py, T-R2)",
    input_schema={
        "secid": str, "bars": list, "reports": list, "dividends": list,
        "as_of": str, "weights": dict,
    },
    required=("secid", "bars", "reports", "dividends"),
    output_range=(0.0, 100.0), range_fields=("composite",),
    note="引擎A 五维评分：估值/质量/成长/动量/低波红利；ETF/LOF 无财报 → 估值类 None",
)

alpha_engine = EngineWrapper(ALPHA_SPEC, F.compute_alpha)   # 委托（公式体只在 factors.py）
