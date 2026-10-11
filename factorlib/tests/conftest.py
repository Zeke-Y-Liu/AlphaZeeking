"""factorlib 测试夹具：合成小库（PIT 语义可控）+ 真实 repo 库（黄金样例）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb
import pytest

FACTORLIB_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = FACTORLIB_ROOT.parent          # factorlib 包的父目录（import factorlib 所需）
for _p in (WORKSPACE_ROOT, FACTORLIB_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

ALPHAPOCKET_REPO = Path("/workspace/alphapocket/remote/repo")

# ── 合成小库：3 标的 × 5 交易日，PIT 语义全部可手工推演 ─────────
# S1：2021-01-06 同日公告 FY2020(2.0) 与 2021Q1(0.6) → 取最新 period(2021Q1)，
#     TTM = 0.6 − eps(2020Q1)=0.5 + eps(2020FY)=2.0 = 2.1（01-06..01-08）
# S2：01-06 公告 FY2020(4.0)；04-05 公告 2021Q1(1.0) → TTM = 1.0 − 0.8 + 4.0 = 4.2
# S3：01-06 公告 FY2020(-1.0) → 负 EPS 置空 → 全程无因子行
DTs = ["2021-01-04", "2021-01-05", "2021-01-06", "2021-01-07", "2021-01-08"]
CLOSES = {"S1": [10.0, 11.0, 12.0, 13.0, 14.0],
          "S2": [20.0, 21.0, 22.0, 23.0, 24.0],
          "S3": [30.0, 31.0, 32.0, 33.0, 34.0]}
REPORTS = {
    # S1：2019FY/2020FY/2021Q1 均公告于 01-06（同日多公告取最新 period）；
    #     2020Q1 公告 2020-04-30 但缺 2019-03-31 同期 → 01-04/05 链不完整无行；
    #     01-06 起 TTM = 0.6 − eps(2020Q1)=0.5 + eps(2020FY)=2.0 = 2.1
    "S1": [("2019-12-31", "2021-01-06", 1.8), ("2020-03-31", "2020-04-30", 0.5),
           ("2020-06-30", "2020-08-31", 1.2), ("2020-12-31", "2021-01-06", 2.0),
           ("2021-03-31", "2021-01-06", 0.6)],
    "S2": [("2020-03-31", "2020-04-30", 0.8), ("2020-12-31", "2021-01-06", 4.0),
           ("2021-03-31", "2021-04-05", 1.0)],
    "S3": [("2020-12-31", "2021-01-06", -1.0)],
}

DDL_BARS = """CREATE TABLE dwd_daily_bar (secid TEXT, dt DATE, open DOUBLE, high DOUBLE,
               low DOUBLE, close DOUBLE, volume BIGINT, amount DOUBLE, pct DOUBLE)"""
DDL_FUND = """CREATE TABLE dwd_financial_report (secid TEXT, period DATE, announce_date DATE,
               eps_ytd DOUBLE, bps DOUBLE, roe DOUBLE, roe_w DOUBLE, rev_g DOUBLE,
               np_g DOUBLE, margin DOUBLE)"""


def make_con(ddl_bars=DDL_BARS, ddl_fund=DDL_FUND) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute(ddl_bars)
    con.execute(ddl_fund)
    for s, closes in CLOSES.items():
        con.executemany("INSERT INTO dwd_daily_bar VALUES (?,?,?,?,?,?,?,?,?)",
                        [(s, dt, c, c, c, c, 1000, 1000 * c, 0.0)
                         for dt, c in zip(DTs, closes)])
    con.executemany("INSERT INTO dwd_financial_report VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [(s, p, a, eps, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0)
                     for s, reps in REPORTS.items() for p, a, eps in reps])
    return con


def make_meta(**kw):
    """合成 FactorMeta（绕开 YAML 文件，供引擎级单测）。"""
    from factorlib.registry import FactorMeta
    base = dict(id="val_pe_ttm", name="市盈率TTM", family="valuation", type="alpha",
                status="draft", version="1.0.0", universe=["CN"], engine="sql",
                lookback_days=0, depends=["dwd_daily_bar", "dwd_financial_report"],
                direction=-1, hypothesis_id=None, transform=[], body="")
    base.update(kw)
    return FactorMeta(**base)


VAL_PE_TTM_SQL = (FACTORLIB_ROOT / "definitions/valuation/val_pe_ttm.yaml").read_text(encoding="utf-8")


def sql_from_yaml() -> str:
    import yaml
    return yaml.safe_load(VAL_PE_TTM_SQL)["sql"]


# ── 真实 repo 库（黄金样例；独立建库 = 数据契约双实现交叉锚定） ──
@pytest.fixture(scope="session")
def repo_con():
    if not (ALPHAPOCKET_REPO / "market/daily_bars.json").exists():
        pytest.skip("alphapocket remote repo 不存在（黄金样例跳过）")
    con = duckdb.connect(":memory:")
    con.execute(DDL_BARS)
    con.execute(DDL_FUND)
    bars = json.loads((ALPHAPOCKET_REPO / "market/daily_bars.json").read_text())
    fund = json.loads((ALPHAPOCKET_REPO / "factors/fundamental_values.json").read_text())
    con.executemany("INSERT INTO dwd_daily_bar VALUES (?,?,?,?,?,?,?,?,?)",
                    [(s, r["dt"], r["open"], r["high"], r["low"], r["close"],
                      r["volume"], r["amount"], r["pct"])
                     for s, arr in bars.items() for r in arr])
    con.executemany("INSERT INTO dwd_financial_report VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [(s, r["period"], r["announce_date"], r["eps_ytd"], r["bps"], r["roe"],
                      r["roe_w"], r["rev_g"], r["np_g"], r["margin"])
                     for s, v in fund.items() for r in v["reports"]])
    return con


@pytest.fixture()
def con():
    return make_con()
