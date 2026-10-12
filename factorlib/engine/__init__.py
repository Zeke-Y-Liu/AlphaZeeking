"""三引擎注册表与分发（实施计划 §4 任务 1.2）。

engine 字段 → 执行器：
    sql        → sql_engine.run_sql      （DuckDB，审计最友好，val_pe_ttm 首选）
    python_udf → udf_engine.run_udf      （纯函数约束：禁联网/随机数/IO，AST 守卫）
    qlib_expr  → qlib_expr.run_expr      （Alpha158/360 表达式口径时序子集）

统一输出契约：(dt, secid, value, used_period, used_announce)。
"""
from __future__ import annotations

import duckdb

from .sql_engine import run_sql, require_tables, REQUIRED_TABLES, OUTPUT_COLUMNS, EngineError
from . import udf_engine, qlib_expr

__all__ = ["ENGINES", "run_factor", "require_tables", "REQUIRED_TABLES", "OUTPUT_COLUMNS",
           "EngineError", "run_sql", "udf_engine", "qlib_expr"]

ENGINES = {"sql": run_sql, "python_udf": udf_engine.run_udf, "qlib_expr": qlib_expr.run_expr}


def run_factor(con: duckdb.DuckDBPyConnection, meta, start_dt: str | None = None,
               end_dt: str | None = None) -> list[tuple]:
    """按 FactorMeta.engine 分发执行；factor_id 由物化层补齐（引擎只算值）。"""
    fn = ENGINES.get(meta.engine)
    if fn is None:
        raise EngineError(f"未注册引擎：{meta.engine}")
    return fn(con, meta.body, start_dt=start_dt, end_dt=end_dt,
              depends=list(meta.depends))
