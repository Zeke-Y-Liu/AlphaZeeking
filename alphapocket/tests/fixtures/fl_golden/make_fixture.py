"""golden 合成 fixture 生成器（M1 验收数据，运行一次提交产物）。

设计（构造与期望完全独立于被测代码 —— 期望值由本文件末尾的手工表格推导）：
- 10 只股票 600001.SH..600010.SH，交易日历 = 2021-01-04..2021-06-30 的工作日（合成市场，无节假日）；
- 收盘价阶梯：close_i(d) = (9+i) + 0.5 × 已经过的检查点数 —— 检查点日跳涨 0.5 并保持，
  4 个检查点 close 分别为 9+i / 9.5+i / 10+i / 10.5+i（手工可验）；
- 财报：单季 EPS 恒 0.25（2019Q3..2020Q4），2021Q1 = 0.35（S07 亏损：eps_ytd = -0.90）；
  → TTM：检查点 1/2 = 1.00（Q4'19..Q3'20），检查点 3 = 1.00（Q1'20..Q4'20），
    检查点 4 = 1.10（Q2'20..Q1'21，S06-S10 仍 1.00 —— 年报/Q1 公告晚）；
- PIT 分组：S01-S05 早公告（年报 2021-03-31 / Q1 2021-04-30），S06-S10 晚公告
  （年报 2021-04-29 / Q1 2021-05-28）→ 同一检查点不同股票用不同报告期；
- S07 自 2021-05-28 起 ttm_eps = -0.15 < 0 → PE 缺失（NULL 路径）。

运行：``cd alphapocket && python tests/fixtures/fl_golden/make_fixture.py``
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHECKPOINTS = ["2021-01-29", "2021-02-26", "2021-04-29", "2021-04-30"]
GROUP_EARLY = [f"6000{i:02d}.SH" for i in range(1, 6)]    # S01-S05
GROUP_LATE = [f"6000{i:02d}.SH" for i in range(6, 11)]    # S06-S10
SECIDS = GROUP_EARLY + GROUP_LATE
INDUSTRIES = ["医药生物", "食品饮料", "银行"]


def _weekdays(lo: str, hi: str) -> list[str]:
    d, out = date.fromisoformat(lo), []
    while d <= date.fromisoformat(hi):
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def make_repo() -> dict:
    days = _weekdays("2021-01-04", "2021-06-30")
    cps = set(CHECKPOINTS)

    bars = {}
    for i, secid in enumerate(SECIDS, start=1):
        arr = []
        for d in days:
            close = (9 + i) + 0.5 * sum(1 for cp in CHECKPOINTS if cp <= d)
            arr.append({"dt": d, "open": close, "high": close, "low": close,
                        "close": close, "volume": 10000 + 100 * i,
                        "amount": round(close * (10000 + 100 * i), 2), "pct": 0.0})
        bars[secid] = arr

    # (period, eps_ytd, 早公告日, 晚公告日) —— 早/晚组公告日不同
    spec = [("2019-09-30", 0.75, "2019-10-31", "2019-10-31"),
            ("2019-12-31", 1.00, "2020-04-29", "2020-04-29"),
            ("2020-03-31", 0.25, "2020-04-30", "2020-04-30"),
            ("2020-06-30", 0.50, "2020-08-31", "2020-08-31"),
            ("2020-09-30", 0.75, "2020-10-31", "2020-10-31"),
            ("2020-12-31", 1.00, "2021-03-31", "2021-04-29"),
            ("2021-03-31", 0.35, "2021-04-30", "2021-05-28")]
    fund = {}
    for secid in SECIDS:
        reports = [{"period": p, "announce_date": ann_early if secid in GROUP_EARLY else ann_late,
                    "eps_ytd": eps, "net_profit": eps * 1e8, "revenue": eps * 1e9,
                    "total_assets": 100e8, "total_liab": 50e8, "ocf": eps * 1e7,
                    "capex": eps * 5e6} for p, eps, ann_early, ann_late in spec]
        fund[secid] = {"reports": reports, "dividends": []}
    fund["600007.SH"]["reports"][-1]["eps_ytd"] = -0.90   # S07 2021Q1 亏损（PIT/NULL 路径）

    dims = [{"secid": s, "name": f"标的{s[:6]}", "market": "SH", "asset_type": "stock",
             "industry": INDUSTRIES[i % 3]} for i, s in enumerate(SECIDS)]
    return {"days": days, "bars": bars, "fund": fund, "dims": dims}


# ── 手工期望值（PIT 阶梯逐点核对，勿由被测代码生成）──────────────────────
def make_golden() -> dict:
    ttm = {  # secid → [(cp, ttm_eps, used_period, used_announce)]
        **{s: [("2021-01-29", 1.00, "2020-09-30", "2020-10-31"),
               ("2021-02-26", 1.00, "2020-09-30", "2020-10-31"),
               ("2021-04-29", 1.00, "2020-12-31", "2021-03-31"),
               ("2021-04-30", 1.10, "2021-03-31", "2021-04-30")] for s in GROUP_EARLY},
        **{s: [("2021-01-29", 1.00, "2020-09-30", "2020-10-31"),
               ("2021-02-26", 1.00, "2020-09-30", "2020-10-31"),
               ("2021-04-29", 1.00, "2020-12-31", "2021-04-29"),
               ("2021-04-30", 1.00, "2020-12-31", "2021-04-29")] for s in GROUP_LATE},
    }
    cases = []
    for i, s in enumerate(SECIDS, start=1):
        for cp, t, period, ann in ttm[s]:
            close = (9 + i) + 0.5 * (CHECKPOINTS.index(cp) + 1)
            cases.append({"secid": s, "dt": cp, "close": close, "ttm_eps": t,
                          "expected": close / t, "used_period": period, "used_announce": ann})
    return {"factor_id": "val_pe_ttm", "checkpoints": CHECKPOINTS, "cases": cases}


def main() -> None:
    data = make_repo()
    (HERE / "repo" / "market").mkdir(parents=True, exist_ok=True)
    (HERE / "repo" / "factors").mkdir(parents=True, exist_ok=True)
    (HERE / "repo" / "dim").mkdir(parents=True, exist_ok=True)
    (HERE / "golden").mkdir(parents=True, exist_ok=True)
    (HERE / "repo" / "market" / "daily_bars.json").write_text(
        json.dumps(data["bars"], ensure_ascii=False), encoding="utf-8")
    (HERE / "repo" / "market" / "trade_calendar.json").write_text(
        json.dumps(data["days"], ensure_ascii=False), encoding="utf-8")
    (HERE / "repo" / "factors" / "fundamental_values.json").write_text(
        json.dumps(data["fund"], ensure_ascii=False), encoding="utf-8")
    (HERE / "repo" / "dim" / "security.json").write_text(
        json.dumps(data["dims"], ensure_ascii=False, indent=1), encoding="utf-8")
    (HERE / "golden" / "golden_pe_ttm.json").write_text(
        json.dumps(make_golden(), ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"fixture written: {len(data['days'])} days × {len(SECIDS)} stocks, "
          f"golden cases = {len(make_golden()['cases'])}")


if __name__ == "__main__":
    main()
