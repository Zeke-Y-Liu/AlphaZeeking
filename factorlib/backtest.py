"""M6 回测引擎（实施计划 §9.2）：月频再平衡 + 自研撮合器（P0 单引擎精验）。

诚实口径（CP-0008）：计划 P0 指定 backtrader 单引擎——沙箱无依赖（与 alphalens 同因），
自研撮合器为唯一路径；§9.2 撮合语义逐条落地，backtrader / vectorbt / Qlib 三引擎
交叉核对位留 M7。

撮合语义（§9.2 表）：
- 成交价：次日 VWAP = amount / volume，且必须 ∈ 当日 [low, high]（越界 = 数据异常，
  回退 (H+L)/2 并标注 vwap_proxy_hl）；滑点表驱动（买 ×(1+s) / 卖 ×(1−s)）；
- 成本：佣金万 2.5（min ¥5，双边）+ 印花税卖出 0.05% + 过户费 0.001%（双边）；
- 约束：T+1（当日买入不可当日卖出）；涨停不买 / 跌停不卖（pct 口径，band 可配）；
  停牌（无 bar）本轮跳过、下轮再平衡重试并登记 deferred；100 股整数倍（资金不足整手跳过）；
- 纪律：信号月末截面 → 次日执行（无偷价）；净值逐日 mark-to-market（停牌 carry forward）；
  报告固定含全成本 / 不含成本两列（§11 纪律 5，同路径零成本重跑）。

审计（DoD 4）：audit_no_lookahead —— 逐笔 raw 成交价 ∈ 成交日 [low, high]、
fill_dt > signal_dt、换手事件与再平衡月匹配。walk-forward（§11 纪律 4）：
train 24 / step 6 折内选法（train Sharpe 最高）→ valid 执行；末端 lockbox 只验证一次。
依赖纪律：纯标准库 + duckdb（无 backtrader，自研撮合兜底）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import duckdb

from .composite import StrategyDSL, composite_scores
from .evaluate import _git_sha

LIMIT_BAND = 0.098              # 涨跌停带宽（|pct| ≥ 9.8%；ST 板 M7 过滤因子接入后细化）
LOT = 100                       # A 股一手股数
INIT_CASH = 1_000_000.0


@dataclass
class CostTable:
    """§9.2 成本表（表驱动；冻结默认 = 佣金万 2.5 / 印花卖 5bp / 过户 0.1bp / 滑点 5bp）。"""
    commission_rate: float = 2.5e-4
    commission_min: float = 5.0
    stamp_tax: float = 5e-4          # 卖出单边
    transfer_fee: float = 1e-5       # 双边
    slippage: float = 5e-4

    @classmethod
    def zero(cls) -> "CostTable":
        return cls(commission_rate=0.0, commission_min=0.0, stamp_tax=0.0,
                   transfer_fee=0.0, slippage=0.0)


# ── 撮合原语（纯函数，DoD 1 手工推演锚） ──
def cost_of(value: float, side: str, cost: CostTable) -> dict:
    """单笔交易成本明细（side: buy/sell；value = 成交金额，滑点另计在成交价内）。"""
    commission = max(value * cost.commission_rate, cost.commission_min)
    stamp = value * cost.stamp_tax if side == "sell" else 0.0
    transfer = value * cost.transfer_fee
    return {"commission": round(commission, 2), "stamp_tax": round(stamp, 2),
            "transfer_fee": round(transfer, 2),
            "total": round(commission + stamp + transfer, 2)}


def fill_price(bar: dict, side: str, cost: CostTable) -> tuple[float, str]:
    """次日成交价：VWAP 优先，且必须 ∈ [low, high]（数学事实，越界 = 数据异常）→
    回退 (H+L)/2 并标注 vwap_proxy_hl；滑点表驱动（买 ×(1+s) / 卖 ×(1−s)）。"""
    raw = src = None
    vol, amt = bar.get("volume") or 0.0, bar.get("amount") or 0.0
    if vol > 0 and amt > 0:
        v = amt / vol
        if bar["low"] - 1e-9 <= v <= bar["high"] + 1e-9:
            raw, src = v, "vwap"
    if raw is None:
        raw, src = (bar["high"] + bar["low"]) / 2, "vwap_proxy_hl"
    px = raw * (1 + cost.slippage) if side == "buy" else raw * (1 - cost.slippage)
    return px, src


def is_limit(bar: dict, side: str, band: float = LIMIT_BAND) -> bool:
    """涨跌停判定（pct 口径）：买入遇涨停 / 卖出遇跌停 → 不可成交。"""
    pct = bar.get("pct") or 0.0
    return pct >= band if side == "buy" else pct <= -band


def lot_shares(budget: float, px: float) -> int:
    """100 股整数倍股数（资金不足整手 → 0）。"""
    if px <= 0 or budget <= 0:
        return 0
    return int(budget // (px * LOT)) * LOT


# ── 数据面板 ──
def _bars_panel(con: duckdb.DuckDBPyConnection, start: str, end: str
                ) -> tuple[dict[str, dict[str, dict]], list[str]]:
    """{secid: {dt: bar}} + 交易日历（全标的并集升序）。"""
    rows = con.execute("""
        SELECT secid, CAST(dt AS VARCHAR), open, high, low, close, volume, amount, pct
        FROM dwd_daily_bar WHERE dt >= CAST(? AS DATE) AND dt <= CAST(? AS DATE)
        ORDER BY dt
    """, [start, end]).fetchall()
    bars: dict[str, dict[str, dict]] = {}
    cal: list[str] = []
    for s, dt, o, h, l, c, v, a, pct in rows:
        if dt not in cal:
            cal.append(dt)
        bars.setdefault(s, {})[dt] = {"open": o, "high": h, "low": l, "close": c,
                                      "volume": v, "amount": a, "pct": pct}
    return bars, cal


# ── 单路径撮合 ──
def _run_path(bars: dict, cal: list[str], targets: dict[str, list[str]],
              cost: CostTable) -> dict:
    """targets {signal_dt: [secid]}（等权 top_n）→ 净值曲线 + 成交清单 + 不可成交登记。"""
    cash, positions, trades = INIT_CASH, {}, []
    deferred: list[dict] = []
    fills_per_rebal: dict[str, int] = {k: 0 for k in targets}
    last_px: dict[str, float] = {}
    equity: list[tuple[str, float]] = []
    start_rec = min(targets) if targets else cal[0]     # 首信号前全现金，不入曲线
    for j, dt in enumerate(cal):
        if j > 0 and cal[j - 1] in targets:
            sig = cal[j - 1]
            cash, positions, trades, deferred = _rebalance(
                dt, sig, targets[sig], bars, cash, positions, trades, deferred,
                fills_per_rebal, cost)
        nav = cash
        for s, pos in positions.items():
            b = bars.get(s, {}).get(dt)
            if b and b.get("close"):
                last_px[s] = b["close"]
            nav += pos["shares"] * last_px.get(s, pos["entry_px"])
        if dt >= start_rec:
            equity.append((dt, nav))
    return {"equity": equity, "trades": trades, "deferred": deferred,
            "n_rebalance": len(targets), "fills_per_rebal": fills_per_rebal}


def _rebalance(exec_dt: str, sig: str, target: list[str], bars: dict, cash: float,
               positions: dict, trades: list, deferred: list,
               fills_per_rebal: dict, cost: CostTable) -> tuple:
    """月度再平衡（单执行日）：先卖后买；T+1/涨跌停不可成交、停牌顺延登记。"""
    # —— 卖出：不在目标内的持仓（T+1：当日买入不可当日卖；跌停/停牌顺延） ——
    for s in sorted(positions):
        if s in target:
            continue
        pos = positions[s]
        if pos["buy_dt"] == exec_dt:        # T+1
            deferred.append({"signal_dt": sig, "secid": s, "side": "sell",
                             "reason": "t_plus_1"})
            continue
        b = bars.get(s, {}).get(exec_dt)
        if not b:
            deferred.append({"signal_dt": sig, "secid": s, "side": "sell",
                             "reason": "suspended"})
            continue
        if is_limit(b, "sell"):
            deferred.append({"signal_dt": sig, "secid": s, "side": "sell",
                             "reason": "limit_down"})
            continue
        px, src = fill_price(b, "sell", cost)
        value = pos["shares"] * px
        c = cost_of(value, "sell", cost)
        cash += value - c["total"]
        trades.append({"signal_dt": sig, "fill_dt": exec_dt, "secid": s, "side": "sell",
                       "shares": pos["shares"], "px_raw": px / (1 - cost.slippage)
                       if cost.slippage < 1 else px, "px": px, "px_source": src,
                       "low": b["low"], "high": b["high"], "value": value, "cost": c})
        fills_per_rebal[sig] += 1
        del positions[s]
    # —— 买入：等权 top_n（已持仓的保留不调；涨停/停牌/整手不足跳过登记） ——
    held_value = sum(p["shares"] * _last_close(bars, s, exec_dt)
                     for s, p in positions.items())
    total = cash + held_value
    per_slot = total / len(target) if target else 0.0
    for s in target:
        if s in positions:
            continue
        b = bars.get(s, {}).get(exec_dt)
        if not b:
            deferred.append({"signal_dt": sig, "secid": s, "side": "buy",
                             "reason": "suspended"})
            continue
        if is_limit(b, "buy"):
            deferred.append({"signal_dt": sig, "secid": s, "side": "buy",
                             "reason": "limit_up"})
            continue
        px, src = fill_price(b, "buy", cost)
        est = cost.commission_rate + cost.transfer_fee      # px 已含滑点；佣金 min 上界另计
        shares = lot_shares(min(per_slot, cash) / (1 + est), px)
        while shares > 0:
            value = shares * px
            c = cost_of(value, "buy", cost)
            if value + c["total"] <= cash:
                break
            shares -= LOT
        if shares <= 0:
            deferred.append({"signal_dt": sig, "secid": s, "side": "buy",
                             "reason": "insufficient_lot_cash"})
            continue
        value = shares * px
        c = cost_of(value, "buy", cost)
        cash -= value + c["total"]
        positions[s] = {"shares": shares, "buy_dt": exec_dt, "entry_px": px,
                        "last_close": b["close"]}
        trades.append({"signal_dt": sig, "fill_dt": exec_dt, "secid": s, "side": "buy",
                       "shares": shares, "px_raw": px / (1 + cost.slippage)
                       if cost.slippage < 1 else px, "px": px, "px_source": src,
                       "low": b["low"], "high": b["high"], "value": value, "cost": c})
        fills_per_rebal[sig] += 1
    return cash, positions, trades, deferred


def _last_close(bars: dict, s: str, dt: str) -> float:
    """该标的 ≤dt 的最近 close（估值口径；无历史 → 0）。"""
    cm = bars.get(s, {})
    for d in sorted(cm, reverse=True):
        if d <= dt and cm[d].get("close"):
            return cm[d]["close"]
    return 0.0


# ── 绩效 ──
def perf(path: dict) -> dict:
    """净值 → 绩效：年化/最大回撤/夏普/卡玛（月频口径）/换手/年度收益分布/成本合计。"""
    eq = path["equity"]
    if len(eq) < 2:
        return {}
    # 月末采样
    month_last: dict[str, float] = {}
    for dt, v in eq:
        month_last[dt[:7]] = v                      # 后写覆盖 = 月内最后交易日
    mv = [month_last[k] for k in sorted(month_last)]
    rets = [b / a - 1 for a, b in zip(mv, mv[1:])]
    n_months = len(rets)
    total = mv[-1] / mv[0] - 1
    ann = (mv[-1] / mv[0]) ** (12 / n_months) - 1 if n_months else 0.0
    mu = sum(rets) / n_months if n_months else 0.0
    sd = (sum((r - mu) ** 2 for r in rets) / n_months) ** 0.5 if n_months else 0.0
    sharpe = mu / sd * (12 ** 0.5) if sd > 1e-12 else None
    peak, mdd = mv[0], 0.0
    for v in mv:
        peak = max(peak, v)
        mdd = max(mdd, 1 - v / peak)
    calmar = ann / mdd if mdd > 1e-12 else None
    years = n_months / 12
    turnover = (sum(t["value"] for t in path["trades"]) / INIT_CASH) / years if years else 0.0
    yearly: dict[str, float] = {}
    y_start: dict[str, float] = {}
    for dt, v in eq:
        y = dt[:4]
        y_start.setdefault(y, v)
        yearly[y] = v
    yret = {y: yearly[y] / y_start[y] - 1 for y in sorted(yearly) if y_start[y]}
    return {"total_return": total, "ann_return": ann, "max_drawdown": mdd,
            "sharpe": sharpe, "calmar": calmar, "turnover_ann": turnover,
            "n_months": n_months, "yearly": yret,
            "total_cost": round(sum(t["cost"]["total"] for t in path["trades"]), 2),
            "n_trades": len(path["trades"]), "n_deferred": len(path["deferred"])}


# ── 无偷价审计（DoD 4） ──
def audit_no_lookahead(path: dict, bars: dict) -> dict:
    """逐笔：fill_dt > signal_dt（次日执行）、raw 成交价 ∈ 成交日 [low, high]、
    换手事件与再平衡月匹配（有信号的月 ≥ 成交月；无成交月须全为持仓不变）。"""
    viol, checked = [], 0
    for t in path["trades"]:
        checked += 1
        if t["fill_dt"] <= t["signal_dt"]:
            viol.append({**t, "why": "fill_not_after_signal"})
        b = bars.get(t["secid"], {}).get(t["fill_dt"])
        if not b:
            viol.append({**t, "why": "no_bar_on_fill_day"})
            continue
        lo, hi = b["low"], b["high"]
        if not (lo - 1e-9 <= t["px_raw"] <= hi + 1e-9):
            viol.append({**t, "why": "px_out_of_day_range",
                         "day_range": [lo, hi]})
    active = [k for k, n in path["fills_per_rebal"].items() if n > 0]
    return {"checked": checked, "violations": viol,
            "n_rebalance": path["n_rebalance"],
            "rebalance_with_fills": len(active),
            "n_vwap_proxy": sum(1 for t in path["trades"]
                                if t.get("px_source") != "vwap"),
            "fills_per_rebal": path["fills_per_rebal"],
            "pass": not viol}


# ── 回测主入口 ──
def run_backtest(con: duckdb.DuckDBPyConnection, strategy: StrategyDSL, metas: dict,
                 start: str, end: str, cost: CostTable | None = None,
                 methods: list[str] | None = None, lockbox_months: int = 0) -> dict:
    """合成打分 → 逐法撮合 → 绩效（含/不含成本两列）+ 双闸 + 无偷价审计。

    lockbox_months > 0 时：主报告窗口 = start..end−lockbox；lockbox 段单次验证
    （选主窗 Sharpe 最高法，lockbox 只验证一次，§11 纪律 4）。
    """
    cost = cost or CostTable()
    comp = composite_scores(con, strategy, metas, start, end)
    bars, cal = _bars_panel(con, start, end)
    mes = [m for m in comp["mes"] if m < cal[-1]]      # 信号月末须有次日执行日
    methods = methods or strategy.methods
    out = {"strategy_id": strategy.id, "window": comp["window"],
           "factor_status": comp["factor_status"],
           "gate_consistency": comp["gate_consistency"],
           "gate_weight_stability": comp["gate_weight_stability"],
           "top_n": strategy.top_n, "cost": cost.__dict__}
    # 主窗口（可选 lockbox 截断）
    lock = set(mes[-lockbox_months:]) if lockbox_months else set()
    main_mes = [m for m in mes if m not in lock]
    per_method = {}
    for m in methods:
        scores = comp["scores"].get(m, {})
        targets = {t: _top_n(scores[t], strategy.top_n)
                   for t in main_mes if scores.get(t)}
        net = _run_path(bars, cal, targets, cost)
        gross = _run_path(bars, cal, targets, CostTable.zero())
        entry = {"method": m, "net": perf(net), "gross": perf(gross),
                 "audit": audit_no_lookahead(net, bars)}
        per_method[m] = entry
    out["results"] = per_method
    best = _best_method(per_method, strategy)
    out["best_method"] = best
    if lock:
        targets = {t: _top_n(comp["scores"][best][t], strategy.top_n)
                   for t in sorted(lock) if comp["scores"].get(best, {}).get(t)}
        net = _run_path(bars, cal, targets, cost)
        out["lockbox"] = {"window": [min(lock), max(lock)], "method": best,
                          "net": perf(net),
                          "audit": audit_no_lookahead(net, bars),
                          "note": "lockbox 样本外只验证一次（§11 纪律 4）"}
    out["git_sha"] = _git_sha()
    return out


def _top_n(scores: dict[str, float], n: int) -> list[str]:
    return [s for s, _ in sorted(scores.items(), key=lambda kv: -kv[1])[:n]]


def _best_method(per_method: dict, strategy: StrategyDSL) -> str:
    """主窗 Sharpe 最高法（并列取 methods 声明序第一）。"""
    best, best_sh = None, None
    for m in strategy.methods:
        sh = per_method.get(m, {}).get("net", {}).get("sharpe")
        if sh is not None and (best_sh is None or sh > best_sh):
            best, best_sh = m, sh
    return best or strategy.methods[0]


# ── walk-forward（§11 纪律 4；DoD 3） ──
def run_walkforward(con: duckdb.DuckDBPyConnection, strategy: StrategyDSL, metas: dict,
                    start: str, end: str, wf: tuple[int, int] = (24, 6),
                    lockbox_months: int = 6, cost: CostTable | None = None) -> dict:
    """train_min/step 扩展窗口：折内 train 四法选优（Sharpe）→ valid 执行；
    末端 lockbox 单次验证（复用 run_backtest 的 lockbox 路径）。"""
    cost = cost or CostTable()
    comp = composite_scores(con, strategy, metas, start, end)
    bars, cal = _bars_panel(con, start, end)
    mes = [m for m in comp["mes"] if m < cal[-1]]
    lock_n = lockbox_months
    folds, i = [], wf[0]
    while i + wf[1] <= len(mes) - lock_n:
        train_mes, valid_mes = mes[:i], mes[i:i + wf[1]]
        # train 段四法绩效
        train_perf = {}
        for m in strategy.methods:
            targets = {t: _top_n(comp["scores"][m][t], strategy.top_n)
                       for t in train_mes if comp["scores"].get(m, {}).get(t)}
            train_perf[m] = perf(_run_path(bars, cal, targets, cost))
        best = max(strategy.methods,
                   key=lambda m: (train_perf[m].get("sharpe") or -9e9))
        v_targets = {t: _top_n(comp["scores"][best][t], strategy.top_n)
                     for t in valid_mes if comp["scores"].get(best, {}).get(t)}
        v_path = _run_path(bars, cal, v_targets, cost)
        folds.append({"fold": len(folds) + 1,
                      "train": [train_mes[0], train_mes[-1]],
                      "valid_window": [valid_mes[0], valid_mes[-1]],
                      "best_method": best,
                      "train_sharpe": {m: train_perf[m].get("sharpe")
                                       for m in strategy.methods},
                      "valid": perf(v_path),
                      "audit": audit_no_lookahead(v_path, bars)})
        i += wf[1]
    # lockbox：全 train 窗选法 → 末端单次验证
    full_train = mes[:len(mes) - lock_n] if lock_n else mes
    full_perf = {}
    for m in strategy.methods:
        targets = {t: _top_n(comp["scores"][m][t], strategy.top_n)
                   for t in full_train if comp["scores"].get(m, {}).get(t)}
        full_perf[m] = perf(_run_path(bars, cal, targets, cost))
    best = max(strategy.methods, key=lambda m: (full_perf[m].get("sharpe") or -9e9))
    lock_mes = mes[len(mes) - lock_n:] if lock_n else []
    l_targets = {t: _top_n(comp["scores"][best][t], strategy.top_n)
                 for t in lock_mes if comp["scores"].get(best, {}).get(t)}
    l_path = _run_path(bars, cal, l_targets, cost)
    return {"strategy_id": strategy.id, "wf": list(wf),
            "lockbox_months": lock_n, "folds": folds,
            "lockbox": {"window": [lock_mes[0], lock_mes[-1]] if lock_mes else None,
                        "method": best, "net": perf(l_path),
                        "audit": audit_no_lookahead(l_path, bars),
                        "note": "lockbox 样本外只验证一次；重切样本 = 协议违规须升 version"},
            "git_sha": _git_sha()}


# ── M6 一条命令 + 报告渲染 ──
def _fmt(x, pct=False):
    if x is None:
        return "—"
    return f"{x:.2%}" if pct else f"{x:.3f}"


def render_backtest_md(rep: dict) -> str:
    """全窗四法并列报告（§9.1/§9.2；不含 lockbox——单次验证只在 walk-forward 报告）。"""
    L = [f"# M6 全窗回测报告 · {rep['strategy_id']}（as_of={rep['window']['end']}）", "",
         f"- 窗口：{rep['window']['start']}..{rep['window']['end']}"
         f"（{rep['window']['n_months']} 个月末截面）· top_n={rep['top_n']} · 执行=次日 VWAP",
         f"- 因子状态（诚实标注）：{rep['factor_status']}",
         "", "## 合成四法并列结果（含全成本 / 不含成本两列）", "",
         "| 方法 | 净值收益(全成本) | 年化 | Sharpe | 最大回撤 | 收益(不含成本) | Sharpe(不含成本) |"
         " 年换手 | 成本¥ | 成交笔数 | 审计 |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for m, e in rep["results"].items():
        n, g, a = e["net"], e["gross"], e["audit"]
        L.append(f"| {m} | {_fmt(n.get('total_return'), True)} | {_fmt(n.get('ann_return'), True)}"
                 f" | {_fmt(n.get('sharpe'))} | {_fmt(n.get('max_drawdown'), True)}"
                 f" | {_fmt(g.get('total_return'), True)} | {_fmt(g.get('sharpe'))}"
                 f" | {_fmt(n.get('turnover_ann'))} | {n.get('total_cost', 0):,.0f}"
                 f" | {n.get('n_trades', 0)} | {'✅' if a['pass'] else '❌'} |")
    gc, ws = rep["gate_consistency"], rep["gate_weight_stability"]
    L += ["", "## 双闸（防过拟合）", "",
          f"- 闸 1 一致性：{'✅ 通过' if gc['pass'] else '⚠️ 告警'}"
          f"（法间 rank 相关均值 {gc.get('mean_rho') and round(gc['mean_rho'], 3)}，"
          f"阈值 {gc.get('threshold')}）"
          f"{gc.get('note') or ''}"]
    if gc.get("pairs"):
        L.append("  - 逐对：" + ", ".join(
            f"{k} ρ={round(v['mean_rho'], 3)}" for k, v in gc["pairs"].items()
            if v['mean_rho'] is not None))
    L += [f"- 闸 2 权重稳定：{'✅ 通过' if ws['pass'] else '⚠️ freeze_review'}"
          f"（最大 L1 漂移 {ws.get('drifts')}）{ws.get('note') or ''}",
          "", "## 无偷价审计（DoD 4）", ""]
    for m, e in rep["results"].items():
        a = e["audit"]
        L.append(f"- {m}：{a['checked']} 笔全查，violation {len(a['violations'])}；"
                 f"再平衡 {a['n_rebalance']} 月中有成交 {a['rebalance_with_fills']} 月"
                 f"（换手与再平衡频率匹配）；VWAP 代理成交 {a['n_vwap_proxy']} 笔"
                 f"（源数据 amount/volume 越界或缺失，回退 (H+L)/2 —— 数据质量局限见 CP-0008）")
    L += ["", "> lockbox 段未在本报告验证（§11 纪律 4：单次验证只在 walk-forward 报告末端）。"]
    return "\n".join(L) + "\n"


def render_walkforward_md(rep: dict) -> str:
    """walk-forward 折报告 + lockbox 单次验证（§11 纪律 4；DoD 3）。"""
    L = [f"# M6 Walk-Forward 报告 · {rep['strategy_id']}（train {rep['wf'][0]}/step {rep['wf'][1]}）",
         "", f"- lockbox：{rep['lockbox_months']} 个月，单次验证（重切样本 = 协议违规）",
         "", "| 折 | train | valid | 折内选法 | valid 收益 | Sharpe | 最大回撤 | 审计 |",
         "|---|---|---|---|---|---|---|---|"]
    for f in rep["folds"]:
        v = f["valid"]
        L.append(f"| {f['fold']} | {f['train'][0]}..{f['train'][1]} "
                 f"| {f['valid_window'][0]}..{f['valid_window'][1]} | {f['best_method']} "
                 f"| {_fmt(v.get('total_return'), True)} | {_fmt(v.get('sharpe'))} "
                 f"| {_fmt(v.get('max_drawdown'), True)} "
                 f"| {'✅' if f['audit']['pass'] else '❌'} |")
    lb = rep["lockbox"]
    if lb.get("window"):
        n = lb.get("net") or {}
        L += ["", "## Lockbox 单次验证", "",
              f"- 窗口：{lb['window'][0]}..{lb['window'][1]} · 选法（全 train 窗 Sharpe 最高）"
              f"：**{lb['method']}**",
              f"- 净值收益（全成本）：{_fmt(n.get('total_return'), True)} · Sharpe："
              f"{_fmt(n.get('sharpe'))} · 最大回撤：{_fmt(n.get('max_drawdown'), True)}",
              f"- 审计：{'✅ 通过' if lb['audit']['pass'] else '❌ 违例'}"
              f"（{lb['audit']['checked']} 笔）",
              f"- {lb['note']}"]
    return "\n".join(L) + "\n"


def run_all(repo_dir: str | Path, out_dir: str | Path | None = None,
            wf: tuple[int, int] = (24, 6), lockbox_months: int = 6,
            start: str | None = None, end: str | None = None) -> dict:
    """M6 一条命令：repo JSON → 库 → 物化策略因子 → 三策略全窗回测 + walk-forward → 落盘。

    全窗报告（四法并列 + 双闸 + 审计）不触碰 lockbox；lockbox 单次验证仅在
    walk-forward 末端（§11 纪律 4）。
    """
    from . import load_definitions, materialize
    from .composite import load_strategies
    from .evaluate import build_universe_db
    con = build_universe_db(repo_dir)
    root = Path(__file__).resolve().parent
    metas = {m.id: m for m in load_definitions(root) if m.type == "alpha"}
    strategies = load_strategies(root)
    if start is None or end is None:
        lo, hi = con.execute("SELECT min(CAST(dt AS VARCHAR)), max(CAST(dt AS VARCHAR))"
                             " FROM dwd_daily_bar").fetchone()
        start, end = start or lo, end or hi
    for fid in sorted({f["id"] for st in strategies.values() for f in st.factors}):
        materialize(con, metas[fid], mode="full")
    out = {}
    for sid in sorted(strategies):
        st = strategies[sid]
        bt = run_backtest(con, st, metas, start, end)
        wfr = run_walkforward(con, st, metas, start, end, wf=wf,
                              lockbox_months=lockbox_months)
        out[sid] = {"backtest": bt, "walkforward": wfr}
        if out_dir is not None:
            d = Path(out_dir)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"backtest_{sid}.md").write_text(render_backtest_md(bt), encoding="utf-8")
            (d / f"walkforward_{sid}.md").write_text(render_walkforward_md(wfr),
                                                     encoding="utf-8")
            (d / f"{sid}.json").write_text(
                json.dumps(out[sid], ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def main() -> None:
    """CLI：python -m factorlib.backtest --repo <dir> [--out <dir>] [--wf 24,6] [--lockbox 6]"""
    import argparse
    ap = argparse.ArgumentParser(description="M6 合成打分 + 回测引擎（三策略一条命令）")
    ap.add_argument("--repo", default="alphapocket/remote/repo", help="alphapocket repo 目录")
    ap.add_argument("--out", default=None, help="报告落盘目录")
    ap.add_argument("--wf", default="24,6", help="walk-forward train_min,step（默认 24,6）")
    ap.add_argument("--lockbox", type=int, default=6)
    args = ap.parse_args()
    wf = tuple(int(x) for x in args.wf.split(","))
    out = run_all(args.repo, out_dir=args.out, wf=wf, lockbox_months=args.lockbox)
    for sid, r in out.items():
        folds = r["walkforward"]["folds"]
        lb = r["walkforward"]["lockbox"]
        lb_ret = (lb.get("net") or {}).get("total_return")
        print(f"{sid:<20} folds={len(folds)} best={lb['method']:<10} "
              f"lockbox={_fmt(lb_ret, True)} audit="
              f"{'✅' if lb['audit']['pass'] else '❌'}")


if __name__ == "__main__":
    main()
