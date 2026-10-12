"""M4 稳健性三关 + 增量价值关 + 多重检验（实施计划 §7，步骤 ④–⑥）。

④ 三关（7.1–7.3）：
- 时间稳健：扩展窗口 walk-forward（train_min_months / step_months 可配，α 阶段 24/6），
  门槛 = 样本外 IC 衰减 < 50% 且无单一年度贡献 > 50%；末端 lockbox 封存只报告不调参。
- 参数稳健：winsorize 分位 ±30% 网格 + 行业中性化开关（轴退化时显式标注），
  门槛 = 方向不反转 且 IC_IR 变化 < 40%。
- 环境稳健：牛熊分段（基准 6 月趋势）；大小盘/成长价值轴在库内截面不足时显式降级标注
  （允许局部失效但可解释：存在 ≥1 个正 IC 环境即 pass，全环境失效才 fail）。

⑤ 增量价值（7.4）：批量因子逐月截面 Spearman 相关矩阵；|ρ̄| ≥ 0.6 → 转「替代版本」对比通道。

⑥ 多重检验（7.5）：Bonferroni + Deflated Sharpe（PSR 口径，逆正态用 Acklam 近似 |ε|<1.2e-9）
  函数就绪；同批候选 < 20 时暂不触发（显式标注 triggered=False）。

诚实口径（CP-0006）：4 股票截面功效弱；行业中性化轴（4 标的 4 行业 → 组内退化为 0）、
大小盘/成长价值轴（组内样本 < 3）在库内均退化为标注项；lockbox 仅报告不调参。
依赖纪律：纯标准库 + duckdb（与 evaluate 同源，不引入 pandas）。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import duckdb

from .evaluate import (DEFAULT_BENCHMARK, build_universe_db, close_map,
                       cross_sections, fwd_returns, month_ends, spearman)

# ── 关卡阈值（实施计划 §7 冻结口径；调整须走版本变更） ──
TIME_GATE_DECAY_MAX = 0.50      # ④a：样本外 IC 衰减上限
YEAR_CONTRIB_MAX = 0.50         # ④a：单一年度贡献上限
PARAM_IR_DRIFT_MAX = 0.40       # ④b：IC_IR 变化上限
PARAM_Q_BASE = 0.01             # ④b：winsorize 基准分位（±30% 网格）
PARAM_GRID_SCALE = (0.7, 1.3)
CORR_THRESHOLD = 0.60           # ⑤：|ρ̄| 注册上限
MULTI_TEST_MIN_BATCH = 20       # ⑥：触发门槛（首批 9 个暂缓）
LOCKBOX_DEFAULT = 6             # 月；末端封存
WF_ALPHA = (24, 6)              # α 阶段 walk-forward：train_min / step（生产 60/12）


# ── ⑥ 统计原语（多重检验，流程就绪） ──
def normal_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


# Acklam 逆正态近似常数：中心段（num 6 项 / den 5 项+1）与尾段（num 6 项 / den 4 项+1）
_PPF_AN = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
           1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
_PPF_AD = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
           6.680131188771972e+01, -1.328068155288572e+01]
_PPF_CN = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
           -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
_PPF_CD = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
           3.754408661907416e+00]


def norm_ppf(p: float) -> float:
    """逆正态分位（Acklam 近似，|ε| < 1.15e-9；stdlib 无 ppf 的自研兜底）。"""
    if not 0 < p < 1:
        raise ValueError(f"p 须 ∈ (0,1)，实得 {p}")
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        num = (((((_PPF_CN[0] * q + _PPF_CN[1]) * q + _PPF_CN[2]) * q + _PPF_CN[3])
                * q + _PPF_CN[4]) * q + _PPF_CN[5])
        den = ((((_PPF_CD[0] * q + _PPF_CD[1]) * q + _PPF_CD[2]) * q + _PPF_CD[3])
               * q + 1)
        return num / den
    if p > phigh:
        return -norm_ppf(1 - p)
    q = p - 0.5
    r = q * q
    num = (((((_PPF_AN[0] * r + _PPF_AN[1]) * r + _PPF_AN[2]) * r + _PPF_AN[3])
            * r + _PPF_AN[4]) * r + _PPF_AN[5]) * q
    den = (((((_PPF_AD[0] * r + _PPF_AD[1]) * r + _PPF_AD[2]) * r + _PPF_AD[3])
            * r + _PPF_AD[4]) * r + 1)
    return num / den


def bonferroni(n_tests: int, alpha0: float = 0.05) -> float:
    """Bonferroni 校正阈值：α/n。"""
    if n_tests < 1:
        raise ValueError(f"n_tests 须 ≥ 1，实得 {n_tests}")
    return alpha0 / n_tests


_EULER_GAMMA = 0.5772156649015329


def deflated_sharpe(sr: float, T: int, n_trials: int, sr_var: float | None = None,
                    skew: float = 0.0, kurt: float = 3.0) -> dict:
    """Deflated Sharpe（PSR 口径，Bailey & López de Prado 2014）。

    SR0 = 无 trial 方差信息时取 0；有跨候选 SR 方差时取期望最大值
    E[max SR] = √sr_var · ((1−γ)·Φ⁻¹(1−1/N) + γ·Φ⁻¹(1−1/(N·e)))。
    PSR = Φ((SR−SR0)·√(T−1) / √(1 − skew·SR + (kurt−1)/4·SR²))；pass = PSR ≥ 0.95。
    月度 IC 场景：SR 代理 = IC_IR。
    """
    if T < 2:
        return {"psr": None, "sr0": None, "pass": None, "note": "T < 2"}
    if sr_var is not None and n_trials >= 1:
        sr0 = (math.sqrt(sr_var)
               * ((1 - _EULER_GAMMA) * norm_ppf(1 - 1 / n_trials)
                  + _EULER_GAMMA * norm_ppf(1 - 1 / (n_trials * math.e))))
    else:
        sr0 = 0.0
    den = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    psr = normal_cdf((sr - sr0) * math.sqrt(T - 1) / math.sqrt(den)) if den > 0 else None
    return {"sr0": sr0, "psr": psr, "pass": (psr is not None and psr >= 0.95),
            "n_trials": n_trials, "sr_var": sr_var}


# ── ④b 参数网格原语 ──
def _quantile(xs: list[float], q: float) -> float:
    """线性插值分位（与 numpy/pandas 默认一致）。xs 须升序。"""
    if not xs:
        raise ValueError("空序列")
    if q <= 0:
        return xs[0]
    if q >= 1:
        return xs[-1]
    pos = q * (len(xs) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def winsorize_cross(vals: dict[str, float], q: float) -> dict[str, float]:
    """截面分位截断（q=0 原样返回；低于 q 分位/高于 1−q 分位截到分位值）。"""
    if q <= 0:
        return dict(vals)
    xs = sorted(vals.values())
    lo, hi = _quantile(xs, q), _quantile(xs, 1 - q)
    return {s: min(max(v, lo), hi) for s, v in vals.items()}


def month_ic(sec_vals: dict[str, float], rets: dict[str, float], direction: int,
             industry: dict[str, str] | None = None, q_win: float = 0.0,
             neutralize: bool = False) -> tuple[float | None, int, bool]:
    """单月对齐 IC = direction · Spearman(因子值, 前瞻收益)。

    返回 (ic_aligned | None, n, degenerate)；中性化开且组内截面退化 → (None, n, True)。
    """
    secids = sorted(set(sec_vals) & set(rets))
    if len(secids) < 3:
        return None, len(secids), False
    vals = winsorize_cross({s: sec_vals[s] for s in secids}, q_win)
    if neutralize and industry:
        groups: dict[str, list[str]] = {}
        for s in secids:
            groups.setdefault(industry.get(s, "__na__"), []).append(s)
        if all(len(g) < 2 for g in groups.values()):
            return None, len(secids), True        # 组内退化：每组 1 只 → demean 后全 0
        means = {g: sum(vals[s] for s in ss) / len(ss) for g, ss in groups.items()}
        s2g = {s: g for g, ss in groups.items() for s in ss}
        vals = {s: vals[s] - means[s2g[s]] for s in secids}
    ic = spearman([vals[s] for s in secids], [rets[s] for s in secids])
    return (None if ic is None else direction * ic), len(secids), False


def ic_series(con: duckdb.DuckDBPyConnection, meta, start: str, end: str,
              horizon: int = 1, xs: dict | None = None, closes: dict | None = None,
              industry: dict[str, str] | None = None, q_win: float = 0.0,
              neutralize: bool = False) -> list[dict]:
    """逐月对齐 IC 时序 [{dt, ic, n, degenerate}]（h 月前瞻；缺样本月诚实跳过）。"""
    if xs is None:
        xs = cross_sections(con, meta.id, start, end)
    if closes is None:
        closes = close_map(con, start, end)
    mes = month_ends(con, start, end)
    out = []
    for i, t in enumerate(mes):
        j = i + horizon
        if j >= len(mes):
            break
        sec_vals = xs.get(t, {})
        if not sec_vals:
            continue
        rets = fwd_returns(closes, t, mes[j], list(sec_vals))
        ic, n, degen = month_ic(sec_vals, rets, meta.direction or 1,
                                industry=industry, q_win=q_win, neutralize=neutralize)
        out.append({"dt": t, "ic": ic, "n": n, "degenerate": degen})
    return out


def _agg_ics(ics: list[dict]) -> dict:
    """IC 时序聚合（非退化月）：mean / ir / n；全退化 → 全 None。"""
    vals = [r["ic"] for r in ics if r["ic"] is not None]
    if not vals:
        return {"mean": None, "ir": None, "n": 0}
    m = sum(vals) / len(vals)
    ir = None
    if len(vals) > 1:
        ss = sum((v - m) ** 2 for v in vals)
        if ss > 0:
            ir = m / math.sqrt(ss / (len(vals) - 1))
    return {"mean": m, "ir": ir, "n": len(vals)}


# ── ④a 时间稳健：walk-forward + 年度贡献 + lockbox ──
def walk_forward(ics: list[dict], train_min: int = 24, step: int = 6) -> dict:
    """扩展窗口滚动验证（expanding train，fixed step valid）。

    fold k：train = 前 train_min + k·step 个月，valid = 随后 step 个月；
    fold 衰减 = 1 − mean(valid)/mean(train)（train 均值非正 → 无意义，记 None）。
    总衰减 = 1 − 池化 mean(全部 valid 月) / mean(最末 train 窗)——OOS 池化不重复计数。
    """
    if train_min < 1 or step < 1:
        raise ValueError(f"train_min/step 须 ≥ 1，实得 {train_min}/{step}")
    folds, all_valid, te_last = [], [], None
    k = 0
    while True:
        te = train_min + k * step
        if te >= len(ics):
            break
        tr, va = ics[:te], ics[te:te + step]
        if not va:
            break
        a_tr, a_va = _agg_ics(tr), _agg_ics(va)
        decay = (1 - a_va["mean"] / a_tr["mean"]
                 if (a_tr["mean"] and a_va["mean"] is not None) else None)
        folds.append({"k": k, "train_end": tr[-1]["dt"], "n_train": a_tr["n"],
                      "train_ic": a_tr["mean"], "valid_dts": [r["dt"] for r in va],
                      "n_valid": a_va["n"], "valid_ic": a_va["mean"], "decay": decay})
        all_valid += [r["ic"] for r in va if r["ic"] is not None]
        te_last = te
        k += 1
    fin_tr = _agg_ics(ics[:te_last]) if te_last else {"mean": None, "n": 0}
    m_va = (sum(all_valid) / len(all_valid)) if all_valid else None
    pooled = (1 - m_va / fin_tr["mean"]
              if (fin_tr["mean"] and m_va is not None) else None)
    return {"folds": folds, "train_ic_mean": fin_tr["mean"], "valid_ic_mean": m_va,
            "decay_pooled": pooled, "n_folds": len(folds)}


def yearly_contribution(ics: list[dict]) -> dict:
    """分年度贡献：share = ΣIC_year / ΣIC_total（总 IC ≤ 0 → share 无意义记 None）。"""
    by_year: dict[str, list[float]] = {}
    for r in ics:
        if r["ic"] is not None:
            by_year.setdefault(r["dt"][:4], []).append(r["ic"])
    total = sum(v for vs in by_year.values() for v in vs)
    rows = {}
    for y in sorted(by_year):
        vs = by_year[y]
        rows[y] = {"sum": sum(vs), "mean": sum(vs) / len(vs), "n": len(vs),
                   "share": (sum(vs) / total) if total > 0 else None}
    max_share = max((r["share"] for r in rows.values() if r["share"] is not None),
                    default=None)
    return {"years": rows, "total_sum": total, "max_share": max_share}


def temporal_gate(ics: list[dict], train_min: int = 24, step: int = 6,
                  lockbox_months: int = LOCKBOX_DEFAULT) -> dict:
    """时间稳健关（④a）：主窗（去 lockbox）walk-forward + 年度贡献；lockbox 只报告。"""
    main = ics[:-lockbox_months] if lockbox_months and len(ics) > lockbox_months else ics
    lb = ics[len(main):] if lockbox_months and len(ics) > lockbox_months else []
    wf = walk_forward(main, train_min=train_min, step=step)
    yc = yearly_contribution(main)
    decay_ok = wf["decay_pooled"] is not None and wf["decay_pooled"] < TIME_GATE_DECAY_MAX
    if wf["train_ic_mean"] is not None and wf["train_ic_mean"] <= 0:
        time_fail_reason = "样本内对齐 IC 非正，衰减无意义"
    elif wf["decay_pooled"] is None:
        time_fail_reason = "有效 fold 不足，无法计算衰减"
    else:
        time_fail_reason = None if decay_ok else (
            f"样本外 IC 衰减 {wf['decay_pooled']:.1%} ≥ {TIME_GATE_DECAY_MAX:.0%}")
    if yc["max_share"] is not None and yc["max_share"] > YEAR_CONTRIB_MAX:
        bad_year = max((y for y, r in yc["years"].items()
                        if r["share"] is not None), key=lambda y: yc["years"][y]["share"])
        time_fail_reason = (time_fail_reason or
                            f"单一年度（{bad_year}）贡献 {yc['max_share']:.1%} > 50%")
    lb_agg = _agg_ics(lb)
    return {"pass": time_fail_reason is None,
            "reason": time_fail_reason or "通过",
            "walk_forward": wf, "yearly": yc,
            "lockbox": {"n": lb_agg["n"], "mean_ic": lb_agg["mean"],
                        "note": "末端封存：不参与门槛，仅报告（α 阶段观察值）"}}


# ── ④b 参数稳健：winsorize ±30% × 中性化开关 ──
def param_gate(con: duckdb.DuckDBPyConnection, meta, start: str, end: str,
               industry: dict[str, str] | None = None, horizon: int = 1,
               q_base: float = PARAM_Q_BASE,
               grid: tuple[float, float] = PARAM_GRID_SCALE,
               lockbox_months: int = LOCKBOX_DEFAULT) -> dict:
    """参数稳健关（④b）：winsorize ±30% 网格 → 方向不反转 + IC_IR 漂移 < 40%。

    中性化开关属实质性口径变更（改变因子定义而非参数扰动）→ 不参与强度关，
    仅记符号/强度对照标注；组内退化（每组 1 只）→ degenerate 显式标注。
    """
    xs = cross_sections(con, meta.id, start, end)
    closes = close_map(con, start, end)
    cells = []
    for q in sorted({round(q_base * grid[0], 6), q_base, round(q_base * grid[1], 6)}):
        for neu in (False, True):
            ser = ic_series(con, meta, start, end, horizon=horizon, xs=xs, closes=closes,
                            industry=industry, q_win=q, neutralize=neu)
            ser_main = ser[:-lockbox_months] if lockbox_months and len(ser) > lockbox_months else ser
            a = _agg_ics(ser_main)
            n_degen = sum(1 for r in ser_main if r["degenerate"])
            cells.append({"q": q, "neutralize": neu, "mean_ic": a["mean"], "ic_ir": a["ir"],
                          "n": a["n"], "n_degenerate": n_degen})
    # 门槛作用域 = winsorize 轴（neutralize=False）；中性化格仅标注
    gate_cells = [c for c in cells if not c["neutralize"] and c["mean_ic"] is not None]
    neu_cells = [c for c in cells if c["neutralize"]]
    base = next((c for c in gate_cells if c["q"] == q_base),
                gate_cells[0] if gate_cells else None)
    sign_flips = []
    if base and base["mean_ic"]:
        for c in gate_cells:
            if c["mean_ic"] and (c["mean_ic"] > 0) != (base["mean_ic"] > 0):
                sign_flips.append(c)
    ir_drift = None
    if base and base["ic_ir"]:
        drifts = [abs(c["ic_ir"] - base["ic_ir"]) / abs(base["ic_ir"])
                  for c in gate_cells if c["ic_ir"] is not None]
        ir_drift = max(drifts) if drifts else None
    degen_axes = [c for c in cells if c["n_degenerate"] and c["n_degenerate"] == c["n"]]
    reasons, notes = [], []
    if not gate_cells:
        reasons.append("网格全退化，无法判定")
    else:
        if sign_flips:
            reasons.append(f"{len(sign_flips)} 格方向反转")
        if base["ic_ir"] is None and base["mean_ic"] is not None:
            notes.append("基准格 IC 恒定（IC_IR 不可估）→ 强度关空转，仅方向关生效")
        elif base["ic_ir"] is not None:
            if ir_drift is None:
                reasons.append("网格有效格均无 IR 可比")
            elif ir_drift >= PARAM_IR_DRIFT_MAX:
                reasons.append(f"IC_IR 漂移 {ir_drift:.0%} ≥ {PARAM_IR_DRIFT_MAX:.0%}")
    return {"pass": not reasons, "reason": "；".join(reasons) or "通过",
            "cells": cells, "base": base["q"], "ir_drift_max": ir_drift,
            "degenerate_axes": degen_axes,
            "notes": notes + (["中性化轴组内退化（行业独占样本）→ 显式标注，不参与门槛"]
                              if degen_axes else [])}


# ── ④c 环境稳健：牛熊分段（大小盘/成长价值轴按数据支持度自动降级） ──
def regime_gate(con: duckdb.DuckDBPyConnection, meta, start: str, end: str,
                benchmark: str | None = DEFAULT_BENCHMARK, horizon: int = 1,
                trend_months: int = 6, min_bucket_n: int = 3,
                lockbox_months: int = LOCKBOX_DEFAULT) -> dict:
    """环境稳健关（④c）：牛熊分段必算；大小盘/成长价值轴数据不足 → 降级标注。

    失效环境 = 该环境月均对齐 IC ≤ 0；pass = 至少一个环境正 IC（局部失效可解释），
    全环境失效 → fail。基准趋势标签 = 月末 close vs 6 个月前 close。
    """
    xs = cross_sections(con, meta.id, start, end)
    closes = close_map(con, start, end)
    mes = month_ends(con, start, end)
    ser = ic_series(con, meta, start, end, horizon=horizon, xs=xs, closes=closes)
    ser_main = ser[:-lockbox_months] if lockbox_months and len(ser) > lockbox_months else ser
    dt2ic = {r["dt"]: r["ic"] for r in ser_main}
    axes: dict = {}
    # 轴 1：牛熊（基准趋势）
    if benchmark and benchmark in closes:
        regimes = {}
        for i, t in enumerate(mes):
            if i < trend_months or t not in dt2ic:
                continue
            t0 = mes[i - trend_months]
            c0, c1 = closes[benchmark].get(t0), closes[benchmark].get(t)
            if not (c0 and c1):
                continue
            regimes.setdefault("bull" if c1 >= c0 else "bear", []).append(dt2ic[t])
        axes["bull_bear"] = {rg: {"mean_ic": (sum(v) / len(v)) if v else None, "n": len(v)}
                             for rg, v in sorted(regimes.items())}
    else:
        axes["bull_bear"] = {"status": "degraded", "reason": f"库内无基准 {benchmark}"}
    # 轴 2/3：大小盘（市值中位数分组）/ 成长价值——支持度检查，不足或未实现 → 显式降级
    mcap_n = con.execute("""SELECT count(DISTINCT secid) FROM dwd_mktcap_daily
                            WHERE dt >= CAST(? AS DATE) AND dt <= CAST(? AS DATE)""",
                         [start, end]).fetchone()[0]
    need = min_bucket_n * 2
    axes["size"] = {"status": "degraded",
                    "reason": (f"分组截面不足（需 ≥ {need} 标的，实得 {mcap_n}）→ 待全量历史补强"
                               if mcap_n < need else
                               "截面已足但分组 IC 实现位待全量数据接入（α 阶段未触发）")}
    axes["value_growth"] = {"status": "degraded",
                            "reason": "无成长/价值分组维度数据 → 待数据底座补强"}
    # 失效环境标签
    tags = []
    means = []
    bb = axes["bull_bear"]
    for rg, st in (bb.items() if isinstance(bb, dict) and "status" not in bb else []):
        if st["mean_ic"] is not None:
            means.append(st["mean_ic"])
            if st["mean_ic"] <= 0:
                tags.append(f"bull_bear/{rg} 失效（IC {st['mean_ic']:.3f}，{st['n']} 月）")
    ok_env = any(m > 0 for m in means)
    if not means:
        reason, gate_pass = "无有效环境样本", False
    elif not ok_env:
        reason, gate_pass = f"全部环境失效（{len(means)} 个环境 IC 均 ≤ 0）", False
    else:
        reason, gate_pass = ("；".join(tags) or "通过", True)
    return {"pass": gate_pass, "reason": reason, "axes": axes,
            "failure_tags": tags, "n_degenerate_axes":
                sum(1 for a in axes.values() if isinstance(a, dict) and a.get("status") == "degraded")}


# ── ⑤ 增量价值：批量因子截面相关矩阵 ──
def corr_matrix(con: duckdb.DuckDBPyConnection, factor_ids: list[str],
                start: str, end: str) -> dict:
    """逐月截面 Spearman 相关矩阵（因子 × 因子；截断面 = 双因子有值交集，≥3 才计）。

    限定月末截面（§7 协议口径）：长表为日频，财报类因子月内恒定——若不限定，
    日内重复样本会使跨族 ρ̄ 虚高（1386 个日频截面 vs 69 个月末截面）。
    """
    mes = set(month_ends(con, start, end))
    xs = {fid: cross_sections(con, fid, start, end) for fid in factor_ids}
    pairs = {}
    for i, fa in enumerate(factor_ids):
        for fb in factor_ids[i + 1:]:
            rhos = []
            dts = sorted((set(xs[fa]) & set(xs[fb])) & mes)
            for dt in dts:
                common = sorted(set(xs[fa][dt]) & set(xs[fb][dt]))
                if len(common) < 3:
                    continue
                r = spearman([xs[fa][dt][s] for s in common],
                             [xs[fb][dt][s] for s in common])
                if r is not None:
                    rhos.append(r)
            pairs[f"{fa}|{fb}"] = {
                "mean_rho": sum(rhos) / len(rhos) if rhos else None,
                "max_abs": max((abs(r) for r in rhos), default=None),
                "n_months": len(rhos)}
    return {"factors": factor_ids, "pairs": pairs}


def incremental_gate(fid: str, corr: dict, threshold: float = CORR_THRESHOLD) -> dict:
    """增量价值关（⑤）：与批量内其余因子 |ρ̄| 均 < 0.6 → pass；否则转「替代版本」通道。"""
    worst, worst_partner = None, None
    for key, st in corr["pairs"].items():
        if fid not in key.split("|") or st["mean_rho"] is None:
            continue
        if worst is None or abs(st["mean_rho"]) > worst:
            worst, worst_partner = abs(st["mean_rho"]), key.replace("|", " vs ")
    if worst is None:
        return {"pass": True, "route": "register", "max_abs_rho": None,
                "reason": "批量内无可比因子（单因子批次）"}
    if worst >= threshold:
        return {"pass": False, "route": "alternative",
                "max_abs_rho": worst, "partner": worst_partner,
                "reason": f"与 {worst_partner} |ρ̄|={worst:.2f} ≥ {threshold} → 转「替代版本」对比通道"}
    return {"pass": True, "route": "register", "max_abs_rho": worst,
            "reason": f"最大 |ρ̄|={worst:.2f}（{worst_partner}）< {threshold}"}


# ── ⑥ 多重检验 ──
def multi_test(candidates: list[dict], min_batch: int = MULTI_TEST_MIN_BATCH) -> dict:
    """⑥：同批 ≥ min_batch 才触发；candidates = [{id, ic_ir, n_months}]。

    Bonferroni：p_i = 2(1−Φ(|t_i|))（NW t，正态近似），p < α/n 才显著；
    Deflated Sharpe：SR 代理 = IC_IR，跨候选 SR 方差估 E[max SR] 基准。
    """
    n = len(candidates)
    if n < min_batch:
        return {"triggered": False, "n": n, "min_batch": min_batch,
                "note": f"同批 {n} < {min_batch} 候选 → 暂缓（流程就绪）"}
    sr_var = None
    srs = [c.get("ic_ir") for c in candidates if c.get("ic_ir") is not None]
    if len(srs) >= 2:
        m = sum(srs) / len(srs)
        ss = sum((s - m) ** 2 for s in srs)
        if ss > 0:
            sr_var = ss / (len(srs) - 1)
    rows = []
    for c in candidates:
        rows.append({"id": c["id"], "ic_ir": c.get("ic_ir"),
                     "dsr": deflated_sharpe(c.get("ic_ir") or 0.0, c.get("n_months") or 0,
                                            n_trials=n, sr_var=sr_var)})
    return {"triggered": True, "n": n, "min_batch": min_batch,
            "bonferroni_alpha": bonferroni(n), "sr_var": sr_var, "rows": rows}


# ── 编排：一条命令出三关报告 ──
def _industry_map(con: duckdb.DuckDBPyConnection) -> dict[str, str]:
    try:
        rows = con.execute("SELECT secid, industry FROM dim_security"
                           " WHERE industry IS NOT NULL").fetchall()
        return dict(rows)
    except duckdb.CatalogException:
        return {}


def run_governance(repo_dir: str | Path, factor_ids: list[str] | None = None,
                   start: str | None = None, end: str | None = None,
                   out_dir: str | Path | None = None,
                   wf: tuple[int, int] = WF_ALPHA,
                   lockbox_months: int = LOCKBOX_DEFAULT,
                   corr_threshold: float = CORR_THRESHOLD,
                   benchmark: str | None = DEFAULT_BENCHMARK) -> dict:
    """M4 一条命令：repo JSON → 内存库 → ④ 三关 + ⑤ 增量 + ⑥ 多重检验 → MD/JSON 落盘。"""
    from . import load_definitions
    con = build_universe_db(repo_dir)
    root = Path(__file__).resolve().parent
    metas = {m.id: m for m in load_definitions(root) if m.type == "alpha"}
    return run_governance_on_con(con, metas, factor_ids=factor_ids, start=start, end=end,
                                 out_dir=out_dir, wf=wf, lockbox_months=lockbox_months,
                                 corr_threshold=corr_threshold, benchmark=benchmark,
                                 materialize_values=True)


def run_governance_on_con(con: duckdb.DuckDBPyConnection, metas: dict,
                          factor_ids: list[str] | None = None,
                          start: str | None = None, end: str | None = None,
                          out_dir: str | Path | None = None,
                          wf: tuple[int, int] = WF_ALPHA,
                          lockbox_months: int = LOCKBOX_DEFAULT,
                          corr_threshold: float = CORR_THRESHOLD,
                          benchmark: str | None = DEFAULT_BENCHMARK,
                          materialize_values: bool = True) -> dict:
    """治理核心（con 已备好 dwd 表与 factor_value_daily；metas = {id: FactorMeta}）。

    materialize_values=False → 跳过物化（合成库测试注入路径：值已在长表中）。
    """
    from . import materialize
    from .pit_audit import audit as pit_audit
    if factor_ids is None:
        factor_ids = sorted(metas)
    missing = [f for f in factor_ids if f not in metas]
    if missing:
        raise ValueError(f"因子未注册或非 alpha：{missing}")
    if start is None or end is None:
        lo, hi = con.execute("SELECT min(CAST(dt AS VARCHAR)), max(CAST(dt AS VARCHAR))"
                             " FROM dwd_daily_bar").fetchone()
        start, end = start or lo, end or hi
    industry = _industry_map(con)

    pit_rows, factor_data = {}, {}
    for fid in factor_ids:
        meta = metas[fid]
        if materialize_values:
            materialize(con, meta, mode="full")
        pit_rows[fid] = pit_audit(con, factor_id=fid)
        xs = cross_sections(con, fid, start, end)
        closes = close_map(con, start, end)
        ics = ic_series(con, meta, start, end, horizon=1, xs=xs, closes=closes)
        main = ics[:-lockbox_months] if lockbox_months and len(ics) > lockbox_months else ics
        factor_data[fid] = {"meta": meta, "xs": xs, "closes": closes,
                            "ics": ics, "main_ic": _agg_ics(main)}

    corr = corr_matrix(con, factor_ids, start, end)
    # ⑤ 高相关组裁决（union-find）：|ρ̄| ≥ 阈值的连通组内保留主窗 |IC| 最大者
    # （并列看 |IC_IR|，再并列按 id 序），其余转「替代版本」通道——组内只留一个独立名额。
    parent = {f: f for f in factor_ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for key, st in corr["pairs"].items():
        if st["mean_rho"] is not None and abs(st["mean_rho"]) >= corr_threshold:
            a, b = key.split("|")
            parent[find(b)] = find(a)
    groups: dict[str, list[str]] = {}
    for f in factor_ids:
        groups.setdefault(find(f), []).append(f)

    verdicts = []
    for fid in factor_ids:
        d = factor_data[fid]
        tg = temporal_gate(d["ics"], train_min=wf[0], step=wf[1],
                           lockbox_months=lockbox_months)
        pg = param_gate(con, d["meta"], start, end, industry=industry,
                        lockbox_months=lockbox_months)
        rg = regime_gate(con, d["meta"], start, end, benchmark=benchmark,
                         lockbox_months=lockbox_months)
        ig = incremental_gate(fid, corr, threshold=corr_threshold)
        grp = groups[find(fid)]
        if len(grp) > 1:
            quality = {f: (-abs(factor_data[f]["main_ic"]["mean"] or 0),
                           -abs(factor_data[f]["main_ic"]["ir"] or 0), f) for f in grp}
            keep = sorted(grp, key=lambda f: quality[f])[0]
            if fid != keep:
                ig = {**ig, "pass": False, "route": "alternative",
                      "reason": (f"高相关组 {sorted(grp)} 内保留 {keep} → "
                                 f"{fid} 转「替代版本」对比通道")}
            else:
                ig = {**ig, "pass": True, "route": "register",
                      "reason": f"高相关组（{len(grp)} 成员）保留代表：主窗 IC 最大"}
        pit = pit_rows[fid]
        pit_ok = pit["violations"] == 0
        gates_pass = tg["pass"] and pg["pass"] and rg["pass"] and ig["pass"] and pit_ok
        overall = ("pass" if gates_pass
                   else ("alternative" if (not ig["pass"] and tg["pass"] and pg["pass"]
                                           and rg["pass"] and pit_ok)
                         else "fail"))
        verdicts.append({
            "factor_id": fid, "name": d["meta"].name, "direction": d["meta"].direction,
            "time_gate": tg, "param_gate": pg, "regime_gate": rg, "incremental": ig,
            "pit": {"checked": pit["checked"], "violations": pit["violations"]},
            "overall": overall, "main_ic": d["main_ic"],
        })
    mt = multi_test([{"id": v["factor_id"], "ic_ir": v["main_ic"]["ir"],
                      "n_months": v["main_ic"]["n"]} for v in verdicts])
    report = {"as_of": end, "window": {"start": start, "end": end},
              "wf": {"train_min": wf[0], "step": wf[1]},
              "lockbox_months": lockbox_months, "corr_threshold": corr_threshold,
              "verdicts": verdicts, "corr_matrix": corr, "multi_test": mt,
              "protocol_notes": [
                  "④a walk-forward：扩展窗口，门槛 = 样本外 IC 衰减 < 50% 且单一年度贡献 ≤ 50%",
                  "④b 参数网格：winsorize 分位 ±30% × 行业中性化开关；方向不反转 + IC_IR 漂移 < 40%",
                  "④c 环境：牛熊（基准 6 月趋势）；大小盘/成长价值轴数据不足显式降级；局部失效可解释",
                  "⑤ 增量：|ρ̄| ≥ 0.6 → 转「替代版本」对比通道（不注册为独立因子）",
                  "⑥ 多重检验：同批 < 20 候选暂缓；Bonferroni + Deflated Sharpe 流程就绪",
                  "lockbox 末端封存：不参与任何门槛，仅报告观察值"]}

    if out_dir is not None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "m4_governance.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        (out / "m4_governance.md").write_text(render_md(report), encoding="utf-8")
    return report


# ── 报告 ──
def render_md(rep: dict) -> str:
    """M4 关卡报告：裁决看板 + 逐因子明细 + 相关性矩阵看板 + 口径与局限。"""
    n_pass = sum(1 for v in rep["verdicts"] if v["overall"] == "pass")
    n_alt = sum(1 for v in rep["verdicts"] if v["overall"] == "alternative")
    lines = [f"# M4 稳健性关卡报告（as_of={rep['as_of']}）", "",
             f"- 窗口：{rep['window']['start']}..{rep['window']['end']} · "
             f"walk-forward train/step = {rep['wf']['train_min']}/{rep['wf']['step']} · "
             f"lockbox = {rep['lockbox_months']} 月",
             f"- 裁决：**{n_pass} pass / {n_alt} alternative（替代通道） / "
             f"{len(rep['verdicts']) - n_pass - n_alt} fail**（共 {len(rep['verdicts'])} 因子）",
             "", "## 裁决看板", "",
             "| 因子 | 主窗 IC | ④a 时间 | ④b 参数 | ④c 环境 | ⑤ 增量 | PIT | 裁决 |",
             "|---|---|---|---|---|---|---|---|"]
    mark = {True: "✅", False: "❌"}
    for v in rep["verdicts"]:
        ic = v["main_ic"]
        ic_txt = ("—" if ic["mean"] is None else
                  f"{ic['mean']:.3f}（IR {'—' if ic['ir'] is None else format(ic['ir'], '.2f')}）")
        lines.append(
            f"| {v['factor_id']} | {ic_txt} | {mark[v['time_gate']['pass']]} | "
            f"{mark[v['param_gate']['pass']]} | {mark[v['regime_gate']['pass']]} | "
            f"{('✅' if v['incremental']['pass'] else '🔁 转替代通道')} | "
            f"{v['pit']['violations']} 违例 | **{v['overall']}** |")
    lines += ["", "## 逐因子明细", ""]
    for v in rep["verdicts"]:
        tg, pg, rg, ig = v["time_gate"], v["param_gate"], v["regime_gate"], v["incremental"]
        wf = tg["walk_forward"]
        tr_ic = wf["train_ic_mean"]
        va_ic = wf["valid_ic_mean"]
        lb_ic = tg["lockbox"]["mean_ic"]
        decay = f"{wf['decay_pooled']:.1%}" if wf["decay_pooled"] is not None else "—"
        drift = (f"{pg['ir_drift_max']:.0%}" if pg["ir_drift_max"] is not None else "—")
        ytxt = "；".join(
            (f"{y} {r['share'] * 100:.0f}%" if r["share"] is not None else f"{y} —")
            for y, r in tg["yearly"]["years"].items())
        lines += [f"### {v['factor_id']}（{v['name']}，direction={v['direction']:+d}）→ {v['overall']}", "",
                  f"- ④a 时间：{tg['reason']}；train IC "
                  f"{'—' if tr_ic is None else f'{tr_ic:.3f}'} · valid IC "
                  f"{'—' if va_ic is None else f'{va_ic:.3f}'} · 池化衰减 {decay}"
                  f"（{wf['n_folds']} 折）",
                  f"  - 年度贡献：{ytxt or '—'}",
                  f"  - lockbox（封存）：IC {'—' if lb_ic is None else f'{lb_ic:.3f}'}"
                  f"（{tg['lockbox']['n']} 月）",
                  f"- ④b 参数：{pg['reason']}；IR 最大漂移 {drift}",
                  f"- ④c 环境：{rg['reason']}；降级轴 {rg['n_degenerate_axes']} 个（诚实标注）",
                  f"- ⑤ 增量：{ig['reason']}"]
        lines.append("")
    corr = rep["corr_matrix"]
    lines += ["## 相关性矩阵看板（月度截面 Spearman ρ̄，⑤ 增量价值）", ""]
    fs = corr["factors"]
    lines += ["| " + " | ".join([""] + fs) + " |",
              "|---" * (len(fs) + 1) + "|"]
    for a in fs:
        cells = []
        for b in fs:
            if a == b:
                cells.append("—")
            else:
                key = f"{a}|{b}" if fs.index(a) < fs.index(b) else f"{b}|{a}"
                mr = corr["pairs"][key]["mean_rho"]
                cells.append("—" if mr is None else f"{mr:.2f}")
        lines.append("| " + " | ".join([a] + cells) + " |")
    mt = rep["multi_test"]
    lines += ["", "## ⑥ 多重检验", "",
              f"- 状态：{'触发' if mt['triggered'] else '暂缓'}（同批 {mt['n']} 候选 / "
              f"门槛 {mt['min_batch']}）" + (f"；Bonferroni α* = {mt['bonferroni_alpha']:.4f}"
                                             if mt["triggered"] else "；流程就绪"),
              "", "## 口径与局限（诚实原则）", ""]
    for n_ in rep["protocol_notes"]:
        lines.append(f"- {n_}")
    lines += ["- 4 股票截面功效弱：关卡结论为「机制成立 + 待全量历史补强」，不静默放大；",
              "- 行业中性化轴（4 标的 4 行业）、大小盘/成长价值轴在库内退化为标注项。"]
    return "\n".join(lines) + "\n"


def main() -> None:
    """CLI：python -m factorlib.governance --repo <dir> [--start --end --out --factors]"""
    import argparse
    ap = argparse.ArgumentParser(description="M4 稳健性三关 + 增量价值 + 多重检验")
    ap.add_argument("--repo", required=True, help="alphapocket repo 目录")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--out", default=None, help="报告落盘目录")
    ap.add_argument("--factors", default=None, help="逗号分隔因子 id（默认全部 alpha）")
    ap.add_argument("--wf", default="24,6", help="walk-forward train_min,step（默认 24,6）")
    ap.add_argument("--lockbox", type=int, default=LOCKBOX_DEFAULT)
    args = ap.parse_args()
    wf = tuple(int(x) for x in args.wf.split(","))
    rep = run_governance(args.repo,
                         factor_ids=args.factors.split(",") if args.factors else None,
                         start=args.start, end=args.end, out_dir=args.out,
                         wf=wf, lockbox_months=args.lockbox)
    for v in rep["verdicts"]:
        print(f"{v['factor_id']:<20} {v['overall']:<12} {v['time_gate']['reason'][:40]}")


if __name__ == "__main__":
    main()
