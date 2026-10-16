"""SQL 引擎（架构文档 §16「SQL / DuckDB」）：定义 SQL 直接在 DuckDB 执行。

约定（val_pe_ttm.yaml 为模板）：
- 定义 SQL 必须产出列：dt, secid, value, used_period, used_announce；
- $start / $end 为 DuckDB 命名参数（物化三模式由参数区间表达）；
- PIT 红线写死在 SQL 内：财报侧 join 条件必须是 announce_date <= d.dt。
  篡改测试（M1 DoD）即把该条件改为 report_period 后验证 PIT 审计能捕获。
"""
from __future__ import annotations

import duckdb

from factorlib.engine.base import EngineError, normalize_rows


class SqlEngine:
    def __init__(self, meta):
        self.meta = meta
        if not (meta.sql or "").strip():
            raise EngineError(f"{meta.id}: engine=sql 但定义缺少 sql")

    def run(self, db_path, start, end) -> list[dict]:
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            cur = con.execute(self.meta.sql, {"start": start, "end": end})
            cols = [d[0] for d in cur.description]
            missing = {"dt", "secid", "value"} - set(cols)
            if missing:
                raise EngineError(f"{self.meta.id}: SQL 输出缺少列 {missing}（实际 {cols}）")
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            con.close()
        return normalize_rows(rows)
