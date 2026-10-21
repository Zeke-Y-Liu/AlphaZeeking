"""技术指标纯函数（单公司分析「短线超卖」模块，T2.3）。

纪律：纯计算、无 IO、不抛异常；输入为 ``daily_bars`` 序列
``[{"dt","open","high","low","close","volume","amount","pct"}, …]``（升序）。
样本不足或字段缺失 → 对应值 None（调用方按「数据不足」展示，绝不阻断分析）。
"""
from __future__ import annotations


def _closes(bars: list[dict]) -> list[float]:
    return [float(b["close"]) for b in bars if b.get("close") is not None]


def rsi(bars: list[dict], n: int = 14) -> float | None:
    """Wilder RSI（n 日）。需 ≥ n+1 根收盘价；全跌无涨 → 0，全涨无跌 → 100。"""
    closes = _closes(bars)
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / n, losses / n
    for i in range(n + 1, len(closes)):          # Wilder 平滑
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0.0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0.0)) / n
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100.0 - 100.0 / (1.0 + rs), 2)


def kdj(bars: list[dict], n: int = 9, m1: int = 3, m2: int = 3) -> dict:
    """KDJ(9,3,3) 标准递推（K/D 初值 50）。样本不足 → 三值 None。"""
    rows = [b for b in bars if b.get("close") is not None
            and b.get("high") is not None and b.get("low") is not None]
    if len(rows) < n:
        return {"k": None, "d": None, "j": None}
    k = d = 50.0
    for i in range(len(rows)):
        win = rows[max(0, i - n + 1): i + 1]
        hi = max(float(x["high"]) for x in win)
        lo = min(float(x["low"]) for x in win)
        c = float(rows[i]["close"])
        rsv = 50.0 if hi == lo else (c - lo) / (hi - lo) * 100.0
        k = (m1 - 1) / m1 * k + 1 / m1 * rsv
        d = (m2 - 1) / m2 * d + 1 / m2 * k
    return {"k": round(k, 2), "d": round(d, 2), "j": round(3 * k - 2 * d, 2)}


def oversold_score(bars: list[dict]) -> dict:
    """短线超卖判定：四条件 + 信号清单。

    条件（任一成立 → ``oversold=True``；``score`` 为满足条数，供强度分级）：
      - RSI(14) < 30；
      - KDJ 的 J < 0；
      - 20 日收益 ≤ -8%（``rev_20d`` = 取负后的超跌幅度，>0.08 即超跌）；
      - 距 20 日最高价回撤 ≥ 15%。
    回撤按收盘价对近 20 根最高价计（``dist_high20`` ≤ 0，负数即回撤）。
    """
    closes = _closes(bars)
    r = rsi(bars)
    kd = kdj(bars)
    rev_20d = None
    if len(closes) >= 21 and closes[-21]:
        rev_20d = round(-(closes[-1] / closes[-21] - 1), 4)
    highs20 = [float(b["high"]) for b in bars[-20:]
               if b.get("high") is not None]
    dist_high20 = None
    if highs20 and closes and max(highs20):
        dist_high20 = round(closes[-1] / max(highs20) - 1, 4)

    signals: list[str] = []
    if r is not None and r < 30:
        signals.append(f"RSI(14)={r}<30")
    if kd["j"] is not None and kd["j"] < 0:
        signals.append(f"KDJ-J={kd['j']}<0")
    if rev_20d is not None and rev_20d > 0.08:
        signals.append(f"20日超跌{rev_20d:.2%}")
    if dist_high20 is not None and dist_high20 <= -0.15:
        signals.append(f"距20日高点{dist_high20:.2%}")

    return {
        "rsi": r,
        "k": kd["k"], "d": kd["d"], "j": kd["j"],
        "rev_20d": rev_20d,
        "dist_high20": dist_high20,
        "score": len(signals),
        "oversold": bool(signals),
        "signals": signals,
        "bars_n": len(closes),
    }
