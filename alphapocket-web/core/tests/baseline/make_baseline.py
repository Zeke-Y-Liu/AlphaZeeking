"""核心基准数据生成器（RG-2）：确定性公式生成，无随机种子 —— 同代码必同产物。

产物（均冻结提交，任何漂移都由 ``test_core_baseline.py::test_baseline_fixture_frozen``
咬住）::

    ws/market/daily_bars.json       合成三标的 300 根日 K（600901.SH / 600902.SH / 511900.SH）
    ws/market/daily_bars_raw.json   raw 面与 qfq 一致（A 股合成无除权）
    ws/market/cross_check_tx.json   交叉源与主序列一致（双源对账全绿）
    ws/market/trade_calendar.json   bars 日期并集（跳过周末）
    expected/anchor_backtest.json   等权组合回测关键指标锚点（首跑生成后人工审阅冻结）

维护：改动生成公式后重跑本脚本并人工审阅新锚点，再提交。
"""
from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent
N = 300


def _dates(n: int) -> list[str]:
    """2025-01-06（周一）起的 n 个工作日（跳过周六日；无假日表，合成数据不要求）。"""
    out, d = [], date(2025, 1, 6)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _series(base: float, drift: float, wave: float, period: float) -> list[float]:
    return [base * (1 + drift) ** i + wave * math.sin(i / period) for i in range(N)]


def _bars(closes: list[float], dts: list[str]) -> list[dict]:
    rows, prev = [], None
    for i, (dt, close) in enumerate(zip(dts, closes, strict=True)):
        open_ = prev if prev is not None else round(close * 0.99, 4)
        hi = round(max(open_, close) * 1.01, 4)
        lo = round(min(open_, close) * 0.99, 4)
        vol = int(1_000_000 + 100_000 * math.sin(i / 11) + 500 * i)
        rows.append({"dt": dt, "open": round(open_, 4), "high": hi, "low": lo,
                     "close": round(close, 4), "volume": vol,
                     "amount": round(close * vol, 2),
                     "pct": round(close / prev - 1, 6) if prev else None})
        prev = round(close, 4)
    return rows


def build_bars() -> dict[str, list[dict]]:
    dts = _dates(N)
    return {
        # 甲：平稳上行小波动（趋势型）
        "600901.SH": _bars(_series(100.0, 0.0009, 1.5, 7.0), dts),
        # 乙：下行大波动（回撤型）
        "600902.SH": _bars(_series(200.0, -0.0015, 6.0, 5.0), dts),
        # ETF：近水平微波动（低波型）
        "511900.SH": _bars(_series(100.0, 0.0001, 0.15, 13.0), dts),
    }


PF = {
    "id": "rg_baseline",
    "name": "基准 · 甲乙+ETF 等权",
    "base_ccy": "CNY",
    "positions": [
        {"secid": "600901.SH", "name": "基准甲", "market": "A", "weight": 33.34},
        {"secid": "600902.SH", "name": "基准乙", "market": "A", "weight": 33.33},
        {"secid": "511900.SH", "name": "基准ETF", "market": "A", "weight": 33.33},
    ],
}


def main() -> None:
    bars = build_bars()
    ws = BASE / "ws"
    (ws / "market").mkdir(parents=True, exist_ok=True)
    (ws / "market" / "daily_bars.json").write_text(
        json.dumps(bars, ensure_ascii=False), encoding="utf-8")
    (ws / "market" / "daily_bars_raw.json").write_text(
        json.dumps({s: [{"dt": r["dt"], "close": r["close"]} for r in v]
                    for s, v in bars.items()}), encoding="utf-8")
    (ws / "market" / "cross_check_tx.json").write_text(
        json.dumps({s: [{"dt": r["dt"], "close": r["close"]} for r in v]
                    for s, v in bars.items()}), encoding="utf-8")
    (ws / "market" / "trade_calendar.json").write_text(
        json.dumps(sorted({r["dt"] for v in bars.values() for r in v})), encoding="utf-8")

    # 回测锚点（等权、默认参数），首跑生成后人工审阅冻结
    import sys
    sys.path.insert(0, "/workspace/alphapocket")
    from applib import backtest as BT
    out = BT.run_backtest(bars, PF, benchmark_secid="600901.SH")
    anchor = {"strategy_metrics": out["strategy_metrics"],
              "n_days": out["n_days"], "n_rebalances": out["n_rebalances"],
              "benchmark": {"secid": out["benchmark"]["secid"],
                            "metrics": out["benchmark"]["metrics"]}}
    exp = BASE / "expected"
    exp.mkdir(exist_ok=True)
    (exp / "anchor_backtest.json").write_text(
        json.dumps(anchor, ensure_ascii=False, indent=2), encoding="utf-8")
    print("生成完成：", ws)
    print(json.dumps(anchor["strategy_metrics"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
