"""因子计算引擎（架构文档 §16/§17）：三引擎执行器路由。

- sql：DuckDB SQL（$start/$end 命名参数绑定；PIT 条件写在定义 SQL 内，审计最友好）
- python_udf：单标的纯函数（AST 守卫禁联网/随机数/IO）
- qlib_expr：极简 Qlib 表达式 → DuckDB SQL 翻译器（Ref/Mean/Std/Max/Min/Sum/Rank + 四则运算；
  Alpha158/360 全集对接属 M7，本子集覆盖 F1 常用趋势/波动表达）

统一输出：DataFrame 语义的行序列 [{dt, secid, value, used_period, used_announce}, ...]；
sql/qlib_expr 引擎由 SQL 自带 used_period/used_announce 列（PIT 元数据同源），python_udf
引擎由 UDF 按 secid 返回 {dt: (value, used_period, used_announce)} 全截面映射。
三引擎统一签名 run(db_path, start, end)，由 materialize 无差别调度。
"""
from factorlib.engine.base import EngineError, Row, normalize_rows
from factorlib.engine.sql_engine import SqlEngine
from factorlib.engine.python_udf import UdfEngine
from factorlib.engine.qlib_expr import QlibExprEngine

_ENGINES = {"sql": SqlEngine, "python_udf": UdfEngine, "qlib_expr": QlibExprEngine}


def get_engine(meta):
    """按 FactorMeta.engine 路由执行器。"""
    cls = _ENGINES.get(meta.engine)
    if cls is None:
        raise EngineError(f"未知引擎：{meta.engine!r}")
    return cls(meta)


__all__ = ["get_engine", "EngineError", "Row", "normalize_rows",
           "SqlEngine", "UdfEngine", "QlibExprEngine"]
