"""engines/ 包装层（组件化重构 T-R1 骨架 · 契约冻结）。

定位：三引擎（A 微观因子 / B 信号层 / C 宏观流动性）的统一包装层——
公式不搬家（factors.compute_x 纯函数原位），包装层只做契约：
入参 schema 校验 → 委托计算 → 输出位范围约束（越界告警不截断）→ provenance 登记。

契约五条与架构决策记录：见 base.py 模块注释与
《任务拆分-组件化重构/T-R1-现状盘点与架构决策.md》（入库可追溯；修订须登记）。
"""
from .a_factors import ALPHA_SPEC, alpha_engine
from .b_signals import BETA_SPEC, beta_engine
from .base import (ENGINE_REGISTRY_VERSION, EngineSpec, EngineWrapper,
                   check_output_range, validate_input)
from .c_macro import MACRO_SPEC, macro_engine
from .config import engine_config_for, load_engine_config

__all__ = [
    "EngineSpec", "EngineWrapper", "check_output_range", "validate_input",
    "ENGINE_REGISTRY_VERSION", "load_engine_config", "engine_config_for",
    "ALPHA_SPEC", "alpha_engine", "BETA_SPEC", "beta_engine", "MACRO_SPEC", "macro_engine",
]
