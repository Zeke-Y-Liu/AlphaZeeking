"""引擎组件包（T-R2）：三引擎统一入口。

用法：
    from applib import engines as E
    eng = E.get_engine("macro"); eng.load(ctx.local_repo); eng.run(...)

get_engine 未知 id → ValueError；注册表缺失/损坏由 load_registry 透明降级（内置默认）。
"""
from __future__ import annotations

from .alpha import AlphaEngine
from .base import (
    MetricSpec,
    export_registry_v2,
    factor_entries,
    find_factor_entry,
    load_registry,
    validate_registry,
)
from .beta import BetaEngine
from .macro import MacroEngine

__all__ = [
    "ENGINES",
    "AlphaEngine",
    "BetaEngine",
    "MacroEngine",
    "MetricSpec",
    "export_registry_v2",
    "factor_entries",
    "find_factor_entry",
    "get_engine",
    "list_engines",
    "load_registry",
    "validate_registry",
]

ENGINES: dict[str, object] = {
    "alpha": AlphaEngine(),
    "beta": BetaEngine(),
    "macro": MacroEngine(),
}


def get_engine(eid: str):
    """按 id 取引擎实例（模块级单例）；未知 id → ValueError。"""
    if eid not in ENGINES:
        raise ValueError(f"未知引擎：{eid}（可选 {sorted(ENGINES)}）")
    return ENGINES[eid]


def list_engines() -> list[dict]:
    """三引擎自描述清单。"""
    return [e.describe() for e in ENGINES.values()]
