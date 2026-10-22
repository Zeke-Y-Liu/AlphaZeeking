"""data_sync 空数据集防御（M11 回归）：纯港/美股 universe 下财报/分红为空，
executemany 空列表不再抛 Invalid Input Error。

背景：2026-10-23 sync-secid 对 PACS.US（美股，无 A 股财报）refresh 后 DuckDB 物化
在 ``frep=[]`` 处炸 ``executemany requires a non-empty list`` → job failed 88%。
空是合法状态（多市场 universe），应跳过插入、表保持 0 行。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_data_sync_empty_fundamental_ok(tmp_path):
    from applib import data as A
    from applib.ctx import Ctx

    ctx = Ctx(root=tmp_path)
    repo = ctx.local_repo
    ctx.ensure_sandbox()
    inputs = {
        "market/daily_bars.json": {"PACS.US": [{"dt": "2026-10-22", "open": 1, "high": 1,
                                                "low": 1, "close": 1, "volume": 10,
                                                "amount": 10, "pct": 0.1}]},
        "market/daily_bars_raw.json": {"PACS.US": [{"dt": "2026-10-22", "close": 1}]},
        "market/cross_check_tx.json": {"PACS.US": [{"dt": "2026-10-22", "close": 1}]},
        "market/trade_calendar.json": ["2026-10-22"],
        "dim/security.json": [{"secid": "PACS.US", "name": "PACS", "market": "US",
                               "asset_type": "STOCK", "industry": None}],
        "factors/fundamental_values.json": {},  # 美股无财报/分红 → frep/fdiv 全空
    }
    for rel, payload in inputs.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    out = A.data_sync({}, ctx)  # 修复前在此抛 duckdb.InvalidInputException

    import duckdb

    con = duckdb.connect(str(ctx.db_path), read_only=True)
    try:
        assert con.execute("select count(*) from dwd_daily_bar").fetchone()[0] == 1
        assert con.execute("select count(*) from dwd_financial_report").fetchone()[0] == 0
        assert con.execute("select count(*) from dwd_dividend").fetchone()[0] == 0
    finally:
        con.close()
    assert out["synced"]["dwd_daily_bar"] == 1
    assert out["synced"]["dwd_financial_report"] == 0
    assert out["synced"]["dwd_dividend"] == 0


def test_data_sync_empty_dividend_only(tmp_path):
    """财报非空但分红空（如无分红标的）→ dwd_dividend 0 行，同样不炸。"""
    from applib import data as A
    from applib.ctx import Ctx

    ctx = Ctx(root=tmp_path)
    repo = ctx.local_repo
    ctx.ensure_sandbox()
    report = {"period": "2026-06-30", "announce_date": "2026-08-30", "eps_ytd": 1.0,
              "bps": 2.0, "roe": 1.0, "roe_w": 1.0, "rev_g": 0.1, "np_g": 0.1,
              "margin": 0.1, "revenue": 1, "net_profit": 1, "total_assets": 1,
              "total_liab": 1, "ocf": 1, "capex": 1, "cash": 1, "interest_debt": 1}
    inputs = {
        "market/daily_bars.json": {"600941.SH": [{"dt": "2026-10-22", "open": 1, "high": 1,
                                                  "low": 1, "close": 1, "volume": 10,
                                                  "amount": 10, "pct": 0.1}]},
        "market/daily_bars_raw.json": {"600941.SH": [{"dt": "2026-10-22", "close": 1}]},
        "market/cross_check_tx.json": {},
        "market/trade_calendar.json": ["2026-10-22"],
        "dim/security.json": [{"secid": "600941.SH", "name": "测试", "market": "A",
                               "asset_type": "STOCK", "industry": None}],
        "factors/fundamental_values.json": {"600941.SH": {"reports": [report],
                                                           "dividends": []}},
    }
    for rel, payload in inputs.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    out = A.data_sync({}, ctx)  # 修复前 dividends=[] 在 frep 之后同样炸

    import duckdb

    con = duckdb.connect(str(ctx.db_path), read_only=True)
    try:
        assert con.execute("select count(*) from dwd_financial_report").fetchone()[0] == 1
        assert con.execute("select count(*) from dwd_dividend").fetchone()[0] == 0
        assert con.execute("select count(*) from cross_check_tx").fetchone()[0] == 0
    finally:
        con.close()
    assert out["synced"]["dwd_dividend"] == 0
