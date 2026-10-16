"""PIT 审计（实施计划 §4 任务 1.6 + §11 纪律 1）：未来函数审计，违例即挂。

审计 SQL：factor_value_daily × factor_pit_meta 按 (dt, secid, factor_id) 关联，
``used_announce > dt`` 即未来函数（取数时点早于公告日）。红线：任何已物化因子违例数必须为 0。

覆盖面诚实声明：used_period/used_announce 缺失的行（如 qlib_expr 纯行情因子）不在审计面内，
由 materialize.summary.pit_uncovered 显式登记——审计只对声明了 PIT 元数据的行负责。
"""
from __future__ import annotations

import duckdb

_JOIN = ("FROM factor_value_daily v JOIN factor_pit_meta m"
         " ON v.dt = m.dt AND v.secid = m.secid AND v.factor_id = m.factor_id")


def audit(con: duckdb.DuckDBPyConnection, factor_id: str | None = None,
          limit: int = 10) -> dict:
    """审计已物化因子：返回 {checked, violations, samples}；violations > 0 即 CI 挂起。"""
    params: dict = {}
    cond = "m.used_announce > v.dt"
    if factor_id:
        params["fid"] = factor_id
        cond += " AND m.factor_id = $fid"
    viol_sql = f"SELECT count(*) {_JOIN} WHERE {cond}"
    checked_sql = "SELECT count(*) FROM factor_pit_meta m" + (
        " WHERE m.factor_id = $fid" if factor_id else "")
    sample_sql = (f"SELECT v.dt, v.secid, v.factor_id, v.value, m.used_period, m.used_announce "
                  f"{_JOIN} WHERE {cond} LIMIT {int(limit)}")
    return {
        "checked": con.execute(checked_sql, params).fetchone()[0],
        "violations": con.execute(viol_sql, params).fetchone()[0],
        "samples": [tuple(r) for r in con.execute(sample_sql, params).fetchall()],
    }
