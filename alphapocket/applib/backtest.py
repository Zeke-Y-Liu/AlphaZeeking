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
from pathlib import Path

COMMISSION = 0.00025   # ETF/LOF 佣金万 2.5（单边，免印花税）；保留以兼容旧引用
DEFAULT_COST_BPS = 2.5  # 默认佣金 2.5bps（万 2.5，与 α 一致）

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
    for y in t:
        t[y]["Y"] -= 1
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


# ── 回测主流程 ──────────────────────────────────────────
def run_backtest(bars: dict, portfolio: dict, start: str | None = None,
                 end: str | None = None, rf: float = 0.0,
                 rebalance: str = "monthly", benchmark_secid: str = "510300.SH",
                 cost_bps: float = DEFAULT_COST_BPS) -> dict:
    if rebalance not in REBALANCE_CHOICES:
        raise ValueError(f"未知再平衡频率：{rebalance}（可选 {REBALANCE_CHOICES}）")
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
    w = {s: 1.0 / n for s in secids}
    nav, series, costs = 1.0, [], []
    for d in dates:
        r = {s: rmap[s].get(d, 0.0) for s in secids}
        pr = sum(w[s] * r[s] for s in secids)
        nav *= (1 + pr)
        series.append((d, nav))
        for s in secids:   # 权重随价格漂移
            w[s] = w[s] * (1 + r[s]) / (1 + pr)
        if d in rebal:     # 再平衡日收盘先漂移再回等权，计提换手成本
            turn = sum(abs(1.0 / n - w[s]) for s in secids)
            cost = turn * commission
            nav *= (1 - cost)
            series[-1] = (d, nav)
            costs.append({"dt": d, "turnover": turn, "cost": cost})
            w = {s: 1.0 / n for s in secids}

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

    # 标的贡献
    port_ret = rets
    per_asset = []
    for s in secids:
        ar = [rmap[s].get(d, 0.0) for d in dates]
        aser, cum = [], 1.0
        for d, r in zip(dates, ar):
            cum *= (1 + r)
            aser.append((d, cum))
        m = _metrics(aser, ar, rf)
        per_asset.append({
            "secid": s, "name": names[s], "weight": 1.0 / n,
            "total_return": m["total_return"], "cagr": m["cagr"], "ann_vol": m["ann_vol"],
            "max_dd": m["max_dd"], "corr": _corr(ar, port_ret),
        })

    mret = _monthly(list(zip(dates, rets)))
    total_cost = (1 - _compound([-c["cost"] for c in costs])) if costs else 0.0
    strategy = (f"等权·买入持有（20%×{n}，期初等权后不再平衡，无再平衡成本）"
                if rebalance == "buyhold"
                else f"等权·{_FREQ_LONG[rebalance]}（20%×{n}，{_FREQ_EXEC[rebalance]}，含换手成本）")
    return {
        "portfolio": portfolio["id"], "portfolio_name": portfolio["name"],
        "strategy": strategy,
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
                 "avg_turnover": sum(c["turnover"] for c in costs) / len(costs) if costs else 0.0},
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
    L.append(f"- 再平衡成本：{bt['cost']['n']} 次 · 平均单边换手 {_pct(bt['cost']['avg_turnover'])}"
             f" · 累计成本拖累 {_pct(bt['cost']['total'], 2)}（{_cost_label(bt)}）\n")
    L.append("## ② 年度收益\n| 年份 | " + " | ".join(f"{m}月" for m in [f"{i:02d}" for i in range(1, 13)]) + " | 全年 |")
    L.append("| --- |" + " --- |" * 13)
    for y in sorted(bt["yearly"]):
        row = bt["yearly"][y]
        cells = [(_pct(row[m]) if m in row else "—") for m in [f"{i:02d}" for i in range(1, 13)]]
        L.append(f"| {y} | " + " | ".join(cells) + f" | **{_pct(row.get('Y'))}** |")
    L.append("")
    L.append("## ③ 标的贡献（同期、等权 20%）\n| 标的 | 区间累计 | 年化收益 | 年化波动 | 最大回撤 | 与组合相关性 |")
    L.append("| --- | --- | --- | --- | --- | --- |")
    for a in bt["per_asset"]:
        L.append(f"| {a['name']} `{a['secid']}` | {_pct(a['total_return'])} | {_pct(a['cagr'])} "
                 f"| {_pct(a['ann_vol'])} | {_pct(a['max_dd'])} | {_num(a['corr'])} |")
    L.append("")
    L.append("## ④ 假设与局限")
    L.append(f"- 数据：AKShare 新浪日线（**不复权价**），ETF/LOF 无复权与分红接口 → 为**价格收益**，低估含息总收益（510300、511260 等分红/付息品种受影响）。")
    L.append(f"- 513100 于 2022-01-14 疑似份额折算（-80.5%），该日 pct 缺失按 0 计；缺失 K 线同样按 0 收益计（{_miss_txt(bt['data_quality']['missing_bars'])}）。")
    L.append(f"- {_FREQ_ASSUME_MD[_freq_of(bt)]}，未计滑点与冲击成本；成本仅含{_cost_label(bt)}（ETF/LOF 免印花税）。")
    L.append("- 名义组合、无税费/申赎约束；不构成投资建议。\n")
    L.append("> 数据来自公开来源（AKShare 聚合新浪行情），仅供研究参考，不构成投资建议。")
    return "\n".join(L)


def render_html(bt: dict, css: str) -> str:
    sm, bm = bt["strategy_metrics"], (bt["benchmark"]["metrics"] if bt["benchmark"] else None)
    bname = bt["benchmark"]["name"] if bt["benchmark"] else "—"

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
    for a in bt["per_asset"]:
        arows += (f'<tr><td><b>{a["name"]}</b><br><span class="mono" style="font-size:9px;color:var(--mute)">{a["secid"]}</span></td>'
                  f'<td class="mono {_cls(a["total_return"])}">{_pct(a["total_return"])}</td>'
                  f'<td class="mono {_cls(a["cagr"])}">{_pct(a["cagr"])}</td><td class="mono">{_pct(a["ann_vol"])}</td>'
                  f'<td class="mono down">{_pct(a["max_dd"])}</td><td class="mono">{_num(a["corr"])}</td></tr>')
    mret = bt["monthly"]
    up_m = sum(1 for v in mret.values() if v > 0) / len(mret) if mret else 0
    best = max(mret.items(), key=lambda kv: kv[1]) if mret else None
    worst = min(mret.items(), key=lambda kv: kv[1]) if mret else None

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
再平衡累计成本拖累 {_pct(bt['cost']['total'], 2)} · 月度胜率 {_pct(up_m)}</div></div>

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
<b>份额折算</b>：513100 于 2022-01-14 疑似份额折算（-80.5%），该日 pct 缺失按 0 计；缺失 K 线同样按 0 收益计（{_miss_txt(bt['data_quality']['missing_bars'])}）。<br>
<b>成交假设</b>：{_FREQ_ASSUME_HTML[_freq_of(bt)]}，未计滑点/冲击成本；成本仅含{_cost_label(bt)}（ETF/LOF 免印花税）。<br>
<b>其他</b>：名义组合，无申赎/税费约束；回测为历史数据分析，不代表未来表现。</div></div></div>

<div class="disc"><b>合规与复现</b>：数据来自公开来源（AKShare 聚合新浪行情），仅供研究参考，不构成投资建议。
口径与结果确定性可复现：<code>python3 ap backtest --portfolio {bt['portfolio']} --start {bt['start']}</code>，
或经 MCP <code>backtest.run</code> 调用。</div>
</body></html>"""


def write_all(bt: dict, art_dir: Path, css: str) -> dict:
    art_dir.mkdir(parents=True, exist_ok=True)
    jp, mp, hp = art_dir / "backtest.json", art_dir / "backtest.md", art_dir / "backtest.html"
    jp.write_text(json.dumps(bt, ensure_ascii=False, indent=2), encoding="utf-8")
    mp.write_text(render_md(bt), encoding="utf-8")
    hp.write_text(render_html(bt, css), encoding="utf-8")
    return {"json": str(jp), "md": str(mp), "html": str(hp)}
