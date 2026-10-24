"""数据面：data.sync（远端→DuckDB 缓存）与 data.query（受限 SQL）

V4-α 真实化：数据集来自 AKShare（经 datasource.fetch 落 remote/repo），
同步层幂等（drop+create+insert），全部表可由 repo JSON 重建。
"""
from __future__ import annotations

import json

import duckdb


def _load_json(p):
    return json.loads(p.read_text(encoding="utf-8"))


def data_sync(args: dict, ctx) -> dict:
    """远端 repo JSON → sandbox/data/ap.duckdb（α 本地替身；生产走 Pre-Signed URL 直拉 Parquet）"""
    ctx.ensure_sandbox()
    bars = _load_json(ctx.local_repo / "market" / "daily_bars.json")
    raws = _load_json(ctx.local_repo / "market" / "daily_bars_raw.json")
    txs = _load_json(ctx.local_repo / "market" / "cross_check_tx.json")
    cal = _load_json(ctx.local_repo / "market" / "trade_calendar.json")
    dims = _load_json(ctx.local_repo / "dim" / "security.json")
    fund = _load_json(ctx.local_repo / "factors" / "fundamental_values.json")

    con = duckdb.connect(str(ctx.db_path))
    for t in ("dwd_daily_bar", "dwd_daily_bar_raw", "cross_check_tx", "dwd_trade_calendar",
              "dwd_trade_calendar_hk", "dwd_trade_calendar_us",
              "dwd_financial_report", "dwd_dividend", "dwd_mktcap_daily", "dim_security",
              "factor_value_daily", "factor_pit_meta"):
        con.execute(f"DROP TABLE IF EXISTS {t}")

    def _insert_many(sql, rows):
        # executemany 空列表防御：DuckDB 对空参数抛 Invalid Input Error，而空是
        # 合法状态（如纯港/美股 universe 无 A 股财报与分红）→ 跳过，表保持空。
        if rows:
            con.executemany(sql, rows)
    con.execute("""CREATE TABLE dwd_daily_bar (secid TEXT, dt DATE, open DOUBLE, high DOUBLE,
                   low DOUBLE, close DOUBLE, volume BIGINT, amount DOUBLE, pct DOUBLE)""")
    con.execute("CREATE TABLE dwd_daily_bar_raw (secid TEXT, dt DATE, close DOUBLE)")
    con.execute("CREATE TABLE cross_check_tx (secid TEXT, dt DATE, close DOUBLE)")
    con.execute("CREATE TABLE dwd_trade_calendar (dt DATE)")
    con.execute("""CREATE TABLE dwd_financial_report (secid TEXT, period DATE, announce_date DATE,
                   eps_ytd DOUBLE, bps DOUBLE, roe DOUBLE, roe_w DOUBLE,
                   rev_g DOUBLE, np_g DOUBLE, margin DOUBLE,
                   revenue DOUBLE, net_profit DOUBLE, total_assets DOUBLE, total_liab DOUBLE,
                   ocf DOUBLE, capex DOUBLE, cash DOUBLE, interest_debt DOUBLE)""")
    con.execute("CREATE TABLE dwd_dividend (secid TEXT, ex_date DATE, announce DATE, dps DOUBLE)")
    con.execute("CREATE TABLE dwd_mktcap_daily (secid TEXT, dt DATE, mktcap DOUBLE)")
    con.execute("CREATE TABLE dim_security (secid TEXT, name TEXT, market TEXT, asset_type TEXT, industry TEXT)")
    con.execute("CREATE TABLE factor_value_daily (dt DATE, secid TEXT, factor_id TEXT, value DOUBLE)")
    con.execute("""CREATE TABLE factor_pit_meta (dt DATE, secid TEXT, factor_id TEXT,
                   used_period DATE, used_announce DATE)""")

    _insert_many("INSERT INTO dwd_daily_bar VALUES (?,?,?,?,?,?,?,?,?)",
                 [(s, r["dt"], r["open"], r["high"], r["low"], r["close"], r["volume"], r["amount"], r["pct"])
                  for s, arr in bars.items() for r in arr])
    _insert_many("INSERT INTO dwd_daily_bar_raw VALUES (?,?,?)",
                 [(s, r["dt"], r["close"]) for s, arr in raws.items() for r in arr])
    _insert_many("INSERT INTO cross_check_tx VALUES (?,?,?)",
                 [(s, r["dt"], r["close"]) for s, arr in txs.items() for r in arr])
    _insert_many("INSERT INTO dwd_trade_calendar VALUES (?)", [(d,) for d in cal])
    # V4-β 多市场：港股/美股日历（由 K 线日期并集推导；文件缺失时跳过，保持向后兼容）
    synced_extra = {}
    for mkt in ("hk", "us"):
        p = ctx.local_repo / "market" / f"trade_calendar_{mkt}.json"
        if not p.exists():
            continue
        try:
            mcal = _load_json(p)
        except (OSError, ValueError):
            continue
        t = f"dwd_trade_calendar_{mkt}"
        con.execute(f"DROP TABLE IF EXISTS {t}")
        con.execute(f"CREATE TABLE {t} (dt DATE)")
        _insert_many(f"INSERT INTO {t} VALUES (?)", [(d,) for d in mcal])
        synced_extra[t] = len(mcal)
    _insert_many("INSERT INTO dim_security VALUES (?,?,?,?,?)",
                 [(d["secid"], d["name"], d["market"], d["asset_type"], d["industry"]) for d in dims])
    # M2：总市值日度（dim/market_cap.json，百度估值抽样粒度 8~13 天一格 → 因子侧 asof 对齐）
    mcap_path = ctx.local_repo / "dim" / "market_cap.json"
    n_mcap = 0
    if mcap_path.exists():
        mcap = _load_json(mcap_path)
        mrows = [(s, dt_, float(mv)) for s, arr in mcap.items() for dt_, mv in arr]
        _insert_many("INSERT INTO dwd_mktcap_daily VALUES (?,?,?)", mrows)
        n_mcap = len(mrows)
    frep, fdiv = [], []
    for s, v in fund.items():
        for r in v["reports"]:
            frep.append((s, r["period"], r["announce_date"], r["eps_ytd"], r["bps"], r["roe"],
                         r["roe_w"], r["rev_g"], r["np_g"], r["margin"],
                         r.get("revenue"), r.get("net_profit"), r.get("total_assets"),
                         r.get("total_liab"), r.get("ocf"), r.get("capex"),
                         r.get("cash"), r.get("interest_debt")))
        for d in v["dividends"]:
            fdiv.append((s, d["ex_date"], d["announce"], d["dps"]))
    _insert_many("INSERT INTO dwd_financial_report VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", frep)
    _insert_many("INSERT INTO dwd_dividend VALUES (?,?,?,?)", fdiv)
    con.close()
    as_of = max(r["dt"] for arr in bars.values() for r in arr)
    return {"synced": {"dwd_daily_bar": sum(len(a) for a in bars.values()),
                       "dwd_daily_bar_raw": sum(len(a) for a in raws.values()),
                       "dwd_financial_report": len(frep), "dwd_dividend": len(fdiv),
                       "dwd_mktcap_daily": n_mcap,
                       "dwd_trade_calendar": len(cal), "dim_security": len(dims), **synced_extra},
            "as_of": as_of, "source": "AKShare(新浪/腾讯) → remote/repo → duckdb",
            "provenance": str(ctx.local_repo / "market" / "provenance.json")}


def data_query(args: dict, ctx) -> dict:
    """受限点查：只读 SELECT + 强制 LIMIT（防 sandbox 内误操作）"""
    sql = str(args.get("sql", "")).strip()
    if not sql.lower().lstrip("(").startswith("select"):
        return {"error": "仅允许 SELECT"}
    if "limit" not in sql.lower():
        sql += " LIMIT 200"
    con = duckdb.connect(str(ctx.db_path), read_only=True)
    try:
        res = con.execute(sql)
        cols = [d[0] for d in res.description]
        return {"columns": cols, "rows": [list(r) for r in res.fetchall()]}
    finally:
        con.close()
