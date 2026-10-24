"""组合回测（V4-α 真实数据版）

口径：等权名义组合（默认 20%×5），默认按月末最后交易日再平衡回等权（含换手成本）；
基准：默认沪深300ETF(510300) 买入持有。再平衡频率 / 基准标的 / 佣金均可参数化，
默认参数与 V4-α 既有产物逐位一致（锚点 A2）。
全部基于 dwd 层真实日线 pct（新浪不复权价）确定性复现，缺 K 线/份额折算日按 0 收益计。

局限（报告内明示）：ETF/LOF 无复权与分红接口 → 为「价格收益」，低估含息总收益；
未计滑点/冲击成本；月度再平衡以月末收盘价执行，非可成交价。
"""
from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

from . import factors as F

COMMISSION = 0.00025   # ETF/LOF 佣金万 2.5（单边，免印花税）；保留以兼容旧引用
DEFAULT_COST_BPS = 2.5  # 默认佣金 2.5bps（万 2.5，与 α 一致）

# P1.4 条件门：卖出信号一致性阈值。规格为「多信号一致才触发」→ 需 ≥2 个卖出类信号
# 同时成立才触发（单一卖出信号不触发，避免过度触发侵蚀收益）。
GATE_MIN_SELL_SIGNALS = 2
# P1.4 软衰减系数：触发门时对标的权重 ×gate_damp（默认 0.5 软降权，释放额度留现金），
# 而非硬剔除归零 —— 从根上避免「卖在低点」与「单票 100% 集中」两病灶。
GATE_DAMP_DEFAULT = 0.5

# 再平衡频率：monthly=月末最后交易日（α 现有行为）；quarterly/yearly=日历季末/年末；
# daily=α 复盘口径（每日收盘回等权）；buyhold=期初等权后不再平衡
REBALANCE_CHOICES = ("daily", "monthly", "quarterly", "yearly", "buyhold")
_FREQ_LONG = {"daily": "每日再平衡", "monthly": "月度再平衡", "quarterly": "季度再平衡",
              "yearly": "年度再平衡", "buyhold": "买入持有"}
_FREQ_SHORT = {"daily": "日再平衡", "monthly": "月再平衡", "quarterly": "季再平衡",
               "yearly": "年再平衡", "buyhold": "买入持有"}
_FREQ_EXEC = {"daily": "每日收盘回等权", "monthly": "月末最后交易日回等权",
              "quarterly": "季末最后交易日回等权", "yearly": "年末最后交易日回等权",
              "buyhold": "期初等权后不再平衡"}
_FREQ_ASSUME_MD = {"daily": "每日收盘按**收盘价**再平衡回等权",
                   "monthly": "月末最后交易日按**收盘价**再平衡回等权",
                   "quarterly": "季末最后交易日按**收盘价**再平衡回等权",
                   "yearly": "年末最后交易日按**收盘价**再平衡回等权",
                   "buyhold": "期初等权后不再平衡（买入持有）"}
_FREQ_ASSUME_HTML = {"daily": "每日收盘按收盘价再平衡回等权",
                     "monthly": "月末最后交易日按收盘价再平衡回等权",
                     "quarterly": "季末最后交易日按收盘价再平衡回等权",
                     "yearly": "年末最后交易日按收盘价再平衡回等权",
                     "buyhold": "期初等权后不再平衡（买入持有）"}


def _freq_of(bt: dict) -> str:
    """返回产物使用的再平衡频率（兼容无该字段的旧产物）。"""
    r = bt.get("rebalance", "monthly")
    return r if r in REBALANCE_CHOICES else "monthly"


def _bench_name(bt: dict, default: str = "沪深300ETF") -> str:
    b = bt.get("benchmark")
    return b["name"] if b else default


def _cost_label(bt: dict) -> str:
    return f"佣金万{bt.get('cost_bps', DEFAULT_COST_BPS):g}"


def _cost_n_avg_total(bt: dict) -> tuple[int | None, float | None, float | None]:
    """cost 块跨路径兼容（M6 T-C5）：
    close 路径 {total, avg_turnover}；next_vwap 路径 {total_fees} + events[turnover] 派生均值。
    渲染层统一经此取数，缺值如实 N/A（禁止编 0）。"""
    c = bt.get("cost") or {}
    turns = [e["turnover"] for e in (c.get("events") or []) if "turnover" in e]
    avg = c.get("avg_turnover")
    if avg is None and turns:
        avg = sum(turns) / len(turns)
    total = c.get("total", c.get("total_fees"))
    return c.get("n", len(turns) or None), avg, total


# ── 指标 ────────────────────────────────────────────────
def _compound(rets: list[float]) -> float:
    c = 1.0
    for r in rets:
        c *= (1 + r)
    return c


def _metrics(series: list, rets: list[float], rf: float = 0.0) -> dict:
    nav = [v for _, v in series]
    nd = len(rets)
    total = nav[-1] / nav[0] - 1
    cagr = (nav[-1] / nav[0]) ** (252 / nd) - 1 if nd else 0.0
    mean = sum(rets) / nd
    sd = math.sqrt(sum((x - mean) ** 2 for x in rets) / (nd - 1)) if nd > 1 else 0.0
    annvol = sd * math.sqrt(252)
    down = [x for x in rets if x < 0]
    dsd = math.sqrt(sum(x * x for x in down) / (len(down) - 1)) if len(down) > 1 else 0.0
    sharpe = (mean * 252 - rf) / annvol if annvol else None
    sortino = (mean * 252 - rf) / (dsd * math.sqrt(252)) if dsd else None
    # 最大回撤（含峰/谷/修复日）：峰取「回撤发生前的最高净值点」
    peak, peak_i, mdd, trough_i, dd_peak_i = nav[0], 0, 0.0, 0, 0
    for i, v in enumerate(nav):
        if v > peak:
            peak, peak_i = v, i
        dd = v / peak - 1
        if dd < mdd:
            mdd, trough_i, dd_peak_i = dd, i, peak_i
    recover_i = next((i for i in range(trough_i, nd) if nav[i] >= nav[dd_peak_i]), None)
    return {
        "total_return": total, "cagr": cagr, "ann_vol": annvol,
        "sharpe": sharpe, "sortino": sortino, "max_dd": mdd,
        "calmar": cagr / abs(mdd) if mdd else None,
        "win_rate": sum(1 for x in rets if x > 0) / nd if nd else 0.0,
        "dd_peak": series[dd_peak_i][0], "dd_trough": series[trough_i][0],
        "dd_recover": series[recover_i][0] if recover_i is not None else None,
        "dd_days": trough_i - dd_peak_i,
        "n_days": nd,
    }


def _monthly(rets_by_date: list) -> dict:
    """按年月聚合：{ 'YYYY-MM': 月收益 }"""
    m = {}
    for d, r in rets_by_date:
        m.setdefault(d[:7], 1.0)
        m[d[:7]] *= (1 + r)
    return {k: v - 1 for k, v in m.items()}


def _yearly_table(mret: dict) -> dict:
    """{ 'YYYY': {'01':r,... , 'Y': 全年} }"""
    t = {}
    for ym, r in mret.items():
        y, mo = ym[:4], ym[5:]
        t.setdefault(y, {})[mo] = r
        t[y]["Y"] = t[y].get("Y", 1.0) * (1 + r)
    for row in t.values():
        row["Y"] -= 1
    return t


def _rebalance_dates(dates: list, rebalance: str) -> set:
    """按频率返回再平衡日（各频率均落在「区间内该（季/年/月）末最后一个交易日」）。

    - monthly  ：每月末最后交易日（α 现有行为，含区间首月末）
    - quarterly：日历季末（3/6/9/12 月）最后交易日
    - yearly   ：日历年末（12 月）最后交易日
    - daily    ：每个交易日（α 复盘口径）
    - buyhold  ：无（期初等权后不再平衡）
    """
    if rebalance == "buyhold":
        return set()
    if rebalance == "daily":
        return set(dates)
    months = {d[:7] for d in dates}
    if rebalance == "monthly":
        keep = months
    elif rebalance == "quarterly":
        keep = {m for m in months if int(m[5:]) % 3 == 0}
    elif rebalance == "yearly":
        keep = {m for m in months if int(m[5:]) == 12}
    else:
        raise ValueError(f"未知再平衡频率：{rebalance}（可选 {REBALANCE_CHOICES}）")
    return {max(d for d in dates if d[:7] == m) for m in keep}


# ── M6 T-C3：撮合精验（次日 VWAP + 成本表 + 交易约束）─────────────────────
# 缺省路径（exec_mode="close"）与既有锚点 bit 级一致；next_vwap 仅在 spec 显式给出时激活。
DEFAULT_COST_TABLE = {
    "commission_rate": 0.00025, "commission_min_cny": 5.0,     # 佣金万 2.5 双边 min¥5
    "stamp_tax_rate": 0.0005, "stamp_tax_side": "sell",        # 印花税 0.05% 卖出
    "transfer_fee_rate": 0.00001,                              # 过户费 0.001% 双边
    "slippage_bps": 5.0,                                       # 滑点：买 ×(1+s) / 卖 ×(1-s)
}
_DEFER_CAP = 5            # defer 顺延上限（日）；超限 → skip + 告警计数
_EPS = 1e-9


def order_fees(amount: float, side: str, cost_table: dict | None = None) -> dict:
    """单笔费用（表驱动，成本只准从 cost_table 读）：佣金双边 min¥5 + 印花税卖出 + 过户费双边。

    side 须为 "buy" | "sell"；印花税按 stamp_tax_side 只计匹配方向。
    """
    ct = {**DEFAULT_COST_TABLE, **(cost_table or {})}
    amt = abs(float(amount))
    sell = side == "sell"
    stamp_rate = ct["stamp_tax_rate"] if (
        (ct.get("stamp_tax_side", "sell") == "sell") == sell) else 0.0
    return {"commission": round(max(amt * ct["commission_rate"], ct["commission_min_cny"]), 6),
            "stamp_tax": round(amt * stamp_rate, 6),
            "transfer_fee": round(amt * ct["transfer_fee_rate"], 6)}


def _fill_price(row: dict) -> tuple[float | None, str]:
    """成交价：VWAP = amount/volume；自洽性校验失败 → avg4 兜底（basis 如实标注）。

    T-C5 数据自洽防御：sandbox A 股行 amount（不复权口径）与 OHLC（前复权）系统性矛盾
    （ratio=amount/(volume·close) 中位 1.08~1.19 且逐行不稳）→ VWAP 越出当日 [low,high]
    可行域即不可信，降级 avg4（O/H/L/C 均在界内，均值必然可行）。
    降级是数据质量处置而非放行：orders.basis='avg4' + data_quality 计数如实披露。
    """
    vol, amt = row.get("volume"), row.get("amount")
    lo, hi = row.get("low"), row.get("high")
    if vol and amt:
        px = amt / vol
        # VWAP 必 ∈ [low, high]（含 1e-9 浮点容差）；越界 = amount 与行情口径矛盾。
        # low/high 缺失时无从校验可行域 → 信任 VWAP（真实行情行均带 OHLC）。
        if lo is None or hi is None or (lo - _EPS <= px <= hi + _EPS):
            return px, "vwap"
        return _fill_price({**row, "amount": None})[0], "avg4_vwap_out_of_range"
    o, h, l, c = (row.get(k) for k in ("open", "high", "low", "close"))
    if None not in (o, h, l, c):
        return (o + h + l + c) / 4.0, "avg4"
    return (row.get("close") if row.get("close") else None), "close"


def _lot_round(shares: float, lot: int) -> int:
    """整手取整：lot>0 → 向下取整手；lot=0（关闭）→ 向下取整股。"""
    if lot > 0:
        return int(shares // lot) * lot
    return math.floor(max(shares, 0.0))


def _find_exec(rows: list[dict], after: str, *, direction: str, t_plus_1: bool,
               earliest_sell: str | None, limit_pol: str, susp_pol: str,
               cap: int = _DEFER_CAP) -> tuple[dict, int] | tuple[None, str]:
    """从 after 之后的 bar 链找首个可成交日；返回 (row, defer_days) 或 (None, skip 原因)。

    - 停牌 = 无行（不在此链内）| volume==0；一字板 = high==low（无对手盘，阈值法不做）；
    - defer：顺延到下一可成交日（仍按该日 VWAP 重定价，禁止沿用原日价格）；超 cap → skip；
    - T+1 卖出：earliest_sell 之后的 bar 才可卖（d+1 买入份额 earliest_sell = d+2）。
    """
    cand = [r for r in rows if r["dt"] > after][:cap + 1]
    for i, r in enumerate(cand):
        if not r.get("volume"):
            if susp_pol == "skip":
                return None, "suspension"
            continue                                    # defer
        if r.get("high") is not None and r.get("low") is not None and r["high"] == r["low"]:
            if limit_pol == "skip":
                return None, "limit_move"
            continue                                    # defer
        if direction == "sell" and t_plus_1 and earliest_sell and r["dt"] < earliest_sell:
            continue                                    # T+1 defer
        return r, i
    return None, "defer_cap"


_ZERO_COST = {k: 0.0 for k in ("commission_rate", "commission_min_cny", "stamp_tax_rate",
                               "transfer_fee_rate", "slippage_bps")}


def _next_vwap_core(bars: dict, portfolio: dict, *, start: str | None, end: str | None,
                    rf: float, rebalance: str, benchmark_secid: str, rule_fn,
                    ct: dict, constraints: dict | None, capital: float) -> dict:
    """次日 VWAP 撮合核心（份额制，单套成本表一次重放）：选股日 d 定目标（d 收盘 sizing，PIT）→
    d+1（或顺延日）VWAP 成交 → 逐日收盘 mark-to-market。

    - 成本表驱动（order_fees）+ 滑点（买 ×(1+s) / 卖 ×(1-s)，滑点不计入无偷价审计——
      审计对象是基础成交价 VWAP/avg4 ∈ [low, high]）；
    - 约束：T+1 / 一字板 defer|skip / 停牌 defer|skip / 整手（残余进 cash）；
    - 资金不足买入 → 按整手向下缩减（确定性），缩到 0 仍不足 → 弃单计数。
    """
    slip = ct["slippage_bps"] / 10000.0
    con = constraints or {}
    t1 = bool(con.get("t_plus_1"))
    lot = int(con.get("lot_size") or 0)
    limit_pol = con.get("limit_move", "defer")
    susp_pol = con.get("suspension", "defer")

    secids = [p["secid"] for p in portfolio["positions"]]
    names = {p["secid"]: p["name"] for p in portfolio["positions"]}
    dates = sorted(set().union(*[{r["dt"] for r in bars[s]} for s in secids]))
    if start:
        dates = [d for d in dates if d >= start]
    if end:
        dates = [d for d in dates if d <= end]
    if not dates:
        raise ValueError("区间内无交易日")
    rebal = _rebalance_dates(dates, rebalance)
    rows_by = {s: sorted(bars[s], key=lambda r: r["dt"]) for s in secids}
    close_at = {s: {r["dt"]: r["close"] for r in rows_by[s]} for s in secids}

    def last_close(s: str, d: str) -> float | None:
        prior = [x for x in close_at[s] if x <= d]
        return close_at[s][max(prior)] if prior else None

    def next_bar_dt(s: str, d: str) -> str | None:
        nxt = [r["dt"] for r in rows_by[s] if r["dt"] > d]
        return nxt[0] if nxt else None

    cash = float(capital)
    pos = {s: 0 for s in secids}
    earliest_sell: dict[str, str | None] = {s: None for s in secids}
    pending: list[dict] = []
    orders: list[dict] = []
    skipped: list[dict] = []
    cancelled = 0
    events: list[dict] = []
    series: list[tuple[str, float]] = []

    for d in dates:
        # ① 到期订单执行（基础价 = 当日 VWAP/avg4；滑点与费用只影响现金）
        still: list[dict] = []
        for p in pending:
            if p["exec_dt"] != d:
                still.append(p)
                continue
            row, s, shares, direction = p["row"], p["secid"], p["shares"], p["direction"]
            px, basis = _fill_price(row)
            if px is None:
                skipped.append({"dt": d, "secid": s, "direction": direction,
                                "reason": "no_price", "defer_days": p["defer_days"]})
                continue
            if direction == "buy":
                price = px * (1 + slip)
                n = shares
                while n > 0:
                    amt = n * price
                    fees = order_fees(amt, "buy", ct)
                    if amt + sum(fees.values()) <= cash + _EPS:
                        break
                    n -= (lot if lot > 0 else 1)
                if n <= 0:
                    skipped.append({"dt": d, "secid": s, "direction": direction,
                                    "reason": "insufficient_cash", "defer_days": p["defer_days"]})
                    continue
                shares = n
                amt = shares * price
                fees = order_fees(amt, "buy", ct)
                cash -= amt + sum(fees.values())
                pos[s] += shares
                if t1:
                    es = next_bar_dt(s, d)
                    if es and (earliest_sell[s] is None or es > earliest_sell[s]):
                        earliest_sell[s] = es
            else:
                price = px * (1 - slip)
                amt = shares * price
                fees = order_fees(amt, "sell", ct)
                cash += amt - sum(fees.values())
                pos[s] -= shares
            orders.append({"decided_at": p["decided_at"], "exec_dt": d, "secid": s,
                           "direction": direction, "shares": shares,
                           "price_base": px, "price": price, "basis": basis,
                           "defer_days": p["defer_days"], "amount": amt,
                           "fees": fees, "low": row.get("low"), "high": row.get("high")})
        pending = still

        # ② 逐日收盘 mark-to-market（缺 K 线用最近已知收盘，停牌持仓不蒸发）
        mtm = cash + sum(pos[s] * (last_close(s, d) or 0.0) for s in secids)
        series.append((d, round(mtm / capital, 10)))

        # ③ 再平衡决策（sizing 用 d 收盘——决策时点已知，PIT）
        if d in rebal:
            tw = {s: float(x) for s, x in dict(rule_fn(d)).items() if x}
            total = mtm
            cancelled += len(pending)
            pending = []
            turn = sum(abs(tw.get(s, 0.0) * total - pos[s] * (last_close(s, d) or 0.0))
                       for s in set(tw) | set(secids)) / (2 * total) if total else 0.0
            for s in sorted(set(tw) | set(secids)):
                tgt_v = tw.get(s, 0.0) * total
                px_d = last_close(s, d)
                if px_d is None or px_d <= 0:
                    continue
                tsh = _lot_round(tgt_v / px_d, lot)
                delta = tsh - pos[s]
                if delta == 0:
                    continue
                direction = "buy" if delta > 0 else "sell"
                row, info = _find_exec(rows_by[s], d, direction=direction, t_plus_1=t1,
                                       earliest_sell=earliest_sell[s], limit_pol=limit_pol,
                                       susp_pol=susp_pol)
                if row is None:
                    skipped.append({"dt": d, "secid": s, "direction": direction,
                                    "reason": info, "defer_days": None})
                    continue
                pending.append({"decided_at": d, "exec_dt": row["dt"], "secid": s,
                                "direction": direction, "shares": abs(delta),
                                "row": row, "defer_days": info})
            events.append({"dt": d, "n_orders": len(pending), "turnover": round(turn, 6),
                           "n_skipped": sum(1 for x in skipped if x["dt"] == d)})

    # 审计与指标（无偷价：基础成交价 ∈ [当日 low, high]；滑点另列）
    violations = []
    for o in orders:
        lo, hi, p = o["low"], o["high"], o["price_base"]
        if lo is None or hi is None or not (lo - _EPS <= p <= hi + _EPS):
            violations.append({"exec_dt": o["exec_dt"], "secid": o["secid"],
                               "price": p, "low": lo, "high": hi})
    n_deferred = sum(1 for o in orders if o["defer_days"] > 0)

    rets = [series[0][1] - 1.0] + [series[i][1] / series[i - 1][1] - 1 for i in range(1, len(series))]
    strat = _metrics(series, rets, rf)
    bench = None
    if benchmark_secid and benchmark_secid in bars:
        bmap = {r["dt"]: (r["pct"] if r["pct"] is not None else 0.0) for r in bars[benchmark_secid]}
        bnav, bseries, brets = 1.0, [], []
        for d in dates:
            r = bmap.get(d, 0.0)
            bnav *= (1 + r)
            brets.append(r)
            bseries.append((d, bnav))
        bname = names.get(benchmark_secid) or ("沪深300ETF" if benchmark_secid == "510300.SH"
                                               else benchmark_secid)
        bench = {"secid": benchmark_secid, "name": bname, "series": bseries,
                 "metrics": _metrics(bseries, brets, rf)}
    mret = _monthly(list(zip(dates, rets)))
    fees_sum = {k: round(sum(o["fees"][k] for o in orders), 6) for k in
                ("commission", "stamp_tax", "transfer_fee")}
    return {
        "portfolio": portfolio["id"], "portfolio_name": portfolio["name"],
        "strategy": "份额制·次日VWAP成交（成本表驱动·T+1/涨跌停/停牌/整手约束）",
        "weighting": "equal", "rebalance": rebalance,
        "benchmark_secid": benchmark_secid, "exec_mode": "next_vwap",
        "cost_table": ct, "constraints": con, "capital": capital,
        "start": dates[0], "end": dates[-1], "n_days": len(dates),
        "n_rebalances": len(events), "rf": rf,
        "series": series, "strategy_metrics": strat, "benchmark": bench,
        "excess": {"total": strat["total_return"] - bench["metrics"]["total_return"] if bench else None,
                   "cagr": strat["cagr"] - bench["metrics"]["cagr"] if bench else None},
        "monthly": mret, "yearly": _yearly_table(mret),
        "cost": {"n": len(orders), "total_fees": round(sum(fees_sum.values()), 6),
                 "fees": fees_sum, "slippage_bps": ct["slippage_bps"],
                 "n_deferred": n_deferred, "n_skipped": len(skipped),
                 "n_cancelled": cancelled,
                 "events": events},
        "exec_orders": orders, "exec_skipped": skipped,
        "no_lookahead": {"check": "fail" if violations else "pass",
                         "n_orders": len(orders), "n_deferred": n_deferred,
                         "n_skipped": len(skipped), "n_violations": len(violations),
                         "violations": violations[:10]},
        "data_quality": {"note": "份额制撮合：VWAP=amount/volume（缺 amount → avg4 兜底，basis 标注）；"
                                 "VWAP 越出当日 [low,high] → amount 与行情口径矛盾，降级 avg4"
                                 "（avg4_vwap_out_of_range，如实计数）；"
                                 "停牌持仓按最近已知收盘估值；无偷价审计对象为基础成交价（不含滑点）",
                         "fill_basis": {b: sum(1 for o in orders if o["basis"] == b)
                                        for b in sorted({o["basis"] for o in orders })}},
    }


def _run_backtest_next_vwap(bars: dict, portfolio: dict, *, start: str | None, end: str | None,
                            rf: float, rebalance: str, benchmark_secid: str, rule_fn,
                            cost_table: dict | None, constraints: dict | None,
                            capital: float) -> dict:
    """次日 VWAP 撮合执行器：净轨（含全成本）+ gross 轨（零费用零滑点重放，同算法）双轨。

    「不含成本」gross = 零成本表口径（非扣滑点口径）；报告两列渲染归 T-C5，本层出数据。
    """
    ct = {**DEFAULT_COST_TABLE, **(cost_table or {})}
    net = _next_vwap_core(bars, portfolio, start=start, end=end, rf=rf, rebalance=rebalance,
                          benchmark_secid=benchmark_secid, rule_fn=rule_fn, ct=ct,
                          constraints=constraints, capital=capital)
    gross = _next_vwap_core(bars, portfolio, start=start, end=end, rf=rf, rebalance=rebalance,
                            benchmark_secid=benchmark_secid, rule_fn=rule_fn,
                            ct={**ct, **_ZERO_COST}, constraints=constraints, capital=capital)
    net["series_gross"] = gross["series"]
    net["strategy_metrics_gross"] = gross["strategy_metrics"]
    net["cost"]["net_final"] = net["series"][-1][1]
    net["cost"]["gross_final"] = gross["series"][-1][1]
    net["cost"]["gross_minus_net"] = round(gross["series"][-1][1] - net["series"][-1][1], 10)
    return net


# ── 回测主流程 ──────────────────────────────────────────
def run_backtest(bars: dict, portfolio: dict, start: str | None = None,
                 end: str | None = None, rf: float = 0.0,
                 rebalance: str = "monthly", benchmark_secid: str = "510300.SH",
                 cost_bps: float = DEFAULT_COST_BPS, weighting: str = "equal",
                 er_window: int = 250, max_weight: float | None = None,
                 risk_limits: dict | None = None, sectors: dict | None = None,
                 track_risk: bool = False, ab_gate: bool = False,
                 gate_damp: float = GATE_DAMP_DEFAULT,
                 gate_redistribute: bool = False,
                 rule_fn=None, exec_mode: str = "close",
                 cost_table: dict | None = None, constraints: dict | None = None,
                 capital: float = 1_000_000.0) -> dict:
    """组合回测主流程。

    ``ab_gate``（P1.4 辅助型条件变量）：引擎 A 买入需引擎 B 不持卖出信号——再平衡日先按
    PIT 重算引擎 B 信号，对「多信号一致」看空的标的（≥ ``GATE_MIN_SELL_SIGNALS`` 个卖出类
    信号同时成立）**软衰减**：权重 ×``gate_damp``（默认 0.5），释放额度默认留现金（降敞口）；
    ``gate_redistribute=True`` 时把释放额度按比例再分配给未触发标的（保持满仓）。
    ``gate_damp=0`` 退化为硬剔除。默认 ``ab_gate=False`` 时与既有 V4-α 锚点逐位一致。

    ``rule_fn``（M2 因子驱动动态持仓）：``rule_fn(d) -> {secid: weight}``——再平衡日 d 的
    目标权重由调用方决定（如 factorlib 因子截面 Top-N）。与 ``ab_gate`` 互斥；
    ``portfolio.positions`` 须为动态标的**全集**（各期规则输出的并集）。每期目标权重
    记入 ``rule_targets``（per-asset 权重 = 各期持仓频率 × 该期 1/n_target 的均值）。
    默认 ``None`` 时行为与既有锚点逐位一致。
    """
    if rebalance not in REBALANCE_CHOICES:
        raise ValueError(f"未知再平衡频率：{rebalance}（可选 {REBALANCE_CHOICES}）")
    if weighting not in ("equal", "equal_risk"):
        raise ValueError(f"未知加权方案：{weighting}（可选 equal / equal_risk）")
    if rule_fn is not None and ab_gate:
        raise ValueError("rule_fn（动态持仓）与 ab_gate 互斥")
    if exec_mode == "next_vwap":
        return _run_backtest_next_vwap(bars, portfolio, start=start, end=end, rf=rf,
                                       rebalance=rebalance, benchmark_secid=benchmark_secid,
                                       rule_fn=rule_fn, cost_table=cost_table,
                                       constraints=constraints, capital=capital)
    if exec_mode != "close":
        raise ValueError(f"未知 exec_mode：{exec_mode!r}（可选 close | next_vwap）")
    secids = [p["secid"] for p in portfolio["positions"]]
    names = {p["secid"]: p["name"] for p in portfolio["positions"]}
    dates = sorted(set().union(*[{r["dt"] for r in bars[s]} for s in secids]))
    if start:
        dates = [d for d in dates if d >= start]
    if end:
        dates = [d for d in dates if d <= end]
    rmap = {s: {r["dt"]: (r["pct"] if r["pct"] is not None else 0.0) for r in bars[s]} for s in secids}
    missing = {s: sum(1 for d in dates if d not in rmap[s]) for s in secids}

    n = len(secids)
    rebal = _rebalance_dates(dates, rebalance)
    commission = cost_bps / 10000.0
    tracked = (weighting == "equal_risk") or bool(risk_limits) or bool(track_risk)
    if tracked:
        from .risk import (
            annualized_vol,
            apply_limits,
            inverse_vol_weights,
            risk_contrib_var,
            sector_of,
        )
        sec_map = {s: sector_of((sectors or {}).get(s)) for s in secids}

    def _trailing_vols(d: str) -> dict:
        past = [x for x in dates if x < d][-int(er_window):]
        return {s: annualized_vol([rmap[s].get(x, 0.0) for x in past]) for s in secids}

    def _gate_factors(d: str) -> dict:
        """P1.4 条件门：引擎 B「多信号一致」看空的标的 → 权重衰减系数（PIT：仅用 d 及之前数据）。

        规格「引擎 A 买入需引擎 B 不持有卖出信号（多信号一致才触发）」→ 需
        ``GATE_MIN_SELL_SIGNALS`` 个及以上卖出类信号同时成立才触发衰减（单一卖出信号不触发）。
        K 线不足 20 日窗口（无法识别信号）→ 系数 1.0（不误触发）。
        """
        out = {}
        for s in secids:
            n_hist = sum(1 for r in bars[s] if r.get("close") and r["dt"] <= d)
            if n_hist < 21:
                out[s] = 1.0
                continue
            n_sell = sum(1 for sig in F.compute_beta(s, bars[s], d)["signals"]
                         if "卖出" in (sig.get("type") or ""))
            out[s] = gate_damp if n_sell >= GATE_MIN_SELL_SIGNALS else 1.0
        return out

    def _rule(d: str):
        """再平衡目标权重（PIT：仅用 d 之前数据）；返回 (权重, 波动表)。

        基准满仓等权（或等风险）覆盖全部标的；``ab_gate`` 时对触发标的权重 ×衰减系数，
        释放额度默认留现金（降敞口），``gate_redistribute`` 时按比例再分配给未触发标的。
        ``rule_fn``（M2 动态持仓）：目标权重完全委托调用方（因子截面 Top-N 等），
        每期目标记入 ``rule_targets`` 供 per-asset 平均权重归因。
        """
        if rule_fn is not None:
            tw = {s: float(x) for s, x in dict(rule_fn(d)).items() if x}
            unknown = set(tw) - set(secids)
            if unknown:
                raise ValueError(f"动态规则返回未登记标的：{sorted(unknown)}")
            w = {s: tw.get(s, 0.0) for s in secids}   # 归一化为全集映射（空权 = 未持有）
            rule_targets.append({"dt": d, "n": len(tw), "weights": dict(tw)})
            return w, None
        if not tracked:
            base = {s: 1.0 / n for s in secids}
            vols = None
        else:
            vols = _trailing_vols(d)
            base = (inverse_vol_weights({s: vols[s] for s in secids}, max_weight)
                    if weighting == "equal_risk" else {s: 1.0 / n for s in secids})
        w = dict(base)
        if ab_gate:
            fac = _gate_factors(d)
            w = {s: base[s] * fac[s] for s in secids}
            if gate_redistribute:
                invested = sum(w.values())
                if invested > 0:
                    w = {s: w[s] / invested for s in secids}   # 释放额度再分配 → 保持满仓
            gate_events.append({"dt": d, "n_flagged": sum(1 for s in secids if fac[s] < 1.0),
                                "n_total": n, "flagged": [s for s in secids if fac[s] < 1.0],
                                "invested_ratio": round(sum(w.values()), 6)})
        if tracked and risk_limits:
            w, bind = apply_limits(w, sec_map, vols, risk_limits)
            limits_binding.extend({"dt": d, **b} for b in bind)
        return w, vols

    limits_binding: list[dict] = []
    gate_events: list[dict] = []
    rc_vars: list[float] = []
    rule_targets: list[dict] = []
    w, _ = _rule(dates[0])
    tgt0 = dict(w)
    nav, series, costs = 1.0, [], []
    for d in dates:
        r = {s: rmap[s].get(d, 0.0) for s in secids}
        pr = sum(w[s] * r[s] for s in secids)
        nav *= (1 + pr)
        series.append((d, nav))
        for s in secids:   # 权重随价格漂移
            w[s] = w[s] * (1 + r[s]) / (1 + pr)
        if d in rebal:     # 再平衡日收盘先漂移再回目标权重，计提换手成本
            tgt, vols = _rule(d)
            turn = sum(abs(tgt[s] - w[s]) for s in secids)
            cost = turn * commission
            nav *= (1 - cost)
            series[-1] = (d, nav)
            costs.append({"dt": d, "turnover": turn, "cost": cost})
            if track_risk and vols is not None:
                v = risk_contrib_var(tgt, vols)
                if v is not None:
                    rc_vars.append(v)
            w = dict(tgt)
    tgt_final = dict(w)

    # 日收益由净值反推（含成本口径一致）
    rets = [series[0][1] - 1.0] + [series[i][1] / series[i - 1][1] - 1 for i in range(1, len(series))]
    strat = _metrics(series, rets, rf)

    # 基准：benchmark_secid 买入持有（同日期轴；标的缺失则降级为 None）
    bench = None
    if benchmark_secid and benchmark_secid in bars:
        bmap = {r["dt"]: (r["pct"] if r["pct"] is not None else 0.0) for r in bars[benchmark_secid]}
        bnav, bseries, brets = 1.0, [], []
        for d in dates:
            r = bmap.get(d, 0.0)
            bnav *= (1 + r)
            brets.append(r)
            bseries.append((d, bnav))
        bname = names.get(benchmark_secid) or ("沪深300ETF" if benchmark_secid == "510300.SH" else benchmark_secid)
        bench = {"secid": benchmark_secid, "name": bname,
                 "series": bseries, "metrics": _metrics(bseries, brets, rf)}

    # 标的贡献（动态持仓：权重 = 各再平衡日「持仓频率 × 1/n_target」的均值）
    port_ret = rets
    if rule_targets:
        _acc: dict[str, float] = {s: 0.0 for s in secids}
        for t in rule_targets:
            nw = t["n"] or 1
            for s in secids:
                _acc[s] += (1.0 / nw) if s in t["weights"] else 0.0
        avg_w = {s: v / len(rule_targets) for s, v in _acc.items()}
    else:
        avg_w = {s: 1.0 / n for s in secids}
    per_asset = []
    for s in secids:
        ar = [rmap[s].get(d, 0.0) for d in dates]
        aser, cum = [], 1.0
        for d, r in zip(dates, ar):
            cum *= (1 + r)
            aser.append((d, cum))
        m = _metrics(aser, ar, rf)
        per_asset.append({
            "secid": s, "name": names[s], "weight": avg_w[s],
            "total_return": m["total_return"], "cagr": m["cagr"], "ann_vol": m["ann_vol"],
            "max_dd": m["max_dd"], "corr": _corr(ar, port_ret),
        })

    mret = _monthly(list(zip(dates, rets)))
    total_cost = (1 - _compound([-c["cost"] for c in costs])) if costs else 0.0
    wlab = "等风险加权" if weighting == "equal_risk" else "等权"
    # 等权时标注权重构成（与 α 锚点文案逐位一致）；等风险加权权重不一，不标
    wsub = "" if weighting == "equal_risk" else f"20%×{n}，"
    glab = ""
    if ab_gate:
        gsub = "剔除" if gate_damp <= 0 else f"衰减×{gate_damp:g}"
        glab = f"·条件门(A×B 不持卖出信号，{gsub}{'，再分配' if gate_redistribute else ''})"
    strategy = (f"{wlab}·买入持有{glab}（{wsub}期初等权后不再平衡，无再平衡成本）"
                if rebalance == "buyhold"
                else f"{wlab}·{_FREQ_LONG[rebalance]}{glab}（{wsub}{_FREQ_EXEC[rebalance]}，含换手成本）")
    gate = None
    if ab_gate and gate_events:
        gate = {
            "n_rebalances": len(gate_events),
            "n_flagged": sum(e["n_flagged"] for e in gate_events),
            "n_fully_derisked": sum(1 for e in gate_events if e["invested_ratio"] <= 1e-9),
            "avg_invested_ratio": round(sum(e["invested_ratio"] for e in gate_events)
                                        / len(gate_events), 4),
            "gate_damp": gate_damp, "redistribute": gate_redistribute,
            "events": gate_events,
        }
    return {
        "portfolio": portfolio["id"], "portfolio_name": portfolio["name"],
        "strategy": strategy, "weighting": weighting, "ab_gate": ab_gate,
        "rebalance": rebalance, "benchmark_secid": benchmark_secid,
        "start": dates[0], "end": dates[-1], "n_days": len(dates), "n_rebalances": len(costs),
        "commission": commission, "cost_bps": cost_bps, "rf": rf,
        "series": series, "benchmark": bench,
        "strategy_metrics": strat,
        "excess": {"total": strat["total_return"] - bench["metrics"]["total_return"] if bench else None,
                   "cagr": strat["cagr"] - bench["metrics"]["cagr"] if bench else None},
        "monthly": mret, "yearly": _yearly_table(mret),
        "per_asset": per_asset,
        "cost": {"n": len(costs), "total": total_cost,
                 "avg_turnover": sum(c["turnover"] for c in costs) / len(costs) if costs else 0.0,
                 # M6 T-C5：期换手率序列（next_vwap 路径同名字段对齐；渲染层与 DoD#4 消费）
                 "events": [{"dt": c["dt"], "turnover": c["turnover"], "cost": c["cost"]}
                            for c in costs]},
        # P0 风险口径产物：目标权重 / 限额生效记录 / 再平衡时点风险贡献方差均值
        "initial_weights": tgt0, "target_weights": tgt_final,
        "limits_binding": limits_binding,
        # M2 动态持仓产物：各再平衡日目标组合（规则驱动时非空）
        "rule_targets": rule_targets if rule_fn is not None else None,
        # P1.4 条件门产物：门事件 + 平均持仓比（衰减系数；1.0=从未触发门）
        "gate": gate, "gate_damp": gate_damp, "gate_redistribute": gate_redistribute,
        "risk_contrib_var": (sum(rc_vars) / len(rc_vars)) if rc_vars else None,
        "data_quality": {"missing_bars": missing,
                         "note": "ETF/LOF 新浪不复权价 → 价格收益；513100 于 2022-01-14 份额折算日 pct 缺失按 0 计；缺 K 线按 0 收益",
                         "corporate_action": "513100.SH 2022-01-14 疑似份额折算（-80.5%），该日不计收益"},
    }


def _corr(a: list[float], b: list[float]) -> float | None:
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va == 0 or vb == 0:
        return None
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    return cov / math.sqrt(va * vb)


# ── 渲染 ────────────────────────────────────────────────
def _pct(v, d=1):
    return "—" if v is None else f"{v * 100:.{d}f}%"


def _num(v, d=2):
    return "—" if v is None else f"{v:.{d}f}"


def _cls(v):
    return "up" if (v or 0) > 0 else "down" if (v or 0) < 0 else ""


def _miss_txt(m: dict) -> str:
    items = [f"{k} {v} 根" for k, v in m.items() if v]
    return "、".join(items) if items else "无"


def _downsample(series: list, k: int = 240) -> list:
    if len(series) <= k:
        return series
    step = len(series) / k
    return [series[int(i * step)] for i in range(k)] + [series[-1]]


def _svg_chart(bt: dict, w: int = 860, h: int = 260) -> str:
    strat = _downsample(bt["series"])
    bench = _downsample(bt["benchmark"]["series"]) if bt["benchmark"] else None
    vals = [v for _, v in strat] + ([v for _, v in bench] if bench else [])
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.08 or 0.01
    lo, hi = lo - pad, hi + pad
    x0, y0, pw, ph = 46, 16, w - 62, h - 40

    def path(ser):
        n = len(ser) - 1
        pts = [f"{x0 + pw * i / n:.1f},{y0 + ph * (1 - (v - lo) / (hi - lo)):.1f}"
               for i, (_, v) in enumerate(ser)]
        return "M" + " L".join(pts)

    grid = "".join(
        f'<line x1="{x0}" y1="{y0 + ph * f:.1f}" x2="{x0 + pw}" y2="{y0 + ph * f:.1f}" '
        f'stroke="#DCD5C6" stroke-dasharray="3 4"/>'
        f'<text x="{x0 - 6}" y="{y0 + ph * f + 3:.1f}" text-anchor="end" font-size="9" fill="#9C9585">'
        f'{hi - (hi - lo) * f:.2f}</text>'
        for f in (0, 0.25, 0.5, 0.75, 1.0))
    bpath = (f'<path d="{path(bench)}" fill="none" stroke="#9C9585" stroke-width="1.4" stroke-dasharray="5 4"/>'
             if bench else "")
    dl = bt["series"][0][0]
    dr = bt["series"][-1][0]
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="净值曲线">'
            f'{grid}<path d="{path(strat)}" fill="none" stroke="#A93226" stroke-width="1.9"/>{bpath}'
            f'<text x="{x0}" y="{h - 6}" font-size="9" fill="#9C9585">{dl}</text>'
            f'<text x="{x0 + pw}" y="{h - 6}" text-anchor="end" font-size="9" fill="#9C9585">{dr}</text>'
            f'<text x="{x0}" y="11" font-size="9" fill="#A93226">— 组合（{_FREQ_SHORT[_freq_of(bt)]}）</text>'
            f'<text x="{x0 + 150}" y="11" font-size="9" fill="#9C9585">--- {_bench_name(bt)}</text></svg>')


def render_md(bt: dict) -> str:
    sm, bm = bt["strategy_metrics"], (bt["benchmark"]["metrics"] if bt["benchmark"] else None)
    L = []
    L.append(f"# 「{bt['portfolio_name']}」组合回测报告\n")
    L.append(f"组合 `{bt['portfolio']}` · 口径 `{bt['strategy']}` · 区间 `{bt['start']} ~ {bt['end']}`"
             f"（{bt['n_days']} 交易日 · {bt['n_rebalances']} 次再平衡）\n")
    L.append(f"## ① 绩效总览\n| 指标 | 组合（{_FREQ_SHORT[_freq_of(bt)]}） | {_bench_name(bt)}（买入持有） |")
    L.append("| --- | --- | --- |")
    rows = [("累计收益", "total_return", _pct), ("年化收益(CAGR)", "cagr", _pct),
            ("年化波动", "ann_vol", _pct), ("最大回撤", "max_dd", _pct),
            ("夏普(rf=0)", "sharpe", _num), ("索提诺", "sortino", _num),
            ("卡玛比率", "calmar", _num), ("日胜率", "win_rate", _pct)]
    for lab, k, f in rows:
        L.append(f"| {lab} | {f(sm[k])} | {f(bm[k]) if bm else '—'} |")
    if bt["excess"]["total"] is not None:
        L.append(f"| 超额（累计） | {_pct(bt['excess']['total'])}（组合 − 基准） | — |")
    L.append("")
    L.append(f"- 回撤区间：峰 {sm['dd_peak']} → 谷 {sm['dd_trough']}"
             f"（{sm['dd_days']} 交易日）→ {'修复 ' + sm['dd_recover'] if sm['dd_recover'] else '尚未修复'}")
    _cn, _cavg, _ctot = _cost_n_avg_total(bt)
    L.append(f"- 再平衡成本：{_cn} 次 · 平均单边换手 {_pct(_cavg)}"
             f" · 累计成本拖累 {_pct(_ctot, 2)}（{_cost_label(bt)}）\n")
    L.append("## ② 年度收益\n| 年份 | " + " | ".join(f"{m}月" for m in [f"{i:02d}" for i in range(1, 13)]) + " | 全年 |")
    L.append("| --- |" + " --- |" * 13)
    for y in sorted(bt["yearly"]):
        row = bt["yearly"][y]
        cells = [(_pct(row[m]) if m in row else "—") for m in [f"{i:02d}" for i in range(1, 13)]]
        L.append(f"| {y} | " + " | ".join(cells) + f" | **{_pct(row.get('Y'))}** |")
    L.append("")
    L.append("## ③ 标的贡献（同期、等权 20%）\n| 标的 | 区间累计 | 年化收益 | 年化波动 | 最大回撤 | 与组合相关性 |")
    L.append("| --- | --- | --- | --- | --- | --- |")
    if "per_asset" not in bt:
        L.append("— 数据缺失（next_vwap 撮合路径不产出标的贡献表；逐笔执行见 exec_orders）")
    for a in bt.get("per_asset") or []:
        L.append(f"| {a['name']} `{a['secid']}` | {_pct(a['total_return'])} | {_pct(a['cagr'])} "
                 f"| {_pct(a['ann_vol'])} | {_pct(a['max_dd'])} | {_num(a['corr'])} |")
    L.append("")
    L.append("## ④ 假设与局限")
    L.append("- 数据：AKShare 新浪日线（**不复权价**），ETF/LOF 无复权与分红接口 → 为**价格收益**，低估含息总收益（510300、511260 等分红/付息品种受影响）。")
    _mb = bt.get("data_quality", {}).get("missing_bars")
    _mbt = _miss_txt(_mb) if _mb is not None else "N/A（next_vwap 路径 data_quality 无明细）"
    L.append(f"- 513100 于 2022-01-14 疑似份额折算（-80.5%），该日 pct 缺失按 0 计；缺失 K 线同样按 0 收益计（{_mbt}）。")
    _fb = bt.get("data_quality", {}).get("fill_basis") or {}
    if any(k != "vwap" for k in _fb):
        L.append("- 成交价基准分布：" + " · ".join(f"`{k}` {v} 笔" for k, v in sorted(_fb.items()))
                 + " —— VWAP 越出当日 [low,high] = amount 与行情口径矛盾（不复权额 × 前复权价），"
                   "已降级 avg4 并如实计数；干净标的（ETF/LOF）仍用真 VWAP。")
    L.append(f"- {_FREQ_ASSUME_MD[_freq_of(bt)]}，未计滑点与冲击成本；成本仅含{_cost_label(bt)}（ETF/LOF 免印花税）。")
    L.append("- 名义组合、无税费/申赎约束；不构成投资建议。\n")
    if "factor_spec" in bt:                      # M6 T-C5：因子策略报告追加 ⑤-⑧ 新节
        L.append(_render_m6_md(bt))
    L.append("> 数据来自公开来源（AKShare 聚合新浪行情），仅供研究参考，不构成投资建议。")
    return "\n".join(L)


def render_html(bt: dict, css: str) -> str:
    sm, bm = bt["strategy_metrics"], (bt["benchmark"]["metrics"] if bt["benchmark"] else None)
    bname = bt["benchmark"]["name"] if bt["benchmark"] else "—"
    _cn, _cavg, _ctot = _cost_n_avg_total(bt)

    def row(lab, key, fmt):
        return (f'<tr><td>{lab}</td><td class="mono {_cls(sm[key])}">{fmt(sm[key])}</td>'
                f'<td class="mono {_cls(bm[key]) if bm else ""}">{fmt(bm[key]) if bm else "—"}</td></tr>')

    rows = "".join([
        row("累计收益", "total_return", _pct), row("年化收益 (CAGR)", "cagr", _pct),
        row("年化波动", "ann_vol", _pct), row("最大回撤", "max_dd", _pct),
        row("夏普比率 (rf=0)", "sharpe", _num), row("索提诺比率", "sortino", _num),
        row("卡玛比率", "calmar", _num), row("日胜率", "win_rate", _pct),
    ])
    yrows = ""
    for y in sorted(bt["yearly"]):
        r = bt["yearly"][y]
        cells = "".join(f'<td class="mono {_cls(r.get(m))}">{_pct(r.get(m))}</td>'
                        for m in [f"{i:02d}" for i in range(1, 13)])
        yrows += f'<tr><td><b>{y}</b></td>{cells}<td class="mono {_cls(r.get("Y"))}"><b>{_pct(r.get("Y"))}</b></td></tr>'
    arows = ""
    if "per_asset" not in bt:
        arows = ('<tr><td colspan="6">— 数据缺失（next_vwap 撮合路径不产出标的贡献表；'
                 '逐笔执行见 exec_orders）</td></tr>')
    for a in bt.get("per_asset") or []:
        arows += (f'<tr><td><b>{a["name"]}</b><br><span class="mono" style="font-size:9px;color:var(--mute)">{a["secid"]}</span></td>'
                  f'<td class="mono {_cls(a["total_return"])}">{_pct(a["total_return"])}</td>'
                  f'<td class="mono {_cls(a["cagr"])}">{_pct(a["cagr"])}</td><td class="mono">{_pct(a["ann_vol"])}</td>'
                  f'<td class="mono down">{_pct(a["max_dd"])}</td><td class="mono">{_num(a["corr"])}</td></tr>')
    mret = bt["monthly"]
    up_m = sum(1 for v in mret.values() if v > 0) / len(mret) if mret else 0
    best = max(mret.items(), key=lambda kv: kv[1]) if mret else None
    worst = min(mret.items(), key=lambda kv: kv[1]) if mret else None
    sec_m6 = _render_m6_html(bt) if "factor_spec" in bt else ""   # M6 T-C5：因子策略报告新节
    _fb = bt.get("data_quality", {}).get("fill_basis") or {}
    _fbtxt_html = ("<br><b>成交价基准</b>：" + " · ".join(f"{k} {v} 笔" for k, v in sorted(_fb.items()))
                   + "（VWAP 越出当日 [low,high] → amount 与行情口径矛盾，降级 avg4 并如实计数）"
                   if any(k != "vwap" for k in _fb) else "")

    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AlphaPocket · {bt['portfolio_name']} 回测报告</title><style>{css}</style></head><body>
<h1>「{bt['portfolio_name']}」组合回测报告</h1>
<div class="meta">组合 <b>{bt['portfolio']}</b> · 区间 <b>{bt['start']} ~ {bt['end']}</b>（{bt['n_days']} 交易日）
· 再平衡 <b>{bt['n_rebalances']} 次</b> · 基准 <b>{bname}</b><br>
口径 {bt['strategy']} · 成本 {_cost_label(bt)} · 无风险利率 rf=0 · 数据 AKShare 新浪（不复权价）</div>

<h2>① 净值曲线<em>Equity Curve</em></h2>
<div class="card">{_svg_chart(bt)}
<div class="meta" style="margin-top:6px">起始净值=1.00（{bt['start']}）· 红线=组合（{_FREQ_LONG[_freq_of(bt)]}）· 灰虚线={bname}（买入持有）</div></div>

<h2>② 绩效总览<em>Performance</em></h2>
<div class="card"><table><tr><th>指标</th><th>组合（{_FREQ_SHORT[_freq_of(bt)]}）</th><th>{bname}（买入持有）</th></tr>{rows}</table>
<div class="meta" style="margin-top:8px">最大回撤区间：峰 {sm['dd_peak']} → 谷 {sm['dd_trough']}（{sm['dd_days']} 交易日）
→ {'修复 ' + sm['dd_recover'] if sm['dd_recover'] else '尚未修复'} ·
再平衡累计成本拖累 {_pct(_ctot, 2)} · 月度胜率 {_pct(up_m)}</div></div>

<h2>③ 年度/月度收益<em>Calendar Returns</em></h2>
<div class="card"><table style="font-size:10px"><tr><th>年份</th>
{''.join(f'<th>{i}月</th>' for i in range(1, 13))}<th>全年</th></tr>{yrows}</table>
<div class="meta" style="margin-top:8px">区间 {bt['start']} ~ {bt['end']}（{bt['start'][:4]} 起自 1 月）；最佳月 {best[0] if best else '—'} {_pct(best[1]) if best else ''}
· 最差月 {worst[0] if worst else '—'} {_pct(worst[1]) if worst else ''}</div></div>

<h2>④ 标的贡献<em>Per-Asset</em></h2>
<div class="card"><table><tr><th>标的</th><th>区间累计</th><th>年化收益</th><th>年化波动</th><th>最大回撤</th><th>与组合相关性</th></tr>{arows}</table>
<div class="warn"><span>◇</span><div>等权 20%×5；相关性为各标的日收益与组合日收益的相关系数，用于分散度评估（越低越分散）。</div></div></div>

<h2>⑤ 假设与局限<em>Assumptions</em></h2>
<div class="card"><div class="warn"><span>⚠</span><div>
<b>价格收益口径</b>：ETF/LOF 新浪行情为不复权价、无分红接口 → 低估含息总收益（510300、511260 等分红/付息品种受影响）。<br>
<b>份额折算</b>：513100 于 2022-01-14 疑似份额折算（-80.5%），该日 pct 缺失按 0 计；缺失 K 线同样按 0 收益计（{_miss_txt(bt['data_quality']['missing_bars']) if 'missing_bars' in bt.get('data_quality', {}) else 'N/A（next_vwap 路径 data_quality 无明细）'}）。<br>
<b>成交假设</b>：{_FREQ_ASSUME_HTML[_freq_of(bt)]}，未计滑点/冲击成本；成本仅含{_cost_label(bt)}（ETF/LOF 免印花税）。{_fbtxt_html}<br>
<b>其他</b>：名义组合，无申赎/税费约束；回测为历史数据分析，不代表未来表现。</div></div></div>

{sec_m6}<div class="disc"><b>合规与复现</b>：数据来自公开来源（AKShare 聚合新浪行情），仅供研究参考，不构成投资建议。
口径与结果确定性可复现：<code>python3 ap backtest --portfolio {bt['portfolio']} --start {bt['start']}</code>，
或经 MCP <code>backtest.run</code> 调用。</div>
</body></html>"""


# ── M6 T-C5：报告升级三块（年度分布 / 因子暴露 / 成本双列与换手）────────
_MISSING_GROSS = "— 数据缺失（T-C3 未启用双轨：exec=next_day_vwap 才有不含成本轨）"
_MISSING_EXP = "— 数据缺失（factor_exposure 仅 factor_backtest 产出）"
_M6_KEYS = ("cagr", "sharpe", "max_dd", "calmar")


def _yearly_from_series(series: list) -> dict[str, float]:
    """净值序列 → {YYYY: 年度复利收益}（跨年首日收益计入新年份；首日为基期）。"""
    out: dict[str, float] = {}
    prev = series[0][1]
    for d, v in series[1:]:
        out[d[:4]] = out.get(d[:4], 1.0) * (v / prev)
        prev = v
    return {y: round(p - 1, 6) for y, p in out.items()}


def _quantiles(vals: list[float]) -> dict[str, float | None]:
    """P10/P25/P50/P75/P90（线性插值，零依赖）。"""
    if not vals:
        return {k: None for k in ("P10", "P25", "P50", "P75", "P90")}
    vs = sorted(vals)

    def q(p: float) -> float:
        pos = (len(vs) - 1) * p
        lo, hi = math.floor(pos), math.ceil(pos)
        return vs[lo] if lo == hi else vs[lo] + (vs[hi] - vs[lo]) * (pos - lo)

    return {k: round(q(p), 6) for k, p in
            (("P10", .1), ("P25", .25), ("P50", .5), ("P75", .75), ("P90", .9))}


def _yearly_dist(bt: dict) -> dict:
    """年度收益分布：年份 × {含成本, 不含成本, 差额} + 含成本年度序列分位。"""
    net_y = _yearly_from_series(bt["series"])
    gross_y = _yearly_from_series(bt["series_gross"]) if "series_gross" in bt else None
    rows = []
    for y in sorted(net_y):
        g = gross_y.get(y) if gross_y else None
        rows.append({"year": y, "net": net_y[y],
                     "gross": round(g, 6) if g is not None else None,
                     "diff": round(g - net_y[y], 6) if g is not None else None})
    return {"rows": rows, "quantiles": _quantiles([r["net"] for r in rows]),
            "gross_available": gross_y is not None}


def _metrics_from_series(series: list) -> dict:
    """净值序列 → _metrics（rets 由相邻 nav 派生；首日为基期）。"""
    rets = [series[i][1] / series[i - 1][1] - 1 for i in range(1, len(series))]
    return _metrics(series, rets)


def _cost_dual(bt: dict) -> dict:
    """成本双列（§11 #5 硬纪律）：年化/夏普/回撤/卡玛 × {不含成本, 含成本}。"""
    if "series_gross" not in bt:
        return {"gross_available": False,
                "rows": {k: {"gross": None, "net": bt["strategy_metrics"][k]} for k in _M6_KEYS}}
    g = _metrics_from_series(bt["series_gross"])
    return {"gross_available": True,
            "rows": {k: {"gross": g[k], "net": bt["strategy_metrics"][k]} for k in _M6_KEYS}}


def _turnover_table(bt: dict) -> dict:
    """期换手率：均值 + 与 rebalance 频率匹配核对（DoD#4 后半句；buyhold → 无期）。"""
    ev = (bt.get("cost") or {}).get("events") or []
    turns = [e["turnover"] for e in ev]
    freq = bt.get("rebalance") or "monthly"
    expect = {"daily": 252, "weekly": 52, "monthly": 12}.get(freq)
    n_per_year = match = None
    if expect and turns and bt.get("start") and bt.get("end"):
        d0 = date.fromisoformat(bt["start"])
        d1 = date.fromisoformat(bt["end"])
        years = max((d1 - d0).days / 365.25, 1e-9)
        n_per_year = round(len(turns) / years, 2)
        match = abs(n_per_year - expect) / expect <= 0.25
    return {"freq": freq, "n_periods": len(turns), "avg_turnover":
            (sum(turns) / len(turns)) if turns else None,
            "periods_per_year": n_per_year, "expect_per_year": expect, "freq_match": match}


def _exposure_check(bt: dict) -> dict | None:
    """因子暴露表数据 + 方向一致性核对（asc → 暴露均值 <0.5；desc → >0.5）。"""
    exp = bt.get("factor_exposure")
    if exp is None:
        return None
    dirs = {f["id"]: f["direction"]
            for f in ((bt.get("factor_spec") or {}).get("factors") or [])}
    check = {}
    for fid, d in dirs.items():
        vals = [r["exposure"][fid] for r in exp
                if isinstance(r.get("exposure"), dict) and r["exposure"].get(fid) is not None]
        if not vals:
            continue
        mean = sum(vals) / len(vals)
        ok = (mean < 0.5) if d == "asc" else (mean > 0.5) if d == "desc" else None
        check[fid] = {"mean": round(mean, 4), "direction": d, "consistent": ok}
    return {"rows": exp, "check": check}


def _four_method_rows(bt: dict) -> dict | None:
    """四法并列汇总（CLI 注入 four_method_summary 后渲染）。"""
    return bt.get("four_method_summary")


def _render_m6_md(bt: dict) -> str:
    """T-C5 三块新节（md）：⑤ 年度分布 / ⑥ 因子暴露 / ⑦ 成本双列与换手（+⑧ 四法并列）。"""
    L = ["", "## ⑤ 年度收益分布（含成本 / 不含成本）",
         "| 年份 | 含成本 | 不含成本 | 差额（费用拖累） |", "| --- | --- | --- | --- |"]
    yd = _yearly_dist(bt)
    for r in yd["rows"]:
        if r["gross"] is None:
            L.append(f"| {r['year']} | {_pct(r['net'])} | {_MISSING_GROSS} | — |")
        else:
            L.append(f"| {r['year']} | {_pct(r['net'])} | {_pct(r['gross'])} | {_pct(r['diff'], 2)} |")
    q = yd["quantiles"]
    L.append(f"\n含成本年度收益分位：P10 {_pct(q['P10'])} · P25 {_pct(q['P25'])} · "
             f"P50 {_pct(q['P50'])} · P75 {_pct(q['P75'])} · P90 {_pct(q['P90'])}\n")

    L.append("## ⑥ 因子暴露（各期持仓 rank 均值）")
    exc = _exposure_check(bt)
    if exc is None:
        L.append(_MISSING_EXP + "\n")
    else:
        L.append("| 期 | " + " | ".join(exc["check"]) + " |")
        L.append("| --- |" + " --- |" * len(exc["check"]))
        for r in exc["rows"]:
            cells = [(_num(r["exposure"].get(fid), 4) if r["exposure"].get(fid) is not None else "—")
                     for fid in exc["check"]]
            L.append(f"| {r['dt']} | " + " | ".join(cells) + " |")
        L.append("")
        for fid, c in exc["check"].items():
            L.append(f"- {fid}（{c['direction']}）：均值暴露 {_num(c['mean'], 4)} → "
                     f"方向一致性 {'✓' if c['consistent'] else '✗（协议违规，须核查）'}")
        L.append("")

    L.append("## ⑦ 成本双列与换手（§11 #5：不含成本对照列）")
    cd = _cost_dual(bt)
    lab = {"cagr": "年化收益", "sharpe": "夏普(rf=0)", "max_dd": "最大回撤", "calmar": "卡玛比率"}
    L.append("| 指标 | 不含成本（gross） | 含成本（net） |")
    L.append("| --- | --- | --- |")
    for k in _M6_KEYS:
        g = cd["rows"][k]["gross"]
        gs = _MISSING_GROSS if g is None else _num(g, 4) if k in ("sharpe", "calmar") else _pct(g, 2)
        nv = cd["rows"][k]["net"]
        ns = _num(nv, 4) if k in ("sharpe", "calmar") else _pct(nv, 2)
        L.append(f"| {lab[k]} | {gs} | {ns} |")
    tn = _turnover_table(bt)
    if tn["n_periods"]:
        L.append(f"\n- 换手：{tn['n_periods']} 期 · 期均单边换手 {_pct(tn['avg_turnover'], 2)}"
                 f" · {tn['periods_per_year']} 期/年 vs 频率 {_FREQ_LONG.get(tn['freq'], tn['freq'])}"
                 f"基准 {tn['expect_per_year']} 期/年 → 匹配 {'✓' if tn['freq_match'] else '✗'}")
    else:
        L.append("\n- 换手：无再平衡期（buyhold）或 events 缺失。")
    L.append("")

    fm = _four_method_rows(bt)
    if fm:
        L.append("## ⑧ 四法并列汇总（T-C2：equal / ic_12m / ic_ir / lasso）")
        L.append("| 方法 | 累计收益 | 夏普 | 最大回撤 | 卡玛 | 降级日 | 冻结日 |")
        L.append("| --- | --- | --- | --- | --- | --- | --- |")
        for m, v in fm.items():
            L.append(f"| {m} | {_pct(v['total_return'])} | {_num(v['sharpe'])} "
                     f"| {_pct(v['max_dd'])} | {_num(v['calmar'])} | {v.get('n_degraded_days', '—')}"
                     f" | {v.get('n_frozen_days', '—')} |")
        L.append("")
    return "\n".join(L)


def _render_m6_html(bt: dict) -> str:
    """T-C5 三块新节（html，与 md 同构）。"""
    yd = _yearly_dist(bt)
    yrows = ""
    for r in yd["rows"]:
        gcell = f'<td class="mono">{_MISSING_GROSS}</td><td>—</td>' if r["gross"] is None else \
                f'<td class="mono">{_pct(r["gross"])}</td><td class="mono">{_pct(r["diff"], 2)}</td>'
        yrows += f'<tr><td><b>{r["year"]}</b></td><td class="mono {_cls(r["net"])}">{_pct(r["net"])}</td>{gcell}</tr>'
    q = yd["quantiles"]
    qline = (f'含成本年度收益分位 P10 {_pct(q["P10"])} · P25 {_pct(q["P25"])} · '
             f'P50 {_pct(q["P50"])} · P75 {_pct(q["P75"])} · P90 {_pct(q["P90"])}')

    exc = _exposure_check(bt)
    if exc is None:
        erows, enote = f'<tr><td colspan="3">{_MISSING_EXP}</td></tr>', ""
    else:
        fids = list(exc["check"])
        erows = ""
        for r in exc["rows"]:
            cells = "".join(f'<td class="mono">{_num(r["exposure"].get(f), 4) if r["exposure"].get(f) is not None else "—"}</td>'
                            for f in fids)
            erows += f'<tr><td class="mono">{r["dt"]}</td>{cells}</tr>'
        enote = "".join(
            f'{fid}（{c["direction"]}）均值 {_num(c["mean"], 4)} → '
            f'方向一致性 {"✓" if c["consistent"] else "✗（协议违规，须核查）"}；'
            for fid, c in exc["check"].items())

    cd = _cost_dual(bt)
    lab = {"cagr": "年化收益", "sharpe": "夏普(rf=0)", "max_dd": "最大回撤", "calmar": "卡玛比率"}
    crows = ""
    for k in _M6_KEYS:
        g = cd["rows"][k]["gross"]
        gs = _MISSING_GROSS if g is None else (_num(g, 4) if k in ("sharpe", "calmar") else _pct(g, 2))
        nv = cd["rows"][k]["net"]
        ns = _num(nv, 4) if k in ("sharpe", "calmar") else _pct(nv, 2)
        crows += f'<tr><td>{lab[k]}</td><td class="mono">{gs}</td><td class="mono">{ns}</td></tr>'
    tn = _turnover_table(bt)
    tnote = (f'{tn["n_periods"]} 期 · 期均单边换手 {_pct(tn["avg_turnover"], 2)} · '
             f'{tn["periods_per_year"]} 期/年 vs 基准 {tn["expect_per_year"]} → 匹配 '
             f'{"✓" if tn["freq_match"] else "✗"}') if tn["n_periods"] else "无再平衡期（buyhold）"

    fm = _four_method_rows(bt)
    if fm:
        frows = "".join(
            f'<tr><td><b>{m}</b></td><td class="mono {_cls(v["total_return"])}">{_pct(v["total_return"])}</td>'
            f'<td class="mono">{_num(v["sharpe"])}</td><td class="mono down">{_pct(v["max_dd"])}</td>'
            f'<td class="mono">{_num(v["calmar"])}</td><td>{v.get("n_degraded_days", "—")}</td>'
            f'<td>{v.get("n_frozen_days", "—")}</td></tr>'
            for m, v in fm.items())
        fsec = (f'<h2>⑨ 四法并列汇总<em>Method Panel</em></h2><div class="card">'
                f'<table><tr><th>方法</th><th>累计收益</th><th>夏普</th><th>最大回撤</th>'
                f'<th>卡玛</th><th>降级日</th><th>冻结日</th></tr>{frows}</table></div>')
    else:
        fsec = ""

    return f"""<h2>⑥ 年度收益分布<em>Yearly Distribution</em></h2>
<div class="card"><table><tr><th>年份</th><th>含成本</th><th>不含成本</th><th>差额（费用拖累）</th></tr>{yrows}</table>
<div class="meta" style="margin-top:6px">{qline}</div></div>

<h2>⑦ 因子暴露<em>Factor Exposure</em></h2>
<div class="card"><table><tr><th>期</th>{''.join(f'<th>{f}</th>' for f in (list(exc['check']) if exc else []))}</tr>{erows}</table>
<div class="meta" style="margin-top:6px">{enote}</div></div>

<h2>⑧ 成本双列与换手<em>Cost Dual & Turnover</em></h2>
<div class="card"><table><tr><th>指标</th><th>不含成本（gross）</th><th>含成本（net）</th></tr>{crows}</table>
<div class="meta" style="margin-top:6px">换手：{tnote} · 双列对照为 §11 #5 硬纪律（无不含成本列 = 验收不通过）</div></div>
{fsec}"""


def write_all(bt: dict, art_dir: Path, css: str) -> dict:
    art_dir.mkdir(parents=True, exist_ok=True)
    jp, mp, hp = art_dir / "backtest.json", art_dir / "backtest.md", art_dir / "backtest.html"
    jp.write_text(json.dumps(bt, ensure_ascii=False, indent=2), encoding="utf-8")
    mp.write_text(render_md(bt), encoding="utf-8")
    hp.write_text(render_html(bt, css), encoding="utf-8")
    return {"json": str(jp), "md": str(mp), "html": str(hp)}
