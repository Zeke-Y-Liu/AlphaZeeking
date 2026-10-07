"""P0 · 风险模型最小集（《量化的黑箱》第 4 章落地）

对应《黑箱框架对照与实施计划》第三部分 P0 0.1–0.4：

  0.1 波动率目标制（仓位层）：组合目标年化波动（默认 15%），按「已实现波动倒数」缩放总
      仓位（T-1 信息决定 T 日敞口，无前视）；对照等权买入持有 → 回撤段最大回撤显著下降。
  0.2 等风险加权（头寸层）：以 250 日已实现波动倒数定权重（替代等权），保留单票上限；
      A/B 对照等权 → 夏普不劣化、单票风险贡献方差下降（回测口径见 backtest.weighting=equal_risk）。
  0.3 敞口报告（无意敞口）：行业 / 市值规模 / 波动 三维度暴露，相对「等权中性基准」的偏离
      超过 2σ（历史逐月偏离序列标准差）标红提示。
  0.4 限额规则：单票 ≤ 25% / 单行业 ≤ 40% / 高波动持仓降权 的硬规则，入策略回测口径且可复现。

纪律
----
- 默认参数下不改变既有 V4-α 锚点（等权 + 无限额回测与 backtest.run_backtest 逐位一致）；
- 无前视：所有滚动量仅用 t 时刻及之前数据；波动目标缩放以 lag-1 施加；
- 报告定位「风险规则证据」，非收益优化工具。
产物：``sandbox/artifacts/risk_<ts>/{risk.json, risk.md, risk.html}``
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

from . import backtest as BT

# ── P0 默认风险参数 ─────────────────────────────────────
DEFAULT_RISK = {
    "target_vol": 0.15,        # 组合目标年化波动
    "vol_window": 20,          # 已实现波动窗口（交易日）
    "max_leverage": 1.0,       # 敞口上限（不加杠杆）
    "min_scale": 0.20,         # 敞口下限（极端波动下仍保留 20%）
    "er_window": 250,          # 等风险加权的波动估计窗口
    "er_cap": 0.25,            # 等风险加权单票上限
    "limits": {"max_single": 0.25, "max_industry": 0.40,
               "vol_downweight": True, "vol_threshold": 0.50, "vol_factor": 0.70},
    "exposure_sigma": 2.0,     # 偏离阈值（σ 倍数）
}
TRADING_DAYS = 252


# ── 基础统计 ────────────────────────────────────────────
def _std(xs: list[float], ddof: int = 1) -> float:
    n = len(xs)
    if n <= ddof:
        return 0.0
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - ddof))


def annualized_vol(rets: list[float]) -> float | None:
    if not rets or len(rets) < 2:
        return None
    return _std(rets, 1) * math.sqrt(TRADING_DAYS)


def _rets_from_series(series: list) -> list[float]:
    """由净值序列反推日收益（与 backtest 口径一致：首日 = nav0 − 1）。"""
    navs = [v for _, v in series]
    return [navs[0] - 1.0] + [navs[i] / navs[i - 1] - 1 for i in range(1, len(navs))]


def _max_dd(values: list[float]) -> float:
    peak, mdd = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return mdd


def drawdown_episodes(series: list, top: int = 3) -> list[dict]:
    """识别区间内最深的 N 个回撤段：峰/谷日期与深度、修复日期。

    逐段独立统计：新高即为「上一段修复」并开启新段（trough 重置），避免把历史最深谷
    错配到后续峰值（曾导致 peak 晚于 trough 的非法段）。
    """
    navs = [v for _, v in series]
    dates = [d for d, _ in series]
    eps, peak_i, in_dd, trough_i = [], 0, False, 0
    for i, v in enumerate(navs):
        if v >= navs[peak_i]:                    # 新高
            if in_dd:                            # 回撤修复 → 结算上一段
                eps.append({"peak": dates[peak_i], "trough": dates[trough_i],
                            "recover": dates[i], "depth": navs[trough_i] / navs[peak_i] - 1,
                            "peak_i": peak_i, "trough_i": trough_i})
                in_dd = False
            peak_i = i
        else:                                    # 回撤中
            if not in_dd:
                in_dd, trough_i = True, i        # 新段起点（trough 重置）
            elif v < navs[trough_i]:
                trough_i = i                     # 段内更新最深谷
    if in_dd:                                    # 未修复段
        eps.append({"peak": dates[peak_i], "trough": dates[trough_i], "recover": None,
                    "depth": navs[trough_i] / navs[peak_i] - 1,
                    "peak_i": peak_i, "trough_i": trough_i})
    eps.sort(key=lambda e: e["depth"])
    return eps[:top]


# ── 头寸层：等风险加权 ──────────────────────────────────
def inverse_vol_weights(vols: dict[str, float | None], cap: float | None = None) -> dict[str, float]:
    """波动倒数权重（归一）+ 单票上限 water-filling；缺失/异常波动按截面中位数补。"""
    inv = {s: (1.0 / v if (v and v > 0) else None) for s, v in vols.items()}
    known = [v for v in inv.values() if v is not None]
    if not known:
        n = len(vols) or 1
        return {s: 1.0 / n for s in vols}
    med = sorted(known)[len(known) // 2]
    inv = {s: (v if v is not None else med) for s, v in inv.items()}
    tot = sum(inv.values())
    w = {s: v / tot for s, v in inv.items()}
    if cap:
        for _ in range(50):
            over = {s: v for s, v in w.items() if v > cap + 1e-12}
            if not over:
                break
            rest = {s: v for s, v in w.items() if s not in over}
            rem = 1.0 - cap * len(over)
            rs = sum(rest.values())
            if not rest or rs <= 0:
                break
            w = {**{s: cap for s in over}, **{s: v / rs * rem for s, v in rest.items()}}
    return w


def risk_contrib_var(weights: dict[str, float], vols: dict[str, float | None]) -> float | None:
    """单票风险贡献占比的截面方差（越小越均衡）；无有效波动 → None。"""
    rc = {s: weights[s] * (vols.get(s) or 0.0) for s in weights}
    tot = sum(rc.values())
    if tot <= 0:
        return None
    ps = [v / tot for v in rc.values()]
    m = sum(ps) / len(ps)
    return sum((x - m) ** 2 for x in ps) / len(ps)


# ── 限额规则（P0.4）─────────────────────────────────────
def sector_of(industry: str | None) -> str:
    """行业粗分类：取「·」前主分类（如 汽车零部件·轮胎 → 汽车零部件）。

    口径说明：当前 dim 行业为内部常识标注（非授权申万一级分类），故此处仅做粗聚合；
    接入业内分类库后可无损替换 sector_of，不影响限额/敞口口径。
    """
    if not industry:
        return "未分类"
    return industry.split("·")[0].strip() or "未分类"


def apply_limits(weights: dict[str, float], sectors: dict[str, str],
                 vols: dict[str, float | None], limits: dict) -> tuple[dict[str, float], list[dict]]:
    """硬限额：单票上限 → 单行业上限 → 高波动降权。

    超额部分在同级「未触限」证券间按权重比例再分配；若无可分配对象则留现金（权重和 < 1）。
    返回 (调整后权重, 生效约束清单)。
    """
    w = dict(weights)
    binding: list[dict] = []
    max_single = limits.get("max_single")
    max_ind = limits.get("max_industry")

    if max_single:                                   # 单票上限
        for _ in range(50):
            over = {s: v for s, v in w.items() if v > max_single + 1e-12}
            if not over:
                break
            excess = sum(v - max_single for v in over.values())
            free = {s: v for s, v in w.items() if s not in over}
            fs = sum(free.values())
            w = {s: max_single for s in over}
            if free and fs > 0:
                w.update({s: v + excess * v / fs for s, v in free.items()})
            else:
                w.update(free)
            binding.append({"rule": "max_single", "limit": max_single,
                            "secids": sorted(over), "n": len(over)})

    if max_ind:                                      # 单行业上限
        for _ in range(50):
            ind_w: dict[str, float] = {}
            for s, v in w.items():
                k = sectors.get(s, "未分类")
                ind_w[k] = ind_w.get(k, 0.0) + v
            over = {k: iv for k, iv in ind_w.items() if iv > max_ind + 1e-12}
            if not over:
                break
            excess = sum(iv - max_ind for iv in over.values())
            under = {k: iv for k, iv in ind_w.items() if k not in over}
            us = sum(under.values())
            for s in list(w):
                k = sectors.get(s, "未分类")
                if k in over:
                    w[s] *= max_ind / ind_w[k]
            if under and us > 0:
                for s in list(w):
                    if sectors.get(s, "未分类") not in over:
                        w[s] += excess * w[s] / us
            binding.append({"rule": "max_industry", "limit": max_ind,
                            "industries": sorted(over), "excess": excess})

    if limits.get("vol_downweight"):                 # 高波动降权（不重归一 → 保留现金）
        thr = limits.get("vol_threshold", 0.50)
        fac = limits.get("vol_factor", 0.70)
        hit = [s for s in w if (vols.get(s) or 0) > thr]
        if hit:
            for s in hit:
                w[s] *= fac
            binding.append({"rule": "vol_downweight", "threshold": thr, "factor": fac,
                            "secids": sorted(hit)})
    return w, binding


# ── 波动率目标制（P0.1，仓位层）─────────────────────────
def vol_target_scale(rets: list[float], target_vol: float = 0.15, window: int = 20,
                     max_leverage: float = 1.0, min_scale: float = 0.20) -> list[float]:
    """给出逐日敞口缩放：scale_t = clip(target / 已实现波动_{t-window..t-1}, min, max)。

    以 T-1 及之前收益估计波动 → T 日敞口，无前视；窗口不足 → 1.0（不缩放）。
    """
    out = []
    for i in range(len(rets)):
        if i < window:
            out.append(1.0)
            continue
        rv = annualized_vol(rets[i - window:i])
        if not rv or rv <= 1e-9:
            out.append(max_leverage)
            continue
        out.append(max(min_scale, min(target_vol / rv, max_leverage)))
    return out


def run_vol_target(bars: dict, pf: dict, *, target_vol: float = 0.15, window: int = 20,
                   max_leverage: float = 1.0, min_scale: float = 0.20,
                   rebalance: str = "monthly", cost_bps: float = 2.5,
                   start: str | None = None, end: str | None = None) -> dict:
    """P0.1：在等权基准策略上叠加波动率目标敞口缩放（含敞口换手成本）。"""
    base = BT.run_backtest(bars, pf, start=start, end=end, rebalance=rebalance, cost_bps=cost_bps)
    dates = [d for d, _ in base["series"]]
    rets = _rets_from_series(base["series"])
    scales = vol_target_scale(rets, target_vol, window, max_leverage, min_scale)
    comm = cost_bps / 10000.0
    nav, series, prev = 1.0, [], 1.0
    de_risk = 0
    for d, r, s in zip(dates, rets, scales):
        cost = abs(s - prev) * comm
        nav *= (1 + s * r) * (1 - cost)
        series.append((d, nav))
        prev = s
        if s < 1.0 - 1e-9:
            de_risk += 1
    vt_rets = _rets_from_series(series)
    base_m = base["strategy_metrics"]
    vt_m = BT._metrics(series, vt_rets)
    episodes = drawdown_episodes(base["series"], top=3)
    for e in episodes:
        seg = [v for d, v in series if e["peak"] <= d <= e["trough"]]
        e["vt_max_dd"] = _max_dd(seg) if seg else None
        e["improved"] = (e["vt_max_dd"] is not None and e["vt_max_dd"] > e["depth"])
    ok = bool(episodes) and all(e["improved"] for e in episodes)
    return {
        "target_vol": target_vol, "window": window, "max_leverage": max_leverage,
        "min_scale": min_scale, "rebalance": rebalance, "cost_bps": cost_bps,
        "start": dates[0], "end": dates[-1], "n_days": len(dates),
        "base_metrics": {k: base_m[k] for k in ("total_return", "cagr", "ann_vol", "sharpe", "max_dd")},
        "vt_metrics": {k: vt_m[k] for k in ("total_return", "cagr", "ann_vol", "sharpe", "max_dd")},
        "scale_stats": {"avg": sum(scales) / len(scales), "min": min(scales), "max": max(scales),
                        "de_risk_days": de_risk,
                        "exposure_changes": sum(1 for i in range(1, len(scales))
                                                if abs(scales[i] - scales[i - 1]) > 1e-9)},
        "series": {"base": BT._downsample(base["series"]), "vt": BT._downsample(series)},
        "episodes": episodes,
        "dod": {"ok": ok,
                "detail": ("回撤段最大回撤均较等权买入持有一致下降" if ok
                           else "部分回撤段未见改善（样本/参数复核）")},
    }


# ── 等风险加权 A/B（P0.2）───────────────────────────────
def run_equal_risk(bars: dict, pf: dict, *, window: int = 250, cap: float = 0.25,
                   rebalance: str = "monthly", cost_bps: float = 2.5,
                   start: str | None = None, end: str | None = None) -> dict:
    """P0.2：等风险加权 vs 等权 A/B 回测（风险贡献方差 + 夏普对照）。"""
    eq = BT.run_backtest(bars, pf, start=start, end=end, rebalance=rebalance,
                         cost_bps=cost_bps, weighting="equal", track_risk=True)
    er = BT.run_backtest(bars, pf, start=start, end=end, rebalance=rebalance,
                         cost_bps=cost_bps, weighting="equal_risk", er_window=window,
                         max_weight=cap, track_risk=True)

    def _pick(bt: dict) -> dict:
        m = bt["strategy_metrics"]
        return {"total_return": m["total_return"], "cagr": m["cagr"], "ann_vol": m["ann_vol"],
                "sharpe": m["sharpe"], "max_dd": m["max_dd"],
                "rc_var": bt.get("risk_contrib_var"), "target_weights": bt.get("target_weights")}

    e, r = _pick(eq), _pick(er)
    sharpe_ok = (e["sharpe"] is None or r["sharpe"] is None or r["sharpe"] >= e["sharpe"] - 0.02)
    rc_ok = (e["rc_var"] is not None and r["rc_var"] is not None and r["rc_var"] <= e["rc_var"] + 1e-12)
    return {"window": window, "cap": cap, "rebalance": rebalance, "cost_bps": cost_bps,
            "equal": e, "equal_risk": r,
            "dod": {"sharpe_not_worse": sharpe_ok, "rc_var_lower": rc_ok,
                    "ok": sharpe_ok and rc_ok,
                    "detail": (f"夏普 {e['sharpe']:.2f}→{r['sharpe']:.2f}；"
                               f"风险贡献方差 {e['rc_var']:.4g}→{r['rc_var']:.4g}"
                               if (e["sharpe"] is not None and r["sharpe"] is not None
                                   and e["rc_var"] is not None and r["rc_var"] is not None)
                               else "样本不足，仅登记读数")}}


# ── 敞口报告（P0.3）─────────────────────────────────────
def _vol_map(bars: dict, secids: list[str], as_of: str | None, window: int = 250) -> dict:
    out = {}
    for s in secids:
        closes = [r["close"] for r in bars.get(s, [])
                  if r.get("close") and (not as_of or r["dt"] <= as_of)]
        rs = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))][-window:]
        out[s] = annualized_vol(rs)
    return out


def _mcap_at(series: list, d: str) -> float | None:
    """市值序列 [[dt,val],…] 中 ≤ d 的最近一点（前向取值，避免前视）。"""
    val = None
    for dt_, v in series:
        if dt_ <= d:
            val = v
        else:
            break
    return val


def _exposure_dims(weights: dict[str, float], secids: list[str], *, dims: dict,
                   vols: dict, mcaps: dict, as_of: str) -> dict:
    """三维度暴露读数（当前权重口径）。规模/波动为加权截面读数，行业为分组权重。"""
    n = len(secids)
    neutral = {s: 1.0 / n for s in secids}

    def industry_exp(w):
        g = {}
        for s in secids:
            k = sector_of((dims.get(s) or {}).get("industry"))
            g[k] = g.get(k, 0.0) + w[s]
        return g

    ind_a, ind_n = industry_exp(weights), industry_exp(neutral)

    # 规模：log10(总市值) 截面 z-score → 组合加权倾斜 vs 中性
    logs = {s: math.log10(mcaps[s]) for s in secids if mcaps.get(s)}
    if len(logs) == len(secids) and len(logs) >= 2:
        mu = sum(logs.values()) / len(logs)
        sd = _std(list(logs.values()), 1) or 1e-9
        z = {s: (logs[s] - mu) / sd for s in logs}
        size_a = sum(weights[s] * z[s] for s in secids)
        size_n = sum(neutral[s] * z[s] for s in secids)
        size = {"active": size_a, "neutral": size_n, "dev": size_a - size_n,
                "detail": {s: round(z[s], 3) for s in secids}}
    else:
        size = {"active": None, "neutral": None, "dev": None, "detail": {}}

    # 波动：加权年化波动
    vol_a = sum(weights[s] * (vols.get(s) or 0.0) for s in secids) if all(vols.get(s) for s in secids) else None
    vol_n = (sum((vols.get(s) or 0.0) for s in secids) / n) if all(vols.get(s) for s in secids) else None
    vol = {"active": vol_a, "neutral": vol_n,
           "dev": (vol_a - vol_n) if (vol_a is not None and vol_n is not None) else None,
           "detail": {s: vols.get(s) for s in secids}}

    return {"industry": {"active": ind_a, "neutral": ind_n,
                         "dev": {k: ind_a.get(k, 0.0) - ind_n.get(k, 0.0) for k in set(ind_a) | set(ind_n)}},
            "size": size, "vol": vol, "as_of": as_of}


def exposure_report(bars: dict, pf: dict, dims: dict, market_cap: dict, *,
                    as_of: str | None = None, window: int = 250, cap: float = 0.25,
                    limits: dict | None = None, rebalance: str = "monthly",
                    sigma: float = 2.0, start: str | None = None) -> dict:
    """P0.3：三维度暴露 + 逐月历史偏离序列 → >2σ 标红。"""
    limits = limits or {}
    secids = [p["secid"] for p in pf["positions"]]
    sectors = {s: sector_of((dims.get(s) or {}).get("industry")) for s in secids}
    dates = sorted(set().union(*[{r["dt"] for r in bars[s]} for s in secids]))
    if start:
        dates = [d for d in dates if d >= start]
    as_of = as_of or dates[-1]

    def active_at(d: str) -> dict[str, float]:
        vols = _vol_map(bars, secids, d, window)
        w = inverse_vol_weights(vols, cap)
        w, _ = apply_limits(w, sectors, vols, limits)
        return w

    # 当前
    vols_now = _vol_map(bars, secids, as_of, window)
    mcaps_now = {s: _mcap_at(market_cap.get(s, []), as_of) for s in secids}
    w_now = active_at(as_of)
    current = _exposure_dims(w_now, secids, dims=dims, vols=vols_now, mcaps=mcaps_now, as_of=as_of)
    current["weights"] = {s: round(w_now[s], 4) for s in secids}

    # 历史逐月偏离（再平衡日）→ σ
    rebal = sorted(BT._rebalance_dates(dates, rebalance))
    dev_hist = {"industry": {}, "size": [], "vol": []}
    for d in rebal:
        vols_d = _vol_map(bars, secids, d, window)
        mcaps_d = {s: _mcap_at(market_cap.get(s, []), d) for s in secids}
        w_d = active_at(d)
        ex = _exposure_dims(w_d, secids, dims=dims, vols=vols_d, mcaps=mcaps_d, as_of=d)
        for k, v in ex["industry"]["dev"].items():
            dev_hist["industry"].setdefault(k, []).append(v)
        if ex["size"]["dev"] is not None:
            dev_hist["size"].append(ex["size"]["dev"])
        if ex["vol"]["dev"] is not None:
            dev_hist["vol"].append(ex["vol"]["dev"])

    flags = []
    for k, v in current["industry"]["dev"].items():
        hist = dev_hist["industry"].get(k, [])
        sd = _std(hist, 1) if len(hist) > 2 else None
        z = (v / sd) if (sd and sd > 1e-9) else None
        if z is not None and abs(z) > sigma:
            flags.append({"dim": "industry", "key": k, "dev": v, "sigma": sd, "z": z})
    for dim, key in (("size", "规模倾斜"), ("vol", "波动暴露")):
        hist = dev_hist[dim]
        sd = _std(hist, 1) if len(hist) > 2 else None
        dev = current[dim]["dev"]
        z = (dev / sd) if (dev is not None and sd and sd > 1e-9) else None
        if z is not None and abs(z) > sigma:
            flags.append({"dim": dim, "key": key, "dev": dev, "sigma": sd, "z": z})

    return {"as_of": as_of, "rebalance": rebalance, "sigma": sigma,
            "current": current, "history": {"n_rebalances": len(rebal),
                                            "sigma": {k: (_std(v, 1) if len(v) > 2 else None)
                                                      for k, v in dev_hist["industry"].items()},
                                            "sigma_size": _std(dev_hist["size"], 1) if len(dev_hist["size"]) > 2 else None,
                                            "sigma_vol": _std(dev_hist["vol"], 1) if len(dev_hist["vol"]) > 2 else None},
            "flags": flags,
            "dod": {"ok": True, "detail": f"三维度可输出；偏离 > {sigma}σ 触发 {len(flags)} 项提示"}}


# ── 限额规则回测（P0.4）─────────────────────────────────
def run_limits(bars: dict, pf: dict, *, limits: dict, rebalance: str = "monthly",
               cost_bps: float = 2.5, start: str | None = None, end: str | None = None) -> dict:
    """P0.4：同一基准加/不加限额的回测对照，确认限额生效且可复现。"""
    dims = pf.get("_dims") or {}
    sectors = {p["secid"]: sector_of((dims.get(p["secid"]) or {}).get("industry"))
               for p in pf["positions"]}
    unlim = BT.run_backtest(bars, pf, start=start, end=end, rebalance=rebalance,
                            cost_bps=cost_bps, weighting="equal")
    lim = BT.run_backtest(bars, pf, start=start, end=end, rebalance=rebalance,
                          cost_bps=cost_bps, weighting="equal_risk",
                          er_window=DEFAULT_RISK["er_window"], max_weight=DEFAULT_RISK["er_cap"],
                          risk_limits=limits, sectors=sectors, track_risk=True)
    binding = lim.get("limits_binding") or []
    return {"limits": limits,
            "unlimited": {k: unlim["strategy_metrics"][k] for k in
                          ("total_return", "cagr", "ann_vol", "sharpe", "max_dd")},
            "limited": {k: lim["strategy_metrics"][k] for k in
                        ("total_return", "cagr", "ann_vol", "sharpe", "max_dd")},
            "target_weights": lim.get("target_weights"),
            "limits_binding": binding,
            "n_binding": len(binding),
            "dod": {"ok": len(binding) > 0,
                    "detail": (f"限额在 {len(binding)} 个再平衡时点生效："
                               + "、".join(sorted({b["rule"] for b in binding}))
                               if binding else "样本内未触发限额（权重未越界）")}}


# ── 编排 ────────────────────────────────────────────────
def run_risk_model(bars: dict, pf: dict, dims: dict, market_cap: dict,
                   *, start: str | None = None, end: str | None = None) -> dict:
    """P0 风险模型最小集总装：0.1 波动目标 / 0.2 等风险 / 0.3 敞口 / 0.4 限额。"""
    from . import factors as F  # noqa: F401  (保持与包内其余模块一致的加载顺序)
    cfg = {k: v for k, v in DEFAULT_RISK.items()}
    secids = [p["secid"] for p in pf["positions"]]
    lim_cfg = dict(cfg["limits"])

    p01 = run_vol_target(bars, pf, target_vol=cfg["target_vol"], window=cfg["vol_window"],
                         max_leverage=cfg["max_leverage"], min_scale=cfg["min_scale"],
                         start=start, end=end)
    p02 = run_equal_risk(bars, pf, window=cfg["er_window"], cap=cfg["er_cap"],
                         start=start, end=end)
    p03 = exposure_report(bars, pf, dims, market_cap, window=cfg["er_window"], cap=cfg["er_cap"],
                          limits=lim_cfg, sigma=cfg["exposure_sigma"], start=start,
                          as_of=end)
    pf_lim = {**pf, "_dims": dims}
    p04 = run_limits(bars, pf_lim, limits=lim_cfg, start=start, end=end)

    dod = {"p0_1": p01["dod"]["ok"], "p0_2": p02["dod"]["ok"],
           "p0_3": p03["dod"]["ok"], "p0_4": p04["dod"]["ok"]}
    return {
        "portfolio": pf["id"], "portfolio_name": pf["name"],
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "code_version": "R-P0",
        "start": p01["start"], "end": p01["end"], "n_days": p01["n_days"],
        "config": cfg, "secids": secids,
        "p0_1_vol_target": p01, "p0_2_equal_risk": p02,
        "p0_3_exposure": p03, "p0_4_limits": p04,
        "dod": dod, "dod_all": all(dod.values()),
        "notes": [
            "风险模型对应《量化的黑箱》第 4 章：控制风险规模（波动率目标制）+ 限制风险种类（无意敞口度量）。",
            "波动目标制以 T-1 及之前已实现波动决定 T 日敞口（无前视），敞口变动计换手成本。",
            "等风险加权 = 250 日已实现波动倒数（water-filling 单票上限），替代等权。",
            "敞口三维度：行业（内部粗分类，申万一级待接入业内分类库）/ 市值规模 / 波动；相对等权中性基准偏离 > 2σ 标红。",
            "限额硬规则：单票 ≤ 25% / 单行业 ≤ 40% / 高波动（年化 > 50%）降权至 70%。",
            "报告定位为风险规则证据，非收益优化工具；不构成投资建议。",
        ],
    }


# ── 渲染 ────────────────────────────────────────────────
def _pct(v, d=1):
    return "—" if v is None else f"{v * 100:.{d}f}%"


def _num(v, d=2):
    return "—" if v is None else f"{v:.{d}f}"


_CSS = """body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#FAF7F0;color:#2C2820;
max-width:1000px;margin:0 auto;padding:28px 20px;line-height:1.6}
h1{font-size:22px;border-bottom:2px solid #A93226;padding-bottom:8px}
h2{font-size:16px;margin-top:26px;color:#8C2F24}h3{font-size:13.5px;margin:18px 0 6px}
.meta{font-size:12px;color:#7A7060;margin:6px 0}
.card{background:#fff;border:1px solid #E5DECF;border-radius:10px;padding:14px 16px;margin:10px 0}
table{border-collapse:collapse;width:100%;font-size:12px}
th,td{border:1px solid #E5DECF;padding:5px 8px;text-align:right}
th{background:#F3EEE2;text-align:center}td:first-child,th:first-child{text-align:left}
.mono{font-variant-numeric:tabular-nums}.up{color:#A93226}.down{color:#1E8449}
.flag{color:#A93226;font-weight:600}.ok{color:#1E8449;font-weight:600}
.warn{background:#FCF3E8;border-left:3px solid #D68910;padding:8px 12px;font-size:12px;border-radius:6px;margin:10px 0}
.disc{margin-top:26px;font-size:11px;color:#9C9585;border-top:1px solid #E5DECF;padding-top:12px}"""


def _svg_curve(p01: dict, w: int = 860, h: int = 250) -> str:
    base, vt = p01["series"]["base"], p01["series"]["vt"]
    vals = [v for _, v in base] + [v for _, v in vt]
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.08 or 0.01
    lo, hi = lo - pad, hi + pad
    x0, y0, pw, ph = 46, 16, w - 62, h - 40

    def path(ser):
        n = len(ser) - 1
        return "M" + " L".join(f"{x0 + pw * i / n:.1f},{y0 + ph * (1 - (v - lo) / (hi - lo)):.1f}"
                               for i, (_, v) in enumerate(ser))

    grid = "".join(
        f'<line x1="{x0}" y1="{y0 + ph * f:.1f}" x2="{x0 + pw}" y2="{y0 + ph * f:.1f}" '
        f'stroke="#DCD5C6" stroke-dasharray="3 4"/>'
        f'<text x="{x0 - 6}" y="{y0 + ph * f + 3:.1f}" text-anchor="end" font-size="9" fill="#9C9585">'
        f'{hi - (hi - lo) * f:.2f}</text>' for f in (0, 0.25, 0.5, 0.75, 1.0))
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="波动目标对照净值">'
            f'{grid}'
            f'<path d="{path(base)}" fill="none" stroke="#9C9585" stroke-width="1.4" stroke-dasharray="5 4"/>'
            f'<path d="{path(vt)}" fill="none" stroke="#A93226" stroke-width="1.9"/>'
            f'<text x="{x0}" y="11" font-size="9" fill="#A93226">— 波动目标组合</text>'
            f'<text x="{x0 + 140}" y="11" font-size="9" fill="#9C9585">--- 等权买入持有</text></svg>')


def render_md(rep: dict) -> str:
    L = [f"# P0 风险模型报告 · {rep['portfolio_name']}", ""]
    L.append(f"区间 `{rep['start']} ~ {rep['end']}`（{rep['n_days']} 交易日）· 生成 `{rep['generated']}`")
    L.append("")
    d = rep["dod"]
    L.append("**DoD 自检**：" + " · ".join(
        f"P0.{i+1} {'✅' if d[f'p0_{i+1}'] else '❌'}" for i in range(4)))
    L.append("")
    # 0.1
    p = rep["p0_1_vol_target"]
    b, v = p["base_metrics"], p["vt_metrics"]
    L.append("## ① P0.1 波动率目标制（仓位层）")
    L.append(f"目标年化波动 `{_pct(p['target_vol'])}` · 窗口 {p['window']} 日 · 敞口区间 "
             f"[{p['min_scale']:.2f}, {p['max_leverage']:.2f}]")
    L.append("")
    L.append("| 指标 | 等权买入持有 | 波动目标组合 |")
    L.append("| --- | --- | --- |")
    for lab, k, f in [("累计收益", "total_return", _pct), ("年化收益", "cagr", _pct),
                      ("年化波动", "ann_vol", _pct), ("夏普", "sharpe", _num),
                      ("最大回撤", "max_dd", _pct)]:
        L.append(f"| {lab} | {f(b[k])} | {f(v[k])} |")
    ss = p["scale_stats"]
    L.append("")
    L.append(f"- 敞口统计：均值 {ss['avg']:.2f} · 最低 {ss['min']:.2f} · 减仓日 {ss['de_risk_days']} · 敞口变动 {ss['exposure_changes']} 次")
    L.append("- 回撤段对照（等权基准最深 3 段）：")
    L.append("")
    L.append("| 区间（峰→谷） | 等权最大回撤 | 波动目标同段最大回撤 | 是否改善 |")
    L.append("| --- | --- | --- | --- |")
    for e in p["episodes"]:
        L.append(f"| {e['peak']} → {e['trough']} | {_pct(e['depth'])} | {_pct(e['vt_max_dd'])} "
                 f"| {'✅' if e['improved'] else '—'} |")
    L.append("")
    # 0.2
    p = rep["p0_2_equal_risk"]
    L.append("## ② P0.2 等风险加权（头寸层）")
    L.append("")
    L.append("| 指标 | 等权 | 等风险加权 |")
    L.append("| --- | --- | --- |")
    for lab, k, f in [("累计收益", "total_return", _pct), ("年化收益", "cagr", _pct),
                      ("年化波动", "ann_vol", _pct), ("夏普", "sharpe", _num),
                      ("最大回撤", "max_dd", _pct), ("风险贡献方差", "rc_var", lambda x: f"{x:.4g}" if x is not None else "—")]:
        L.append(f"| {lab} | {f(p['equal'][k])} | {f(p['equal_risk'][k])} |")
    L.append("")
    L.append(f"- 判定：{p['dod']['detail']} → {'✅ 通过' if p['dod']['ok'] else '❌ 未达标'}")
    L.append(f"- 等风险目标权重：`{json.dumps(p['equal_risk']['target_weights'], ensure_ascii=False)}`")
    L.append("")
    # 0.3
    p = rep["p0_3_exposure"]
    cur = p["current"]
    L.append("## ③ P0.3 敞口报告（无意敞口）")
    L.append(f"AS_OF `{cur['as_of']}` · 阈值 {p['sigma']}σ · 风险模型权重 `{json.dumps(cur['weights'], ensure_ascii=False)}`")
    L.append("")
    L.append("**行业暴露**（风险模型权重 vs 等权中性）")
    L.append("")
    L.append("| 行业 | 风险权重 | 等权中性 | 偏离 |")
    L.append("| --- | --- | --- | --- |")
    for k in sorted(cur["industry"]["active"]):
        L.append(f"| {k} | {_pct(cur['industry']['active'][k])} | {_pct(cur['industry']['neutral'].get(k, 0.0))} "
                 f"| {_pct(cur['industry']['dev'].get(k, 0.0))} |")
    L.append("")
    sz, vl = cur["size"], cur["vol"]
    L.append(f"**规模暴露**（log10 总市值截面 z 加权倾斜）：风险 {_num(sz['active'], 3)} vs 中性 {_num(sz['neutral'], 3)}"
             f" → 偏离 {_num(sz['dev'], 3)}")
    L.append("")
    L.append(f"**波动暴露**（加权年化波动）：风险 {_pct(vl['active'])} vs 中性 {_pct(vl['neutral'])}"
             f" → 偏离 {_pct(vl['dev'])}")
    L.append("")
    if p["flags"]:
        L.append(f"**⚠ 偏离 > {p['sigma']}σ 提示 {len(p['flags'])} 项**：")
        for f in p["flags"]:
            L.append(f"- {f['dim']}/{f['key']}：偏离 {_num(f['dev'], 3)}（{f['z']:.1f}σ）")
    else:
        L.append(f"**无超阈值偏离**：三维度当前偏离均在 {p['sigma']}σ 带内。")
    L.append("")
    # 0.4
    p = rep["p0_4_limits"]
    L.append("## ④ P0.4 限额规则")
    L.append(f"规则 `{json.dumps(p['limits'], ensure_ascii=False)}`")
    L.append("")
    L.append("| 指标 | 无限额（等权） | 限额（等风险+硬约束） |")
    L.append("| --- | --- | --- |")
    for lab, k, f in [("累计收益", "total_return", _pct), ("年化波动", "ann_vol", _pct),
                      ("夏普", "sharpe", _num), ("最大回撤", "max_dd", _pct)]:
        L.append(f"| {lab} | {f(p['unlimited'][k])} | {f(p['limited'][k])} |")
    L.append("")
    L.append(f"- 生效时点 {p['n_binding']}：{p['dod']['detail']}")
    L.append(f"- 末次目标权重：`{json.dumps(p['target_weights'], ensure_ascii=False)}`")
    L.append("")
    L.append("## ⑤ 说明与边界")
    L.append("")
    for n in rep["notes"]:
        L.append(f"- {n}")
    L.append("")
    return "\n".join(L)


def render_html(rep: dict) -> str:
    d = rep["dod"]
    p1 = rep["p0_1_vol_target"]
    b, v = p1["base_metrics"], p1["vt_metrics"]
    p2 = rep["p0_2_equal_risk"]
    p3 = rep["p0_3_exposure"]
    cur = p3["current"]
    p4 = rep["p0_4_limits"]

    def tr(lab, f, x, y):
        return f'<tr><td>{lab}</td><td class="mono">{f(x)}</td><td class="mono">{f(y)}</td></tr>'

    m1 = "".join(tr(lab, f, b[k], v[k]) for lab, k, f in
                 [("累计收益", "total_return", _pct), ("年化收益", "cagr", _pct),
                  ("年化波动", "ann_vol", _pct), ("夏普", "sharpe", _num), ("最大回撤", "max_dd", _pct)])
    eps = "".join(f'<tr><td class="mono">{e["peak"]} → {e["trough"]}</td><td class="mono down">{_pct(e["depth"])}</td>'
                  f'<td class="mono">{_pct(e["vt_max_dd"])}</td>'
                  f'<td>{"<span class=ok>✅</span>" if e["improved"] else "—"}</td></tr>' for e in p1["episodes"])
    m2 = "".join(tr(lab, f, p2["equal"][k], p2["equal_risk"][k]) for lab, k, f in
                 [("累计收益", "total_return", _pct), ("年化收益", "cagr", _pct),
                  ("年化波动", "ann_vol", _pct), ("夏普", "sharpe", _num), ("最大回撤", "max_dd", _pct),
                  ("风险贡献方差", "rc_var", lambda x: f"{x:.4g}" if x is not None else "—")])
    ind = "".join(f'<tr><td>{k}</td><td class="mono">{_pct(cur["industry"]["active"][k])}</td>'
                  f'<td class="mono">{_pct(cur["industry"]["neutral"].get(k, 0.0))}</td>'
                  f'<td class="mono">{_pct(cur["industry"]["dev"].get(k, 0.0))}</td></tr>'
                  for k in sorted(cur["industry"]["active"]))
    flags = ("<div class='warn'><b>⚠ 偏离超阈值提示</b><br>"
             + "<br>".join(f'{f["dim"]}/{f["key"]}：偏离 {_num(f["dev"], 3)}（{f["z"]:.1f}σ）' for f in p3["flags"])
             + "</div>") if p3["flags"] else f"<div class='card ok'>三维度偏离均在 {p3['sigma']}σ 带内。</div>"
    m4 = "".join(tr(lab, f, p4["unlimited"][k], p4["limited"][k]) for lab, k, f in
                 [("累计收益", "total_return", _pct), ("年化波动", "ann_vol", _pct),
                  ("夏普", "sharpe", _num), ("最大回撤", "max_dd", _pct)])
    dod_badges = " · ".join(f"P0.{i+1} {'✅' if d[f'p0_{i+1}'] else '❌'}" for i in range(4))
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AlphaPocket · P0 风险模型 · {rep['portfolio_name']}</title><style>{_CSS}</style></head><body>
<h1>P0 风险模型报告 — {rep['portfolio_name']}</h1>
<div class="meta">区间 <b>{rep['start']} ~ {rep['end']}</b>（{rep['n_days']} 交易日）· 生成 {rep['generated']} · DoD {dod_badges}</div>

<h2>① P0.1 波动率目标制<em>Volatility Targeting</em></h2>
<div class="card">{_svg_curve(p1)}
<div class="meta">目标年化波动 {_pct(p1['target_vol'])} · 窗口 {p1['window']} 日 · 敞口 [{p1['min_scale']:.2f}, {p1['max_leverage']:.2f}] · 均值 {p1['scale_stats']['avg']:.2f}</div>
<table><tr><th>指标</th><th>等权买入持有</th><th>波动目标组合</th></tr>{m1}</table>
<h3>回撤段对照</h3><table><tr><th>区间（峰→谷）</th><th>等权最大回撤</th><th>波动目标同段</th><th>改善</th></tr>{eps}</table></div>

<h2>② P0.2 等风险加权<em>Equal Risk Contribution</em></h2>
<div class="card"><table><tr><th>指标</th><th>等权</th><th>等风险加权</th></tr>{m2}</table>
<div class="meta">{p2['dod']['detail']} → {'✅ 通过' if p2['dod']['ok'] else '❌ 未达标'}</div></div>

<h2>③ P0.3 敞口报告<em>Unintended Exposure</em></h2>
<div class="card"><div class="meta">AS_OF {cur['as_of']} · 风险模型权重 <span class="mono">{json.dumps(cur['weights'], ensure_ascii=False)}</span></div>
<table><tr><th>行业</th><th>风险权重</th><th>等权中性</th><th>偏离</th></tr>{ind}</table>
<div class="meta">规模暴露（log10 市值 z 倾斜）：风险 {_num(cur['size']['active'],3)} vs 中性 {_num(cur['size']['neutral'],3)} → 偏离 {_num(cur['size']['dev'],3)}<br>
波动暴露（加权年化）：风险 {_pct(cur['vol']['active'])} vs 中性 {_pct(cur['vol']['neutral'])} → 偏离 {_pct(cur['vol']['dev'])}</div>
{flags}</div>

<h2>④ P0.4 限额规则<em>Hard Limits</em></h2>
<div class="card"><div class="meta">规则 <span class="mono">{json.dumps(p4['limits'], ensure_ascii=False)}</span></div>
<table><tr><th>指标</th><th>无限额（等权）</th><th>限额（等风险+硬约束）</th></tr>{m4}</table>
<div class="meta">生效时点 {p4['n_binding']}：{p4['dod']['detail']}<br>末次目标权重 <span class="mono">{json.dumps(p4['target_weights'], ensure_ascii=False)}</span></div></div>

<h2>⑤ 说明与边界<em>Notes</em></h2>
<div class="card"><ul style="font-size:12px;margin:0;padding-left:18px">
{''.join(f'<li>{n}</li>' for n in rep['notes'])}</ul></div>
<div class="disc"><b>合规与复现</b>：数据来自公开来源（AKShare 聚合），仅供研究参考，不构成投资建议。
复现：<code>ap risk --portfolio {rep['portfolio']}</code></div></body></html>"""


def write_all(rep: dict, art_dir: Path) -> dict:
    art_dir.mkdir(parents=True, exist_ok=True)
    jp, mp, hp = art_dir / "risk.json", art_dir / "risk.md", art_dir / "risk.html"
    jp.write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    mp.write_text(render_md(rep), encoding="utf-8")
    hp.write_text(render_html(rep), encoding="utf-8")
    return {"json": str(jp), "md": str(mp), "html": str(hp)}
