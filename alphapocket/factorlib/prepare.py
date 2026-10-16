"""数据准备（架构文档 §17「数据准备」步）：repo JSON → fl_* 输入表 + TTM 阶梯。

输入契约（与 α repo 同源，测试用合成 fixture 走同一形状）：
- market/daily_bars.json      {secid: [{dt, open, high, low, close, volume, amount, pct}, ...]}
- market/trade_calendar.json  ["YYYY-MM-DD", ...]
- factors/fundamental_values.json {secid: {reports: [{period, announce_date, eps_ytd, net_profit,
                              revenue, total_assets, total_liab, ocf, capex, ...}], dividends: [...]}}
- dim/security.json           [{secid, name, market, asset_type, industry}, ...]
- dim/industry_sw.json        {records: [{code, start_date, industry_code, l1_code, l2_code}, ...]}
                              （M7 T-F5：申万行业 PIT 变迁史；文件缺失 → 空表，合成 fixture 兼容）

产出（fl_* 命名空间，幂等 drop+create+insert）：
- fl_daily_bar / fl_trade_calendar / fl_dim_security / fl_fin_report / fl_industry_sw
- fl_dws_financial_ttm：单季分解 + 累计口径 TTM（SQL 实现，可审计；M7 T-F2 起）
  · 单季 q = 本期累计 − 上季累计（年内首报 Q1 = 累计值）；上季缺失（如缺中报）→ q = NULL
  · TTM(P) = YTD(P) + FY(Y−1) − YTD(同期上年)，12 月报告期直接 TTM = YTD（不依赖上年年报）
    —— 数据完整时与「最近 4 个单季求和」数学等价，且容忍部分报告期缺失
    （如东财现金流量表 Q1/Q3 折旧摊销全缺：累计口径仅 H1/FY 期可算出 ttm_dep，
     其余报告期如实 NULL，由消费端 IS NOT NULL 过滤后回退最近可算报告期）
  · 每报告期一行（不再要求 4 期连续单季）；某列 TTM 依赖项缺失 → 该列 NULL（不 mock 不填 0）
  · announce_date 取该报告期自身的公告日 —— TTM(P) 视为在 announce_date(P) 可用
    （前提：历史 eps/净利累计值不回改；财报重述场景属 M0 数据底座升级项，已在 RUNBOOK 登记）
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb

_FIN_FIELDS = ("eps_ytd", "net_profit", "revenue", "total_assets", "total_liab", "ocf", "capex",
               # M7 T-F1 扩展科目（datasource.A_STMT_COLS 同名；旧行/旧数据无该字段 → None）
               "cost_of_sales", "total_profit", "finance_expense", "cash",
               "short_borrow", "long_borrow", "bonds_payable", "current_mature_debt",
               "ebit", "interest_debt",
               # M7 T-F2：折旧摊销（东财现金流量表补充资料四列求和，scripts/m7_fetch_cashflow_em.py）
               "dep_amort")

_TTM_SQL = """
CREATE OR REPLACE TABLE fl_dws_financial_ttm AS
WITH b AS (
    SELECT secid, period, announce_date,
           CASE WHEN month(period) = 3 THEN eps_ytd
                WHEN date_diff('month', LAG(period) OVER w, period) = 3
                THEN eps_ytd - LAG(eps_ytd) OVER w END AS q_eps,
           CASE WHEN month(period) = 3 THEN net_profit
                WHEN date_diff('month', LAG(period) OVER w, period) = 3
                THEN net_profit - LAG(net_profit) OVER w END AS q_np,
           CASE WHEN month(period) = 3 THEN revenue
                WHEN date_diff('month', LAG(period) OVER w, period) = 3
                THEN revenue - LAG(revenue) OVER w END AS q_rev,
           eps_ytd, net_profit, revenue, ebit, dep_amort,
           cost_of_sales,
           total_assets, total_liab, ocf, capex
    FROM fl_fin_report
    WINDOW w AS (PARTITION BY secid, year(period) ORDER BY period)
), j AS (
    SELECT b.*,
           p.eps_ytd AS eps_py, p.net_profit AS np_py, p.revenue AS rev_py,
           p.ebit AS ebit_py, p.dep_amort AS dep_py,
           p.ocf AS ocf_py, p.cost_of_sales AS cost_py,
           f.eps_ytd AS eps_fy, f.net_profit AS np_fy, f.revenue AS rev_fy,
           f.ebit AS ebit_fy, f.dep_amort AS dep_fy,
           f.ocf AS ocf_fy, f.cost_of_sales AS cost_fy
    FROM b
    LEFT JOIN fl_fin_report p
      ON p.secid = b.secid
     AND year(p.period) = year(b.period) - 1 AND month(p.period) = month(b.period)
    LEFT JOIN fl_fin_report f
      ON f.secid = b.secid
     AND year(f.period) = year(b.period) - 1 AND month(f.period) = 12
)
SELECT secid, period, announce_date, q_eps, q_np, q_rev,
       CASE WHEN month(period) = 12 THEN eps_ytd
            ELSE eps_ytd + eps_fy - eps_py END AS ttm_eps,
       CASE WHEN month(period) = 12 THEN net_profit
            ELSE net_profit + np_fy - np_py END AS ttm_np,
       CASE WHEN month(period) = 12 THEN revenue
            ELSE revenue + rev_fy - rev_py END AS ttm_rev,
       CASE WHEN month(period) = 12 THEN ebit
            ELSE ebit + ebit_fy - ebit_py END AS ttm_ebit,
       CASE WHEN month(period) = 12 THEN dep_amort
            ELSE dep_amort + dep_fy - dep_py END AS ttm_dep,
       CASE WHEN month(period) = 12 THEN ocf
            ELSE ocf + ocf_fy - ocf_py END AS ttm_ocf,
       CASE WHEN month(period) = 12 THEN cost_of_sales
            ELSE cost_of_sales + cost_fy - cost_py END AS ttm_cost,
       total_assets, total_liab, ocf, capex
FROM j
"""


def _read_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def prepare(repo: Path, db_path: Path) -> dict:
    """repo JSON → fl_* 输入表（幂等）。返回各表行数摘要。"""
    bars = _read_json(repo / "market" / "daily_bars.json")
    cal = _read_json(repo / "market" / "trade_calendar.json")
    fund = _read_json(repo / "factors" / "fundamental_values.json")
    dims = _read_json(repo / "dim" / "security.json")
    # M7 T-F2：市值周频快照（dim/market_cap.json {secid: [[date, 亿], ...]}）；缺文件 → 空（合成 fixture）
    mcap = _read_json(repo / "dim" / "market_cap.json") if (repo / "dim" / "market_cap.json").exists() else {}

    con = duckdb.connect(str(db_path))
    try:
        for t in ("fl_daily_bar", "fl_trade_calendar", "fl_dim_security", "fl_fin_report",
                  "fl_dws_financial_ttm", "fl_dividend", "fl_market_cap", "fl_industry_sw"):
            con.execute(f"DROP TABLE IF EXISTS {t}")
        con.execute("""CREATE TABLE fl_daily_bar (secid TEXT, dt DATE, open DOUBLE, high DOUBLE,
                       low DOUBLE, close DOUBLE, volume BIGINT, amount DOUBLE, pct DOUBLE)""")
        con.execute("CREATE TABLE fl_trade_calendar (dt DATE)")
        con.execute("""CREATE TABLE fl_dim_security (secid TEXT, name TEXT, market TEXT,
                       asset_type TEXT, industry TEXT)""")
        con.execute("""CREATE TABLE fl_fin_report (secid TEXT, period DATE, announce_date DATE,
                       eps_ytd DOUBLE, net_profit DOUBLE, revenue DOUBLE,
                       total_assets DOUBLE, total_liab DOUBLE, ocf DOUBLE, capex DOUBLE,
                       cost_of_sales DOUBLE, total_profit DOUBLE, finance_expense DOUBLE,
                       cash DOUBLE, short_borrow DOUBLE, long_borrow DOUBLE,
                       bonds_payable DOUBLE, current_mature_debt DOUBLE,
                       ebit DOUBLE, interest_debt DOUBLE, dep_amort DOUBLE)""")
        # M7 T-F1：分红历史表（val_div_yield / div_payout_streak 数据源）。
        # PIT 口径：announce_date 可空（上游缺公告日如实 NULL，消费方计数披露，不编造）
        con.execute("""CREATE TABLE fl_dividend (secid TEXT, ex_date DATE,
                       announce_date DATE, dps DOUBLE)""")
        # M7 T-F2：市值周频快照（亿元原值；消费端 ×1e8 与财报科目（元）对齐）。
        # 周频点对月度截面滞后 ≤7 天：估值因子用「≤dt 最新市值点」，透明口径
        con.execute("CREATE TABLE fl_market_cap (secid TEXT, dt DATE, mktcap_yi DOUBLE)")
        # M7 T-F5：申万行业 PIT 变迁史（mom_industry_adj 的 industry-asof 数据源）。
        # l1_code 即分组键（申万官方只发布代码不发布中文名，note 字段已声明）；
        # code→secid 按 dim security 的 {code}.SH/.SZ 匹配（ETF/未覆盖标的自然缺席）
        con.execute("CREATE TABLE fl_industry_sw (secid TEXT, start_date DATE, l1_code TEXT)")

        bar_rows = [(s, r["dt"], r.get("open"), r.get("high"), r.get("low"), r.get("close"),
                     r.get("volume"), r.get("amount"), r.get("pct"))
                    for s, arr in bars.items() for r in arr]
        con.executemany("INSERT INTO fl_daily_bar VALUES (?,?,?,?,?,?,?,?,?)", bar_rows)
        con.executemany("INSERT INTO fl_trade_calendar VALUES (?)", [(d,) for d in cal])
        # M7 T-F1：universe_m7.json 行业并入 dim（board 作为 industry；ETF/既有 dim 条目不动）
        um7 = _read_json(repo / "dim" / "universe_m7.json") if (repo / "dim" / "universe_m7.json").exists() else {}
        board_map = {r["secid"]: r["board"] for r in um7.get("picked") or []}
        dims = [dict(d, industry=d.get("industry") or board_map.get(d["secid"])) for d in dims]
        con.executemany("INSERT INTO fl_dim_security VALUES (?,?,?,?,?)",
                        [(d["secid"], d["name"], d["market"], d["asset_type"], d.get("industry"))
                         for d in dims])
        fin_rows = []
        dvd_rows = []
        for s, v in fund.items():
            for r in v.get("reports", []):
                fin_rows.append((s, r["period"], r["announce_date"],
                                 *[r.get(f) for f in _FIN_FIELDS]))
            for r in v.get("dividends") or []:
                if not r.get("ex_date"):
                    continue
                dvd_rows.append((s, r["ex_date"], r.get("announce"), r.get("dps")))
        con.executemany("INSERT INTO fl_fin_report VALUES (%s)" % ",".join(["?"] * (3 + len(_FIN_FIELDS))),
                        fin_rows)
        if dvd_rows:                            # 合成 fixture 可无分红 → 空列表跳过（duckdb 不收空 executemany）
            con.executemany("INSERT INTO fl_dividend VALUES (?,?,?,?)", dvd_rows)
        mc_rows = [(s, dt_v, mc_v) for s, arr in mcap.items() for dt_v, mc_v in arr if mc_v is not None]
        if mc_rows:
            con.executemany("INSERT INTO fl_market_cap VALUES (?,?,?)", mc_rows)
        # 申万行业 PIT：code → secid（dim security 匹配 .SH/.SZ；未匹配丢弃并计数）
        ind_fp = repo / "dim" / "industry_sw.json"
        ind_rows = []
        if ind_fp.exists():
            secids = {d["secid"] for d in dims}
            for r in _read_json(ind_fp).get("records") or []:
                for suf in (".SH", ".SZ"):
                    if f"{r['code']}{suf}" in secids:
                        ind_rows.append((f"{r['code']}{suf}", r["start_date"], r["l1_code"]))
                        break
        if ind_rows:
            con.executemany("INSERT INTO fl_industry_sw VALUES (?,?,?)", ind_rows)
        con.execute(_TTM_SQL)
        n_ttm = con.execute("SELECT count(*) FROM fl_dws_financial_ttm").fetchone()[0]
        return {"fl_daily_bar": len(bar_rows), "fl_trade_calendar": len(cal),
                "fl_dim_security": len(dims), "fl_fin_report": len(fin_rows),
                "fl_industry_sw": len(ind_rows),
                "fl_dividend": len(dvd_rows), "fl_market_cap": len(mc_rows),
                "fl_dws_financial_ttm": n_ttm}
    finally:
        con.close()
