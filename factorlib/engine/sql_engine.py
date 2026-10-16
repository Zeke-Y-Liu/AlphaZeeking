"""SQL 引擎（DuckDB，实施计划 §4 任务 1.2：审计最友好的首选引擎）。

数据面契约（M1 冻结 + M2 扩展；schema 与 applib/data.py data_sync 逐列一致——factorlib
独立声明，双实现交叉锚定，applib 侧漂移时本表校验先报错）：

    dwd_daily_bar(secid TEXT, dt DATE, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
                  volume BIGINT, amount DOUBLE, pct DOUBLE)
    dwd_financial_report(secid TEXT, period DATE, announce_date DATE, eps_ytd DOUBLE,
                         bps DOUBLE, roe DOUBLE, roe_w DOUBLE, rev_g DOUBLE, np_g DOUBLE,
                         margin DOUBLE, revenue DOUBLE, net_profit DOUBLE,
                         total_assets DOUBLE, total_liab DOUBLE, ocf DOUBLE, capex DOUBLE,
                         cash DOUBLE, interest_debt DOUBLE)
    dwd_dividend(secid TEXT, ex_date DATE, announce DATE, dps DOUBLE)
    dwd_mktcap_daily(secid TEXT, dt DATE, mktcap DOUBLE)
    dim_security(secid TEXT, name TEXT, ...)

M2 契约修订（登记于 CP-0004）：fin 表新增 8 科目列（尾部追加，旧消费方不受影响）；
新增分红/市值两表。市值表为百度估值抽样粒度（8~13 天一格）→ 因子侧 ASOF 对齐 + staleness。

SQL 约定：
- 模板可用命名参数 $start_dt / $end_dt（可空 DATE；物化层按模式注入）；
- 输出列契约：dt, secid, value, used_period, used_announce
  （used_* 供 PIT 审计；value IS NULL 的行由物化层丢弃）；
- PIT 红线：SQL 内取财务口径必须以 announce_date <= dt 对齐（ASOF JOIN），
  CI 篡改测试验证：换成 period 对齐会被 pit_audit 捕获。
"""
from __future__ import annotations

import duckdb

# 依赖表 → 必需列（执行前校验，缺失/漂移显式报错）
REQUIRED_TABLES = {
    "dwd_daily_bar": {"secid", "dt", "open", "high", "low", "close", "volume", "amount", "pct"},
    "dwd_financial_report": {"secid", "period", "announce_date", "eps_ytd", "bps", "roe",
                             "roe_w", "rev_g", "np_g", "margin",
                             "revenue", "net_profit", "total_assets", "total_liab",
                             "ocf", "capex", "cash", "interest_debt"},
    "dwd_dividend": {"secid", "ex_date", "announce", "dps"},
    "dwd_mktcap_daily": {"secid", "dt", "mktcap"},
    "dim_security": {"secid", "name"},
}

OUTPUT_COLUMNS = ("dt", "secid", "value", "used_period", "used_announce")


class EngineError(ValueError):
    """引擎执行违例（表缺失 / SQL 语义错 / 输出契约不符）。"""


def require_tables(con: duckdb.DuckDBPyConnection, depends: list | None = None) -> None:
    """依赖表存在性 + 列契约校验。

    depends=None → 校验全部 REQUIRED_TABLES（保守）；否则仅校验 meta.depends 声明的子集
    （M2：flt_st 依赖 dim_security、val_ps_ttm 依赖 dwd_mktcap_daily——按需校验，
    不强制无关表在库）。
    """
    for t, cols in REQUIRED_TABLES.items():
        if depends is not None and t not in depends:
            continue
        have = {r[0] for r in con.execute(
            f"SELECT column_name FROM information_schema.columns WHERE table_name = '{t}'").fetchall()}
        if not have:
            raise EngineError(f"依赖表缺失：{t}（先做数据同步/建库）")
        missing = cols - have
        if missing:
            raise EngineError(f"{t} 缺列 {sorted(missing)}（契约漂移，拒绝执行）")


def run_sql(con: duckdb.DuckDBPyConnection, sql: str, start_dt: str | None = None,
            end_dt: str | None = None, depends: list | None = None) -> list[tuple]:
    """执行因子 SQL，返回 (dt, secid, value, used_period, used_announce) 行列表。

    start_dt/end_dt 为 None 时表示不设界（DATE 参数显式 CAST，类型可推断）；
    body 未引用的参数不传（DuckDB 禁止多余参数）。
    depends：meta.depends 声明子集（按需校验列契约；None = 全部表保守校验）。
    输出纪律：dt/used_* 统一 'YYYY-MM-DD' 字符串；value IS NULL 的行不产出
    （缺失即无行——与 UDF continue 语义、物化层 null_dropped 三方一致）。
    """
    require_tables(con, depends)
    params = {}
    if "$start_dt" in sql:
        params["start_dt"] = start_dt
    if "$end_dt" in sql:
        params["end_dt"] = end_dt
    try:
        cur = con.execute(sql, params)
    except duckdb.Error as e:
        raise EngineError(f"SQL 执行失败：{e}") from e
    cols = [d[0] for d in cur.description]
    missing = set(OUTPUT_COLUMNS) - set(cols)
    if missing:
        raise EngineError(f"SQL 输出缺列 {sorted(missing)}（契约：{OUTPUT_COLUMNS}）")
    idx = {c: cols.index(c) for c in OUTPUT_COLUMNS}

    def _norm(v):
        return v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else v

    out = []
    for r in cur.fetchall():
        if r[idx["value"]] is None:
            continue                      # 缺失即无行（P0 纪律：不产出 NULL 值行）
        out.append(tuple(_norm(r[idx[c]]) for c in OUTPUT_COLUMNS))
    return out
