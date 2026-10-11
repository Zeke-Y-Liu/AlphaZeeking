"""engines/ 包装层（组件化重构 T-R1 骨架 · 契约冻结）。

定位：三引擎（A 微观因子 / B 信号层 / C 宏观流动性）的统一包装层——
公式不搬家（factors.compute_x 纯函数原位），包装层只做契约：
入参 schema 校验 → 委托计算 → 输出位范围约束（越界告警不截断）→ provenance 登记。

契约五条与架构决策记录：见 base.py 模块注释与
《任务拆分-组件化重构/T-R1-现状盘点与架构决策.md》（入库可追溯；修订须登记）。
"""
from .base import EngineSpec, EngineWrapper, check_output_range, validate_input

__all__ = ["EngineSpec", "EngineWrapper", "check_output_range", "validate_input"]
