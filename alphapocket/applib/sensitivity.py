"""R1 · 策略级参数敏感性扫描（《研究缺口R1-R4实施计划》§3）

口径
----
策略层（新增，独立于 ``backtest.run_backtest`` 的等权锚点）：
  目标权重 = 加权方案（equal / alpha）× 引擎 B 止损缩放（触发→0.6，未触发→1.0，释放部分留现金）
  再平衡 = 按频率回目标权重（换手计费）；与 run_backtest 的漂移/换手/成本口径逐位对齐，
  ``stops=False 且 weighting=equal`` 时退化为 run_backtest（报告内自动做锚点比对）。

扫描参数（§3.1，共 6 类）：
  再平衡频率 / 成本假设 / MA 窗口 win / 确认带宽 band / 波动止损带宽 band_sigma / 五维权重
  （五维权重仅在 alpha 驱动加权下影响绩效，故该 OAT 采用 weighting=alpha 口径）

产物：``sandbox/artifacts/sens_<ts>/{sensitivity.json, sensitivity.md, sensitivity.html}``
纪律：默认参数下逐位复现既有产物；报告定位为「稳健性证据」而非选参工具（禁止从网格挑尖峰档位入产）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import backtest as BT
from . import factors as F

# ── R1 基准参数（§3.1）────────────────────────────────
R1_DEFAULTS = {
    "rebalance": "monthly", "cost_bps": 2.5,
    "win": 20, "band": 0.02, "band_sigma": 1.5,
    "weights": None, "weighting": "equal", "stops": True, "max_weight": 0.40,
}
SPIKE_THRESHOLD = 0.30   # 相邻档绩效差 > 30% → 尖峰（触发评审）
STOP_SCALE = 0.6         # 止损触发后的仓位缩放（与 portfolio.rebalance_plan 的 0.6 一致）

VARIANTS = {
    "rebalance": ["daily", "monthly", "quarterly", "yearly", "buyhold"],
    "cost_bps": [0.0, 2.5, 5.0, 10.0],
    "win": [12, 16, 20, 24, 30],
    "band": [0.010, 0.016, 0.020, 0.024, 0.030],
    "band_sigma": [1.00, 1.20, 1.50, 1.80, 2.25],
}
HEAT_WIN = [12, 16, 20, 24, 30]
HEAT_BAND = [0.010, 0.016, 0.020, 0.024, 0.030]


# ── 引擎 A 五维权重变体 ────────────────────────────────
def _weight_variants(base: dict, delta: float = 0.20) -> list[tuple[str, dict]]:
    """基准 + 逐维 ±20%（其余等比重归一）→ 11 个变体"""
    out = [("baseline", dict(base))]
    for k, v0 in base.items():
        for sign, lab in ((1 - delta, "↓20%"), (1 + delta, "↑20%")):
            w = dict(base)
            w[k] = v0 * sign
            s = sum(w.values())
            out.append((f"{k}{lab}", {kk: v / s for kk, v in w.items()}))
    return out


# ── 引擎 B 止损序列（T-R4 统一：走引擎真源，删除独立公式）────────
def _own_closes(bars_secid: list[dict]) -> list[tuple[str, float]]:
    """该标的自身有效 K 线（与 compute_beta 的 closes 同源：dt/close 且 close 为真）"""
    return [(r["dt"], r["close"]) for r in bars_secid if r.get("close")]


def _beta_series(secid: str, bars_secid: list[dict], n: int, band: float,
                 band_sigma: float) -> dict[str, dict]:
    """{date: {ma_stop, vol_stop, scale}}；样本不足窗口（i < n）→ 不触发（scale=1）。

    T-R4 统一：逐日经引擎 B 真源（engines.BetaEngine.run → factors.compute_beta，
    overrides 注入 win/band/band_sigma）取 ma_stop（state==below）/vol_stop，
    删除与 compute_beta 同式的第三份独立实现。离线研究工具，允许变慢，
    不允许变错（与旧公式逐位对拍见 tests/test_engines_beta_cfg.py）。
    """
    from . import engines as E
    eng = E.get_engine("beta")
    out: dict[str, dict] = {}
    own = _own_closes(bars_secid)
    ovr = {"win": int(n), "band": band, "band_sigma": band_sigma}
    for i, (d, _c) in enumerate(own):
        if i < n:
            out[d] = {"ma_stop": False, "vol_stop": False, "scale": 1.0}
            continue
        r = eng.run(secid, bars_secid, as_of=d, overrides=ovr)
        ma_stop = r["state"] == "below"
        vol_stop = bool(r["vol_stop"])
        out[d] = {"ma_stop": ma_stop, "vol_stop": vol_stop,
                  "scale": STOP_SCALE if (ma_stop or vol_stop) else 1.0}
    return out


def _ffill_scale(bs: dict[str, dict], dates: list[str]) -> dict[str, float]:
    """把标的自身 K 线上的止损缩放前向填充到组合日期轴（停牌/缺 K 线沿用前值）"""
    out, last = {}, 1.0
    for d in dates:
        v = bs.get(d)
        if v is not None:
            last = v["scale"]
        out[d] = last
    return out


# ── 引擎A 真源取数（T-R5 统一：经 AlphaEngine；惰性取，monkeypatch 友好）──
def _alpha_weights() -> dict:
    """引擎A生效五维权重（registry dim_weights 优先，否则内置 DEFAULT_DIM_WEIGHTS）。"""
    from . import engines as E
    return E.get_engine("alpha").weights()


def _alpha_composite(secid: str, bars_secid: list[dict], fd: dict, d: str,
                     weights: dict | None) -> float | None:
    """引擎A综合分（经 AlphaEngine.run → factors.compute_alpha 真源）。

    weights=None → registry dim_weights 生效（未加载 → 内置默认，与旧直调逐位一致）；
    显式 weights dict（OAT 权重变体）→ overrides 覆盖 registry（调试注入同语义）。
    """
    from . import engines as E
    eng = E.get_engine("alpha")
    ovr = {"weights": weights} if weights is not None else None
    return eng.run(secid, bars_secid, fd.get("reports", []), fd.get("dividends", []),
                   as_of=d, overrides=ovr)["composite"]


# ── 规则型阿尔法驱动加权（P2.1 规则版前置）───────────────
def _alpha_targets(scores: dict[str, float | None], cap: float) -> dict[str, float] | None:
    """综合分 → 权重（分差映射 + 单票上限）；有任一 None（无财报）→ 不适用（返回 None）"""
    if any(v is None for v in scores.values()):
        return None
    lo = min(scores.values())
    raw = {s: (v - lo) + 1.0 for s, v in scores.items()}
    tot = sum(raw.values())
    w = {s: r / tot for s, r in raw.items()}
    for _ in range(20):           # 迭代削峰 + 重归一（water-filling）
        over = {s: v for s, v in w.items() if v > cap}
        if not over:
            break
        rest = {s: v for s, v in w.items() if v <= cap}
        rem, rs = 1.0 - cap * len(over), sum(rest.values())
        if not rest or rs <= 0:
            break
        w = {**{s: cap for s in over}, **{s: v / rs * rem for s, v in rest.items()}}
    return w


# ── 策略层回测 ─────────────────────────────────────────
def run_strategy_bt(pf: dict, bars: dict, fund: dict, *,
                    rebalance: str = "monthly", cost_bps: float = 2.5,
                    win: int = 20, band: float = 0.02, band_sigma: float = 1.5,
                    weighting: str = "equal", weights: dict | None = None,
                    stops: bool = True, max_weight: float = 0.40,
                    start: str | None = None, end: str | None = None,
                    _cache: dict | None = None) -> dict:
    secids = [p["secid"] for p in pf["positions"]]
    n = len(secids)
    dates = sorted(set().union(*[{r["dt"] for r in bars[s]} for s in secids]))
    if start:
        dates = [d for d in dates if d >= start]
    if end:
        dates = [d for d in dates if d <= end]
    rmap = {s: {r["dt"]: (r["pct"] if r["pct"] is not None else 0.0) for r in bars[s]} for s in secids}

    cache = _cache if _cache is not None else {}
    # 止损序列（按标的自身 K 线计算 → 前向填充到组合日期轴；结果按参数组合缓存）
    scale: dict[str, dict[str, float]] = {}
    if stops:
        for s in secids:
            key = (s, win, band, band_sigma)
            if key not in cache:
                cache[key] = _beta_series(s, bars[s], int(win), band, band_sigma)
            scale[s] = _ffill_scale(cache[key], dates)

    rebal = BT._rebalance_dates(dates, rebalance)
    commission = cost_bps / 10000.0
    scores_memo: dict[str, float | None] = {}

    def targets(d: str) -> dict[str, float]:
        if weighting == "alpha":
            sc = {}
            for s in secids:
                if (s, d) not in scores_memo:
                    # T-R5 统一：经 AlphaEngine 真源取综合分（registry 权重/锚点/启停生效）
                    scores_memo[(s, d)] = _alpha_composite(s, bars[s], fund.get(s, {}), d, weights)
                sc[s] = scores_memo[(s, d)]
            t = _alpha_targets(sc, max_weight)
            if t:
                return t
        return {s: 1.0 / n for s in secids}

    wf = {s: 1.0 / n for s in secids}
    kk = {s: (scale[s].get(dates[0], 1.0) if stops else 1.0) for s in secids}
    w = {s: wf[s] * kk[s] for s in secids}
    nav, series, turns = 1.0, [], []
    for d in dates:
        r = {s: rmap[s].get(d, 0.0) for s in secids}
        pr = sum(w[s] * r[s] for s in secids)
        nav *= (1 + pr)
        series.append((d, nav))
        for s in secids:                       # 满仓权重随价格漂移
            wf[s] = wf[s] * (1 + r[s]) / (1 + pr)
        w = {s: wf[s] * kk[s] for s in secids}
        if d in rebal:                          # 再平衡 → 回目标权重（计换手成本）
            tgt = targets(d)
            turn = sum(abs(tgt[s] * kk[s] - w[s]) for s in secids)
            if turn:
                cost = turn * commission
                nav *= (1 - cost)
                series[-1] = (d, nav)
                turns.append(turn)
            wf, w = dict(tgt), {s: tgt[s] * kk[s] for s in secids}
        if stops:                               # 止损缩放（仅状态切换时交易）
            turn = 0.0
            for s in secids:
                kd = scale[s].get(d, 1.0)
                if kd != kk[s]:
                    turn += abs(wf[s] * kd - w[s])
                    kk[s], w[s] = kd, wf[s] * kd
            if turn:
                cost = turn * commission
                nav *= (1 - cost)
                series[-1] = (d, nav)
                turns.append(turn)

    rets = [series[0][1] - 1.0] + [series[i][1] / series[i - 1][1] - 1 for i in range(1, len(series))]
    m = BT._metrics(series, rets)
    return {"start": dates[0], "end": dates[-1], "n_days": len(dates),
            "n_trades": len(turns), "turnover": sum(turns),
            "metrics": {k: m[k] for k in ("total_return", "cagr", "ann_vol", "sharpe",
                                          "max_dd", "calmar", "win_rate")},
            "scale_events": (sum(1 for s in secids for v in scale[s].values() if v < 1.0)
                             if stops else 0)}


# ── 扫描 ───────────────────────────────────────────────
def _cell(bt: dict) -> dict:
    m = bt["metrics"]
    return {"cagr": m["cagr"], "sharpe": m["sharpe"], "max_dd": m["max_dd"],
            "ann_vol": m["ann_vol"], "total_return": m["total_return"],
            "turnover": bt["turnover"], "n_trades": bt["n_trades"]}


def _oat(name: str, values: list, run, base: dict, metric: str = "cagr") -> dict:
    rows, prev = [], None
    for v in values:
        bt = run(v)
        c = _cell(bt)
        mv = c[metric]
        rel = (abs(mv - prev) / max(abs(prev), 1e-9)) if (prev is not None and mv is not None) else None
        rows.append({"value": v, **c, "rel_change": rel,
                     "flagged": bool(rel is not None and rel > SPIKE_THRESHOLD)})
        prev = mv
    vals = [r[metric] for r in rows if r[metric] is not None]
    n_flag = sum(1 for r in rows if r["flagged"])
    spread = (max(vals) - min(vals)) / max(abs(max(vals, key=abs)), 1e-9) if vals else None
    verdict = ("存在尖峰（相邻档绩效差 > 30%）→ 触发评审" if n_flag else
               "平坦·稳健（档间极差 < 5%）" if (spread is not None and spread < 0.05) else
               "中等敏感（档间极差 5%–30%）")
    return {"param": name, "metric": metric, "baseline": base, "rows": rows,
            "n_flagged": n_flag, "spread": spread, "verdict": verdict}


def _heat(xs: list, ys: list, run, metric: str) -> dict:
    z = [[run(x, y)[metric] for x in xs] for y in ys]
    flat = [v for row in z for v in row if v is not None]
    return {"x": xs, "y": ys, "metric": metric, "z": z,
            "min": min(flat) if flat else None, "max": max(flat) if flat else None}


def run_scan(pf: dict, bars: dict, fund: dict, start: str | None = None,
             end: str | None = None) -> dict:
    cfg = dict(R1_DEFAULTS)
    has_fund = any(fund.get(p["secid"], {}).get("reports") for p in pf["positions"])
    cache: dict = {}

    def run_bt(**over) -> dict:
        p = {**cfg, **over}
        return run_strategy_bt(pf, bars, fund, rebalance=p["rebalance"], cost_bps=p["cost_bps"],
                               win=p["win"], band=p["band"], band_sigma=p["band_sigma"],
                               weighting=p["weighting"], weights=p["weights"],
                               stops=p["stops"], max_weight=p["max_weight"],
                               start=start, end=end, _cache=cache)

    # ① 锚点比对：stops=False + equal → 必须与 backtest.run_backtest 逐位一致
    anchor_bt = run_bt(stops=False, weighting="equal")
    ref = BT.run_backtest(bars, pf, start=start, end=end, rebalance=cfg["rebalance"],
                          cost_bps=cfg["cost_bps"])
    keys = ("total_return", "cagr", "ann_vol", "sharpe", "max_dd", "calmar")
    diffs = {k: abs(anchor_bt["metrics"][k] - ref["strategy_metrics"][k])
             for k in keys if anchor_bt["metrics"][k] is not None}
    anchor = {"ok": all(d < 1e-12 for d in diffs.values()),
              "max_diff": max(diffs.values()) if diffs else None,
              "detail": "stops=False + 等权 与 backtest.run_backtest 逐位一致" if all(
                  d < 1e-12 for d in diffs.values()) else f"偏差 {diffs}"}

    # ② 引擎 B 状态一致性：快路径 ma_stop ↔ F.compute_beta(win/band)
    dates = sorted(set().union(*[{r["dt"] for r in bars[p["secid"]]} for p in pf["positions"]]))
    if start:
        dates = [d for d in dates if d >= start]
    if end:
        dates = [d for d in dates if d <= end]
    # ② 引擎 B 状态一致性：统一路径 ↔ F.compute_beta 直调（各自 K 线抽样，验「无自相矛盾」）
    mism, n_samp = 0, 0
    for s in [p["secid"] for p in pf["positions"]]:
        key = (s, cfg["win"], cfg["band"], cfg["band_sigma"])
        if key not in cache:                  # 锚点比对（stops=False）未填充 → 此处按默认参数补算
            cache[key] = _beta_series(s, bars[s], cfg["win"], cfg["band"], cfg["band_sigma"])
        bs = cache[key]
        own_dates = list(bs)
        for d in own_dates[::max(1, len(own_dates) // 12)][1:]:
            ref = F.compute_beta(s, bars[s], d, cfg["win"], cfg["band"], cfg["band_sigma"])["state"]
            if (ref == "below") != bs[d]["ma_stop"]:
                mism += 1
            n_samp += 1
    consistency = {"ok": mism == 0, "samples": n_samp, "mismatch": mism}

    # ③ OAT 扫描（§3.1）
    oat = {
        "rebalance": _oat("再平衡频率", VARIANTS["rebalance"], lambda v: run_bt(rebalance=v), cfg["rebalance"]),
        "cost_bps": _oat("成本假设(bps)", VARIANTS["cost_bps"], lambda v: run_bt(cost_bps=v), cfg["cost_bps"]),
        "win": _oat("MA窗口(日)", VARIANTS["win"], lambda v: run_bt(win=v), cfg["win"]),
        "band": _oat("确认带宽", VARIANTS["band"], lambda v: run_bt(band=v), cfg["band"]),
        "band_sigma": _oat("止损带宽(σ)", VARIANTS["band_sigma"], lambda v: run_bt(band_sigma=v), cfg["band_sigma"]),
    }
    if has_fund:
        wvs = _weight_variants(_alpha_weights())     # T-R5：变体基准 = 引擎A生效权重
        wmap = {lab: w for lab, w in wvs}
        oat["weights"] = _oat("五维权重", [lab for lab, _ in wvs],
                              lambda lab: run_bt(weighting="alpha", weights=wmap[lab]),
                              "baseline")
    else:
        oat["weights"] = {"param": "五维权重", "metric": "cagr", "baseline": "n/a", "rows": [],
                          "n_flagged": 0, "spread": None,
                          "verdict": "n/a（持仓无财报 → 五维综合不适用，alpha 加权不可用，权重参数无影响）"}

    # ④ 热力图（§3.2）
    heat = {
        "win_x_band": _heat(HEAT_WIN, HEAT_BAND,
                            lambda wi, ba: _cell(run_bt(win=wi, band=ba)), "sharpe"),
        "rebalance_x_cost": _heat(VARIANTS["rebalance"], VARIANTS["cost_bps"],
                                  lambda rb, cb: _cell(run_bt(rebalance=rb, cost_bps=cb)), "cagr"),
    }

    flagged = [{"param": k, "value": r["value"], "rel_change": r["rel_change"]}
               for k, v in oat.items() for r in v["rows"] if r.get("flagged")]
    base_cell = _cell(run_bt())

    return {
        "portfolio": pf["id"], "portfolio_name": pf["name"],
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "code_version": "R1",
        "start": dates[0], "end": dates[-1], "n_days": len(dates),
        "params_baseline": {k: v for k, v in cfg.items() if k != "weights"},
        "baseline_cell": base_cell,
        "anchor_check": anchor, "beta_state_consistency": consistency,
        "oat": oat, "heatmaps": heat, "flagged": flagged,
        "notes": [
            "策略层口径：目标权重（等权/alpha 驱动）× 引擎B止损缩放（触发→0.6，未触发→1.0，释放部分留现金）；换手计费与 run_backtest 同口径。",
            "stops=False 且 weighting=equal 时与 backtest.run_backtest 逐位一致（见 anchor_check）→ 重构未破坏 V4-α 锚点。",
            "五维权重仅在 alpha 驱动加权下影响绩效，故该 OAT 为 weighting=alpha 口径；无财报持仓（ETF/LOF）标 n/a。",
            "尖峰判定：相邻档绩效（CAGR）相对差 > 30% 触发 flag → 送评审（§3.3）。",
            "报告定位为稳健性证据，禁止从网格挑尖峰档位入产（§8 防过拟合）。",
            f"样本区间 {dates[0]} ~ {dates[-1]}（{len(dates)} 交易日）：机制已验证，统计功效待 2016 起全量历史接入后补强。",
        ],
    }


# ── 渲染 ───────────────────────────────────────────────
def _pct(v, d=1):
    return "—" if v is None else f"{v * 100:.{d}f}%"


def _num(v, d=2):
    return "—" if v is None else f"{v:.{d}f}"


def _val_lab(v) -> str:
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _svg_line(rows: list[dict], key: str, title: str, w: int = 840, h: int = 210) -> str:
    vals = [r[key] for r in rows if r[key] is not None]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.15 or 0.01
    lo, hi = lo - pad, hi + pad
    dense = len(rows) > 6                       # 档位多（如 11 个权重变体）→ 标签旋转，避免相互重叠
    x0, y0, pw = 56, 18, w - 96
    ph = h - y0 - (54 if dense else 30)
    nseg = len(rows) - 1
    pts = []
    for i, r in enumerate(rows):
        if r[key] is None:
            continue
        pts.append((x0 + pw * i / nseg, y0 + ph * (1 - (r[key] - lo) / (hi - lo)), r[key]))
    path = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y, _ in pts)
    dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="#A93226"/>'
                   f'<text x="{x:.1f}" y="{y - 7:.1f}" text-anchor="middle" font-size="8.5" fill="#7A7060">'
                   f'{v:.2f}</text>' for x, y, v in pts)
    xs = ""
    for i in range(len(rows)):
        px, lab = x0 + pw * i / nseg, _val_lab(rows[i]["value"])
        if dense:
            ly = h - 8
            xs += (f'<text x="{px:.1f}" y="{ly}" text-anchor="end" font-size="8" fill="#9C9585" '
                   f'transform="rotate(28 {px:.1f} {ly})">{lab}</text>')
        else:
            xs += (f'<text x="{px:.1f}" y="{h - 8}" text-anchor="'
                   f'{"start" if i == 0 else "end" if i == nseg else "middle"}" '
                   f'font-size="8.5" fill="#9C9585">{lab}</text>')
    grid = "".join(
        f'<line x1="{x0}" y1="{y0 + ph * f:.1f}" x2="{x0 + pw}" y2="{y0 + ph * f:.1f}" '
        f'stroke="#DCD5C6" stroke-dasharray="3 4"/>'
        f'<text x="{x0 - 6}" y="{y0 + ph * f + 3:.1f}" text-anchor="end" font-size="9" fill="#9C9585">'
        f'{hi - (hi - lo) * f:.2f}</text>' for f in (0, 0.5, 1.0))
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="{title}参数敏感度曲线">'
            f'{grid}<path d="{path}" fill="none" stroke="#A93226" stroke-width="1.9"/>{dots}{xs}</svg>')


def _heat_rgb(t: float) -> tuple[int, int, int]:
    lo, mid, hi = (169, 50, 38), (245, 240, 225), (30, 132, 73)
    if t < 0.5:
        k, a, b = t / 0.5, lo, mid
    else:
        k, a, b = (t - 0.5) / 0.5, mid, hi
    return tuple(round(a[i] + (b[i] - a[i]) * k) for i in range(3))


def _lum(rgb: tuple[int, int, int]) -> float:
    def f(c: int) -> float:
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (f(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _text_on(bg: tuple[int, int, int]) -> str:
    """按 WCAG 对比度择优选取格内字色（深字/奶油字），确保任意底色均可读"""
    def cr(fg: tuple[int, int, int]) -> float:
        a, b = _lum(fg) + 0.05, _lum(bg) + 0.05
        return max(a, b) / min(a, b)
    dark, light = (44, 40, 32), (255, 248, 238)
    return "#FFF8EE" if cr(light) >= cr(dark) else "#2C2820"


def _svg_heat(hm: dict, title: str, cw: int = 96, ch: int = 40) -> str:
    xs, ys, z = hm["x"], hm["y"], hm["z"]
    lo, hi = hm["min"], hm["max"]
    w, h = 96 + cw * len(xs), 46 + ch * len(ys)
    cells = ""
    for j, yv in enumerate(ys):
        cells += (f'<text x="86" y="{34 + ch * j + ch / 2 + 3:.0f}" text-anchor="end" font-size="9.5" '
                  f'fill="#6B6353">{_val_lab(yv)}</text>')
        for i, xv in enumerate(xs):
            v = z[j][i]
            t = 0.5 if (lo is None or hi == lo) else (v - lo) / (hi - lo)
            t = max(0.0, min(1.0, t))
            bg = _heat_rgb(t)
            cells += (f'<rect x="{96 + cw * i}" y="{34 + ch * j}" width="{cw}" height="{ch}" '
                      f'fill="rgb({bg[0]},{bg[1]},{bg[2]})" stroke="#fff"/>'
                      f'<text x="{96 + cw * i + cw / 2}" y="{34 + ch * j + ch / 2 + 4:.0f}" '
                      f'text-anchor="middle" font-size="9.5" fill="{_text_on(bg)}">{_num(v)}</text>')
    xt = "".join(f'<text x="{96 + cw * i + cw / 2}" y="26" text-anchor="middle" font-size="9.5" '
                 f'fill="#6B6353">{_val_lab(xs[i])}</text>' for i in range(len(xs)))
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="{title}热力图">'
            f'{cells}{xt}</svg>')


def _oat_md(o: dict) -> list[str]:
    L = [f"### {o['param']}（主指标 {o['metric'].upper()}）", "", o["verdict"], ""]
    if not o["rows"]:
        return L
    L += ["| 档位 | CAGR | 夏普 | 最大回撤 | 累计换手 | 相邻差 | 尖峰 |",
          "| --- | --- | --- | --- | --- | --- | --- |"]
    for r in o["rows"]:
        L.append(f"| {_val_lab(r['value'])} | {_pct(r['cagr'])} | {_num(r['sharpe'])} | {_pct(r['max_dd'])} "
                 f"| {_num(r['turnover'])} | {_pct(r['rel_change']) if r['rel_change'] is not None else '—'} "
                 f"| {'⚠' if r['flagged'] else '—'} |")
    L.append("")
    return L


def render_md(rep: dict) -> str:
    L = [f"# 参数敏感性报告 · {rep['portfolio_name']}", ""]
    L.append(f"区间 `{rep['start']} ~ {rep['end']}`（{rep['n_days']} 交易日）· 生成 `{rep['generated']}` · "
             f"基准口径 `{json.dumps(rep['params_baseline'], ensure_ascii=False)}`")
    L.append("")
    bc = rep["baseline_cell"]
    L.append(f"**基准绩效**：CAGR {_pct(bc['cagr'])} · 夏普 {_num(bc['sharpe'])} · 最大回撤 {_pct(bc['max_dd'])} · "
             f"累计换手 {_num(bc['turnover'])}")
    L.append("")
    a, c = rep["anchor_check"], rep["beta_state_consistency"]
    L.append(f"**完整性自检**：锚点比对 {'✅ 通过' if a['ok'] else '❌ 失败'}"
             f"（{a['detail']}）；引擎B状态一致性 {'✅ 通过' if c['ok'] else '❌ 失败'}"
             f"（{c['samples']} 抽样 · 不一致 {c['mismatch']}）")
    L.append("")
    if rep["flagged"]:
        L.append(f"**⚠ 尖峰参数 {len(rep['flagged'])} 处（相邻档绩效差 > 30%）→ 触发评审**：")
        for f in rep["flagged"]:
            L.append(f"- {f['param']} = `{_val_lab(f['value'])}`（相对差 {_pct(f['rel_change'])}）")
        L.append("")
    else:
        L.append("**无尖峰**：全部参数档间绩效差 < 30%。")
        L.append("")
    L.append("## ① OAT 单参数扫描")
    L.append("")
    for o in rep["oat"].values():
        L += _oat_md(o)
    L.append("## ② 双参数热力图")
    L.append("")
    for key, hm in rep["heatmaps"].items():
        L.append(f"### {key}（指标 {hm['metric'].upper()}）")
        L.append("")
        L.append("| \\ | " + " | ".join(_val_lab(x) for x in hm["x"]) + " |")
        L.append("| --- |" + " --- |" * len(hm["x"]))
        for j, yv in enumerate(hm["y"]):
            L.append(f"| **{_val_lab(yv)}** | " + " | ".join(_num(v) for v in hm["z"][j]) + " |")
        L.append("")
    L.append("## ③ 说明与边界")
    L.append("")
    for n in rep["notes"]:
        L.append(f"- {n}")
    L.append("")
    return "\n".join(L)


_CSS = """body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#FAF7F0;color:#2C2820;
max-width:1000px;margin:0 auto;padding:28px 20px;line-height:1.6}
h1{font-size:22px;border-bottom:2px solid #A93226;padding-bottom:8px}
h2{font-size:16px;margin-top:26px;color:#8C2F24}h3{font-size:13.5px;margin:18px 0 6px}
.meta{font-size:12px;color:#7A7060;margin:6px 0}
.card{background:#fff;border:1px solid #E5DECF;border-radius:10px;padding:14px 16px;margin:10px 0}
table{border-collapse:collapse;width:100%;font-size:12px}
th,td{border:1px solid #E5DECF;padding:5px 8px;text-align:right}
th{background:#F3EEE2;text-align:center}td:first-child,th:first-child{text-align:left}
.mono{font-variant-numeric:tabular-nums}
.up{color:#A93226}.down{color:#1E8449}
.flag{color:#A93226;font-weight:600}.ok{color:#1E8449;font-weight:600}
.warn{background:#FCF3E8;border-left:3px solid #D68910;padding:8px 12px;font-size:12px;border-radius:6px;margin:10px 0}
.note{font-size:11.5px;color:#7A7060;margin-top:6px}
.disc{margin-top:26px;font-size:11px;color:#9C9585;border-top:1px solid #E5DECF;padding-top:12px}"""


def render_html(rep: dict) -> str:
    bc = rep["baseline_cell"]
    a, c = rep["anchor_check"], rep["beta_state_consistency"]
    oat_blocks = ""
    for o in rep["oat"].values():
        if not o["rows"]:
            oat_blocks += f'<h3>{o["param"]}</h3><div class="card">{o["verdict"]}</div>'
            continue
        rows = "".join(
            f'<tr><td class="mono">{_val_lab(r["value"])}</td><td class="mono">{_pct(r["cagr"])}</td>'
            f'<td class="mono">{_num(r["sharpe"])}</td><td class="mono down">{_pct(r["max_dd"])}</td>'
            f'<td class="mono">{_num(r["turnover"])}</td>'
            f'<td class="mono">{_pct(r["rel_change"]) if r["rel_change"] is not None else "—"}</td>'
            f'<td class="mono">{"<span class=flag>⚠</span>" if r["flagged"] else "—"}</td></tr>'
            for r in o["rows"])
        oat_blocks += (f'<h3>{o["param"]} · 主指标 {o["metric"].upper()}</h3>'
                       f'<div class="card"><div class="meta">{o["verdict"]}</div>'
                       f'{_svg_line(o["rows"], o["metric"], o["param"])}'
                       f'<table><tr><th>档位</th><th>CAGR</th><th>夏普</th><th>最大回撤</th>'
                       f'<th>累计换手</th><th>相邻差</th><th>尖峰</th></tr>{rows}</table></div>')
    heat_blocks = ""
    for key, hm in rep["heatmaps"].items():
        heat_blocks += (f'<h3>{key} · {hm["metric"].upper()}</h3>'
                        f'<div class="card">{_svg_heat(hm, key)}'
                        f'<div class="note">纵轴：{key.split("_x_")[1]}；横轴：{key.split("_x_")[0]}；'
                        f'颜色由红（低）到绿（高）映射 {hm["metric"].upper()}</div></div>')
    flags = (f'<div class="warn"><b>⚠ 尖峰参数 {len(rep["flagged"])} 处（相邻档绩效差 > 30%）→ 触发评审</b><br>'
             + "<br>".join(f'{f["param"]} = {_val_lab(f["value"])}（相对差 {_pct(f["rel_change"])}）'
                           for f in rep["flagged"]) + "</div>") if rep["flagged"] else \
            '<div class="card ok">无尖峰：全部参数档间绩效差 &lt; 30%。</div>'
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AlphaPocket · R1 参数敏感性报告 · {rep['portfolio_name']}</title><style>{_CSS}</style></head><body>
<h1>R1 · 参数敏感性报告 — {rep['portfolio_name']}</h1>
<div class="meta">区间 <b>{rep['start']} ~ {rep['end']}</b>（{rep['n_days']} 交易日）· 生成 {rep['generated']}<br>
基准口径 <span class="mono">{json.dumps(rep['params_baseline'], ensure_ascii=False)}</span></div>
<div class="card"><b>基准绩效</b>：CAGR <span class="mono">{_pct(bc['cagr'])}</span> ·
夏普 <span class="mono">{_num(bc['sharpe'])}</span> · 最大回撤 <span class="mono">{_pct(bc['max_dd'])}</span> ·
累计换手 <span class="mono">{_num(bc['turnover'])}</span>
<div class="meta">完整性自检：锚点比对 <span class="{'ok' if a['ok'] else 'flag'}">
{'✅ 通过' if a['ok'] else '❌ 失败'}</span>（{a['detail']}）· 引擎B状态一致性
<span class="{'ok' if c['ok'] else 'flag'}">{'✅ 通过' if c['ok'] else '❌ 失败'}</span>（{c['samples']} 抽样）</div></div>
{flags}
<h2>① OAT 单参数扫描</h2>{oat_blocks}
<h2>② 双参数热力图</h2>{heat_blocks}
<h2>③ 说明与边界</h2><div class="card"><ul style="font-size:12px;margin:0;padding-left:18px">
{''.join(f'<li>{n}</li>' for n in rep['notes'])}</ul></div>
<div class="disc"><b>合规与复现</b>：数据来自公开来源（AKShare 聚合），仅供研究参考，不构成投资建议。
复现：<code>ap sensitivity --portfolio {rep['portfolio']}</code></div></body></html>"""


def write_all(rep: dict, art_dir: Path) -> dict:
    art_dir.mkdir(parents=True, exist_ok=True)
    jp, mp, hp = art_dir / "sensitivity.json", art_dir / "sensitivity.md", art_dir / "sensitivity.html"
    jp.write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    mp.write_text(render_md(rep), encoding="utf-8")
    hp.write_text(render_html(rep), encoding="utf-8")
    return {"json": str(jp), "md": str(mp), "html": str(hp)}
