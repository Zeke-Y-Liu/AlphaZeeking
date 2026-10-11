"""引擎B · 20 日信号层包装器（组件化 T-R2）。

契约：委托 factors.compute_beta（公式零复制，grep 红线）；入参 schema 校验；
输出位域 state ∈ {above, below, neutral}（enum_fields，T-R2 契约扩展）。
调用点现状：tools.py factor_compute_signals / portfolio_review / sensitivity.py 快路径对拍。
"""
from __future__ import annotations

from .. import factors as F
from .base import EngineSpec, EngineWrapper

BETA_SPEC = EngineSpec(
    engine_id="engine_b_beta",
    version="v1 (compute_beta @ factors.py, T-R2)",
    input_schema={
        "secid": str, "bars": list, "as_of": str,
        "win": int, "band": float, "band_sigma": float,
        "bias_thresh": float, "vpd_thresh": float, "pct_window": int,
    },
    required=("secid", "bars"),
    enum_fields={"state": ("above", "below", "neutral")},
    note="引擎B 信号层：20日动量/MA20突破/高点触及/冷却；win/band 等参数 R1 敏感度扫描可配",
)

beta_engine = EngineWrapper(BETA_SPEC, F.compute_beta)      # 委托（公式体只在 factors.py）
