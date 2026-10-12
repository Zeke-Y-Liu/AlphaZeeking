"""M7 快筛引擎（实施计划 §10.2 第一环：vectorbt 参数网格快筛位）。

三引擎分工（§10.2）：快筛（本模块，无约束近似）→ 精验（backtest.py 撮合器）
→ 交叉核对（crosscheck.py）。快筛只负责排序筛选，不做投资结论。

依赖纪律：vectorbt 沙箱无依赖（与 backtrader/alphalens 同因）→ 自研无约束
比例复利核；vectorbt 接入位 = VBT_AVAILABLE hook，依赖可用时接入并比对
网格排序一致性（Spearman）后再替换。

无约束近似口径（与精验的差异显式声明，报告头部固定展示）：
- 月末收盘价成交（精验 = 次日 VWAP ∈ [low,high]）；无 T+1/涨跌停/停牌/整手约束；
- 成本 = 单边 cost_rate × 单边换手（精验 = 表驱动逐笔：佣金万2.5 min¥5/印花/过户/滑点）；
- 满仓等权目标，月末调仓（权重漂移用上月个股月收益）。
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb

try:                                            # vectorbt 接入位（沙箱无依赖）
    import vectorbt as vbt                      # noqa: F401
    VBT_AVAILABLE = True
except ImportError:
    VBT_AVAILABLE = False

from .composite import composite_scores
from .evaluate import close_map, month_ends


def month_returns(close: dict, mes: list[str]) -> dict:
    """{月末dt: {secid: 月收益}}（月末对月末 close；首月无前值 → 缺）。"""
    rets = {}
    for prev, cur in zip(mes, mes[1:]):
        rets[cur] = {s: close[s][cur] / close[s][prev] - 1
                     for s in close if close[s].get(prev) and close[s].get(cur)}
    return rets


def proportional_path(targets: dict, rets: dict, cost_rate: float,
                      rebalance: str = "monthly", calendar: list | None = None) -> dict:
    """无约束比例复利核（独立于 backtest.py，快筛/交叉核对共用）。

    每月末：先按上月末权重 × 本月个股收益漂移（组合收益 r_p = Σ w·r），
    再按 rebalance 语义调仓：
    - monthly：每月强制再平衡到等权 1/n（Qlib topk 标准快筛语义）；
    - target：  仅目标集合变动时调仓（新集合等权；集合不变 → 纯漂移）——
      与精验撮合器「已持仓 skip、集合变动才买卖」语义对齐（交叉核对用）。
    成本 = cost_rate × 单边换手（Σ|Δw| / 2；首月建仓换手 = 1）。
    calendar：净值时间轴（全月末日历）。缺省 = sorted(targets)（targets 键驱动，
    快筛兼容）；交叉核对须传全月末——无信号月沿用旧持仓吃当月收益、缺月不跳点
    （跳点会把缺月收益并入下月，造成与撮合器不可比的时间轴错位）。
    返回 {equity: [月末 nav], turnover: 年化单边换手}。
    """
    if rebalance not in ("monthly", "target"):
        raise ValueError(f"未知再平衡语义：{rebalance}")
    mes = list(calendar) if calendar is not None else sorted(targets)
    # 首点 = 建仓月（无收益键）；后续月须有收益键（缺收益键的月末不进时间轴）
    mes = [mes[0]] + [m for m in mes[1:] if m in rets] if mes else []
    nav, w, equity, turns = 1.0, {}, [], []
    prev_set = None
    for i, m in enumerate(mes):
        tgt = targets.get(m)
        rebal_now = (tgt is not None and
                     (rebalance == "monthly" or prev_set != set(tgt) or not w))
        if rebal_now:
            w_new = {s: 1.0 / len(tgt) for s in tgt}
        else:
            w_new = None                                   # 纯漂移（不调仓）
        r = sum(v * rets[m].get(s, 0.0) for s, v in w.items())
        nav *= 1.0 + r
        w_old = ({s: v * (1.0 + rets[m].get(s, 0.0)) / (1.0 + r) for s, v in w.items()}
                 if i > 0 and abs(1.0 + r) > 1e-12 else dict(w))
        turn = 1.0 if (i == 0 and w_new is not None) else (
            0.0 if w_new is None else
            sum(abs(w_new.get(s, 0.0) - w_old.get(s, 0.0))
                for s in set(w_new) | set(w_old)) / 2)
        if w_new is None:
            w_new = w_old                                  # 漂移后权重归一（和≈1）
        nav *= 1.0 - cost_rate * turn
        w = w_new
        if tgt is not None:
            prev_set = set(tgt)
        equity.append((m, nav))
        turns.append(turn)
    years = max(len(mes) / 12.0, 1e-9)
    return {"equity": equity, "turnover": sum(turns) / years if mes else None}


def run_grid(con: duckdb.DuckDBPyConnection, strategy, metas: dict,
             start: str, end: str, top_ns: tuple = (3, 5),
             cost_rates: tuple = (0.0, 0.001)) -> dict:
    """参数网格快筛：method × top_n × cost_rate → 绩效表（按净 Sharpe 降序）。"""
    comp = composite_scores(con, strategy, metas, start, end)
    all_mes = month_ends(con, start, end)
    mes = [m for m in comp["mes"] if m < all_mes[-1]]
    close = close_map(con, start, end)
    rets = month_returns(close, all_mes)               # 全月末收益（缺月不跳点）
    rows = []
    for method in strategy.methods:
        for top_n in top_ns:
            targets = {t: [s for s, _ in _top_n(comp["scores"][method][t], top_n)]
                       for t in mes if comp["scores"].get(method, {}).get(t)}
            for cr in cost_rates:
                p = proportional_path(targets, rets, cr)
                perf = _fast_perf(p["equity"], p["turnover"], cr)
                rows.append({"method": method, "top_n": top_n, "cost_rate": cr, **perf})
    rows.sort(key=lambda r: -(r["sharpe"] if r["sharpe"] is not None else -9))
    return {"strategy_id": strategy.id, "grid": rows,
            "engine": "vectorbt" if VBT_AVAILABLE else "自研比例复利核（vectorbt 位预留）",
            "n_months": len(mes), "start": start, "end": end}


def _top_n(scores: dict, n: int) -> list[tuple]:
    return sorted(scores.items(), key=lambda kv: -kv[1])[:n]


def _fast_perf(equity: list, turnover: float | None, cr: float) -> dict:
    if len(equity) < 2:
        return {"total_return": None, "ann_return": None, "sharpe": None,
                "max_drawdown": None, "turnover": turnover}
    navs = [v for _, v in equity]
    rs = [navs[i] / navs[i - 1] - 1 for i in range(1, len(navs))]
    mu = sum(rs) / len(rs)
    sd = (sum((x - mu) ** 2 for x in rs) / (len(rs) - 1)) ** 0.5 if len(rs) > 1 else None
    peak, mdd = navs[0], 0.0
    for v in navs:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    years = len(navs) / 12.0
    total = navs[-1] / 1.0 - 1
    return {"total_return": total, "ann_return": (1 + total) ** (1 / years) - 1,
            "sharpe": mu / sd * (12 ** 0.5) if sd and sd > 0 else None,
            "max_drawdown": mdd, "turnover": turnover}


def render_grid_md(rep: dict) -> str:
    L = [f"# M7 参数网格快筛 · {rep['strategy_id']}（{rep['engine']}）", "",
         f"- 窗口：{rep['start']}..{rep['end']}（{rep['n_months']} 个月末）· "
         "无约束近似口径：月末收盘成交 / 单边成本率 × 换手（精验以 backtest.py 表驱动为准）",
         "", "| method | top_n | 单边成本率 | 总收益 | 年化 | Sharpe | 最大回撤 | 年化换手 |",
         "|---|---|---|---|---|---|---|---|"]
    for r in rep["grid"]:
        f = lambda x: "—" if x is None else (f"{x:.2%}" if abs(x) < 10 else f"{x:.3f}")
        s = "—" if r["sharpe"] is None else f"{r['sharpe']:.3f}"   # Sharpe 无量纲，不加 %
        L.append(f"| {r['method']} | {r['top_n']} | {r['cost_rate']:.4f} "
                 f"| {f(r['total_return'])} | {f(r['ann_return'])} | {s} "
                 f"| {f(r['max_drawdown'])} | {f(r['turnover'])} |")
    L += ["", "> 快筛结论仅用于筛选精验候选（top_n × 法排序）；绩效数字不可直接作投资结论"]
    return "\n".join(L) + "\n"


def main() -> None:
    """CLI：python -m factorlib.fastscreen --repo <dir> [--out <dir>] [--top-ns 3,5]"""
    import argparse
    from . import load_definitions
    from .composite import load_strategies
    from .evaluate import build_universe_db
    ap = argparse.ArgumentParser(description="M7 快筛（vectorbt 位）参数网格")
    ap.add_argument("--repo", default="alphapocket/remote/repo")
    ap.add_argument("--out", default=None)
    ap.add_argument("--top-ns", default="3,5")
    args = ap.parse_args()
    con = build_universe_db(args.repo)
    root = Path(__file__).resolve().parent
    metas = {m.id: m for m in load_definitions(root) if m.type == "alpha"}
    from . import materialize
    strategies = load_strategies(root)
    for fid in sorted({f["id"] for st in strategies.values() for f in st.factors}):
        materialize(con, metas[fid], mode="full")
    lo, hi = con.execute("SELECT min(CAST(dt AS VARCHAR)), max(CAST(dt AS VARCHAR))"
                         " FROM dwd_daily_bar").fetchone()
    for sid, st in sorted(strategies.items()):
        rep = run_grid(con, st, metas, lo, hi,
                       top_ns=tuple(int(x) for x in args.top_ns.split(",")))
        print(f"{sid:<22} best: "
              + " ".join(f"{k}={v}" for k, v in rep["grid"][0].items()
                         if k in ("method", "top_n", "cost_rate", "sharpe")))
        if args.out:
            d = Path(args.out)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"fastscreen_{sid}.md").write_text(render_grid_md(rep), encoding="utf-8")
            (d / f"fastscreen_{sid}.json").write_text(
                json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
