"""factorlib —— 因子仓库单仓（M1：注册中心 + 三引擎 + 物化 + PIT 审计）。

上游规格：《中长期因子挖掘与回测-实施计划（引擎 A）》§4（M1）；
架构契约：《数据层与因子库架构设计》§14–§18（FactorMeta / 状态机 / PIT 红线）。

模块边界（M1 冻结）：
- registry      因子定义 YAML → FactorMeta → 状态机（draft→active→deprecated），按 id 排序
- engine/       三引擎执行器：sql（DuckDB，审计最友好）/ python_udf（纯函数约束）/ qlib_expr（子集解释器）
- preprocess    截面预处理链：winsorize → 中性化（可选）→ zscore/rank
- materialize   三模式物化（full / incremental / snapshot）→ factor_value_daily 长表 + factor_pit_meta
- pit_audit     未来函数审计：used_announce <= dt 违例必须为 0（CI 红线）

贯穿纪律（实施计划 §11）：
1. PIT 红线：任何取数强制 announce_date 对齐（as_of 快照亦然），违例即挂；
2. 可复现：两次全量重算逐行 diff 必须为 0；
3. 单仓解耦：本包不 import applib，仅依赖 duckdb/pyyaml/pandas/numpy，
   数据面契约 = DuckDB 表 dwd_daily_bar / dwd_financial_report（schema 在 engine.sql_engine 冻结）。
"""
from .registry import FactorMeta, RegistryError, load_definitions, set_status, resolve_start
from .materialize import materialize, ensure_tables, verify_reproducible
from .pit_audit import audit as pit_audit

__all__ = [
    "FactorMeta", "RegistryError", "load_definitions", "set_status", "resolve_start",
    "materialize", "ensure_tables", "verify_reproducible", "pit_audit",
]
__version__ = "0.1.0"
