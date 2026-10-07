"""因子计算核心（真实数据）：引擎 A（中长期）+ 引擎 B（20日信号）

引擎 A：估值（PE/PB·五年分位）、质量（ROE_TTM/利润率）、成长（营收/净利同比）、
        动量（12-1月）、红利低波（股息率TTM/250日波动）—— PIT 阶梯函数对齐报告可用人时点。
引擎 B：20日窗口 mom/vol/MA20突破/20日新高/距高点——增强不决策。

全部输入来自 dwd 层（AKShare 真实数据），输出确定性可复现。
"""
from __future__ import annotations
import datetime as dt

ENGINE_A_FACTORS = ["val_pe_ttm", "val_pb", "val_pe_pct", "val_pb_pct", "div_yield_ttm",
                    "qua_roe_ttm", "qua_margin", "grow_rev_yoy", "grow_np_yoy", "mom_12_1", "vol_250",
                    # P1.1 反转族（价格类，超跌为正）
                    "rev_20d", "rev_60d",
                    # P1.2 品质族补齐（需 A 股三表科目，D5）
                    "qua_leverage", "qua_accrual", "qua_fcf"]
ENGINE_B_FACTORS = ["mom_20d", "vol_20d", "ma20_break", "high20_touch", "dist_high20",
                    # P1.3 量价情绪（成交量维度）
                    "turnover_pct"]

# 五维默认权重（锚点：与历史硬编码一致；R1 敏感度扫描通过 compute_alpha(weights=...) 覆盖）
DEFAULT_DIM_WEIGHTS = {"估值": .25, "质量": .25, "成长": .20, "动量": .15, "红利低波": .15}


# ── 工具 ──────────────────────────────────────────────
def _interp(x, anchors):
    """分段线性插值打分：anchors=[(x,score),...] 升序"""
    if x is None:
        return None
    if x <= anchors[0][0]:
        return anchors[0][1]
    if x >= anchors[-1][0]:
        return anchors[-1][1]
    for (x0, s0), (x1, s1) in zip(anchors, anchors[1:]):
        if x0 <= x <= x1:
            return s0 + (s1 - s0) * (x - x0) / (x1 - x0) if x1 != x0 else s0
    return None


def _grade(score):
    if score is None:
        return "—"
    for th, g in [(85, "A"), (70, "B+"), (60, "B"), (50, "C+"), (40, "C"), (30, "D+")]:
        if score >= th:
            return g
    return "D"


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """皮尔逊相关系数；样本 < 3 或任一常数列 → None（P1.3 量价相关性用）。"""
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0 or vy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (vx * vy) ** 0.5


def _pctile(hist: list[float], cur: float | None) -> float | None:
    """当前值在历史序列中的分位（0–1，并列取 0.5 权重）；空序列或 cur=None → None。"""
    if cur is None or not hist:
        return None
    return (sum(1 for v in hist if v < cur) + 0.5 * sum(1 for v in hist if v == cur)) / len(hist)


def rev(secid: str, bars: list[dict], n: int, as_of: str | None = None) -> float | None:
    """n 交易日反转因子：过去 n 日累计收益取负（超跌为正 → 预期均值回复，P1.1）。

    行业中性化在截面评估层完成（本函数为单标的取值）；当前 α universe 仅 4 只股票且各属
    不同行业 → 行业中性退化为横截面去均值，见 ``industry_neutralize``。
    """
    closes = [b["close"] for b in bars if b.get("close") and (as_of is None or b["dt"] <= as_of)]
    if len(closes) < n + 1:
        return None
    return -(closes[-1] / closes[-1 - n] - 1)


def industry_neutralize(vals: dict[str, float | None], industries: dict[str, str]) -> dict[str, float | None]:
    """行业内去均值（行业中性化，P1.1）：同行业截面减行业均值；单只成组 → 减自身 → 0。

    universe 扩张后无损接入；当前 α universe（4 股/4 行业）中性后趋 0，属数据面限制。
    """
    groups: dict[str, list[str]] = {}
    for s, v in vals.items():
        if v is not None:
            groups.setdefault(industries.get(s) or "—", []).append(s)
    out: dict[str, float | None] = {}
    for s, v in vals.items():
        if v is None:
            out[s] = None
            continue
        g = groups.get(industries.get(s) or "—", [])
        gm = sum(vals[x] for x in g) / len(g) if g else 0.0
        out[s] = v - gm
    return out


def quality_factors(R: dict | None) -> dict:
    """品质族补齐（P1.2）：偿债 / 应计质量 / 自由现金流率 —— 取自 A 股三表科目（PIT 报告期）。

    - ``qua_leverage``：资产负债率 = 负债合计/资产总计（%，越低越优）
    - ``qua_accrual``：应计质量 = 经营活动现金流净额/净利润（越高越优，盈利含金量）
    - ``qua_fcf``：自由现金流率 = (经营现金流净额−资本开支)/营业总收入（%，越高越优）
    三表科目为 YTD 累计口径 → 比值为同期口径自洽；科目缺失（非 A 股）→ None（不适用）。
    """
    if not R:
        return {"qua_leverage": None, "qua_accrual": None, "qua_fcf": None}
    ta, tl = R.get("total_assets"), R.get("total_liab")
    ocf, capex, rev, np_ = R.get("ocf"), R.get("capex"), R.get("revenue"), R.get("net_profit")
    lev = (tl / ta * 100) if (ta and tl is not None and ta > 0) else None
    accr = (ocf / np_) if (ocf is not None and np_ and np_ > 0) else None
    fcf = ((ocf - (capex or 0.0)) / rev * 100) if (ocf is not None and rev and rev > 0) else None
    return {"qua_leverage": lev, "qua_accrual": accr, "qua_fcf": fcf}


# ── PIT：报告期可用性 ──────────────────────────────────
def latest_report(reports: list[dict], t: str) -> dict | None:
    """t 日（含）已可用的最新报告期（按 period 优先、announce 兜底）"""
    avail = [r for r in reports if r["announce_date"] <= t]
    return max(avail, key=lambda r: (r["period"], r["announce_date"])) if avail else None


def _by_period(reports: list[dict], period: str, t: str) -> dict | None:
    for r in reports:
        if r["period"] == period and r["announce_date"] <= t:
            return r
    return None


def ttm_eps(reports: list[dict], t: str) -> float | None:
    """TTM 摊薄 EPS；港股/美股取上游直出 ``eps_ttm``，A 股按 YTD 递推。

    - 港股/美股：报告自带 ``eps_ttm``（港股 EPS_TTM 列 / 美股近 4 季合计）——
      美股单季报口径下 YTD 递推不成立，凡带该键即以其为准（缺失则降级 None）。
    - A 股（无 ``eps_ttm`` 键）：上年FY + 本年YTD − 上年同期YTD（FY 报告期直接取全年）。
    """
    R = latest_report(reports, t)
    if R is None:
        return None
    if "eps_ttm" in R:
        return R["eps_ttm"]
    if R.get("eps_ytd") is None:
        return None
    y, md = R["period"][:4], R["period"][5:]
    if md == "12-31":
        return R["eps_ytd"]
    py = int(y) - 1
    fy = _by_period(reports, f"{py}-12-31", t)
    prev = _by_period(reports, f"{py}-{md}", t)
    if fy and prev and fy.get("eps_ytd") is not None and prev.get("eps_ytd") is not None:
        return fy["eps_ytd"] + R["eps_ytd"] - prev["eps_ytd"]
    return None


# ── 引擎 A ────────────────────────────────────────────
def compute_alpha(secid: str, bars: list[dict], reports: list[dict], dividends: list[dict],
                  as_of: str | None = None, weights: dict | None = None) -> dict:
    """bars/reports 按 dt/announce 升序；返回 as_of 的引擎 A 全因子 + 五维评分

    ``weights``：五维权重（默认 None = 内置 ``DEFAULT_DIM_WEIGHTS``，R1 敏感度扫描可配）；
    默认权重下输出与历史版本逐位一致（锚点纪律）。
    """
    closes = [(b["dt"], b["close"]) for b in bars if b["close"]]
    as_of = as_of or closes[-1][0]
    hist = [(d, c) for d, c in closes if d <= as_of]
    close = hist[-1][1]

    # PE/PB 历史序列（PIT 阶梯）与五年分位
    win_start = (dt.date.fromisoformat(as_of) - dt.timedelta(days=365 * 5)).isoformat()
    pe_s, pb_s = [], []
    for d, c in hist:
        e = ttm_eps(reports, d)
        R = latest_report(reports, d)
        if e and e > 0:
            pe_s.append((d, c / e))
        if R and R.get("bps") and R["bps"] > 0:
            pb_s.append((d, c / R["bps"]))
    pe_win = [v for d, v in pe_s if d >= win_start]
    pb_win = [v for d, v in pb_s if d >= win_start]
    pe = pe_s[-1][1] if pe_s else None
    pb = pb_s[-1][1] if pb_s else None
    pct = lambda w, cur: (sum(1 for v in w if v < cur) + 0.5 * sum(1 for v in w if v == cur)) / len(w) if (w and cur) else None
    pe_pct, pb_pct = pct(pe_win, pe), pct(pb_win, pb)

    # 报告期类因子（取 as_of 可用最新报告）
    R = latest_report(reports, as_of)
    e = ttm_eps(reports, as_of)
    bps = R.get("bps") if R else None
    roe_ttm = (e / bps * 100) if (e and bps) else None

    # 股息率 TTM（近365日除权除息，闭区间 [as_of-365d, as_of]）
    dvd_start = (dt.date.fromisoformat(as_of) - dt.timedelta(days=365)).isoformat()
    # 无分红数据（ETF/LOF）→ 不适用（None）；有数据但窗口内无除权 → 0（真实为零）
    dps_sum = (sum(d["dps"] for d in dividends if dvd_start <= d["ex_date"] <= as_of and d.get("dps"))
               if dividends else None)
    div_yield = dps_sum / close if (dps_sum is not None and close) else None

    # 动量与波动（价格类）
    cs = [c for _, c in hist]
    mom_12_1 = cs[-21] / cs[-252] - 1 if len(cs) >= 252 else None
    rets = [cs[i] / cs[i - 1] - 1 for i in range(max(1, len(cs) - 250), len(cs))]
    vol_250 = (sum((r - sum(rets) / len(rets)) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5 * (252 ** 0.5) if len(rets) > 2 else None

    # P1.1 反转族：过去 20/60 交易日累计收益取负（超跌为正，预期均值回复）
    rev_20d = -(cs[-1] / cs[-21] - 1) if len(cs) >= 21 else None
    rev_60d = -(cs[-1] / cs[-61] - 1) if len(cs) >= 61 else None

    # P1.2 品质族补齐：三表科目（仅 A 股有；缺失 → None 不适用）
    qf = quality_factors(R)

    # 五维评分（0-100，锚点见 report 说明）
    s_val = (lambda a, b: (a + b) / 2 if (a is not None and b is not None) else (a if a is not None else b))(
        *(100 - p * 100 if p is not None else None for p in (pe_pct, pb_pct)))
    s_qua = _interp(roe_ttm, [(0, 10), (5, 35), (10, 55), (15, 75), (20, 90), (30, 100)])
    gs = [R.get("rev_g"), R.get("np_g")] if R else [None, None]
    gs = [g for g in gs if g is not None]
    s_gro = _interp(sum(gs) / len(gs), [(-20, 10), (0, 40), (10, 60), (20, 75), (35, 90), (60, 100)]) if gs else None
    s_mom = _interp(mom_12_1, [(-30, 10), (-10, 30), (0, 45), (10, 60), (30, 80), (60, 100)])
    s_dvd = _interp(div_yield * 100 if div_yield is not None else None, [(0, 10), (1, 35), (2, 55), (3, 70), (5, 90), (7, 100)])
    s_lvol = _interp(vol_250 * 100 if vol_250 is not None else None, [(15, 100), (20, 85), (30, 60), (40, 40), (60, 20)])
    s_dl = (s_dvd + s_lvol) / 2 if (s_dvd is not None and s_lvol is not None) else (s_dvd if s_dvd is not None else s_lvol)
    dims = {"估值": s_val, "质量": s_qua, "成长": s_gro, "动量": s_mom, "红利低波": s_dl}
    W = weights or DEFAULT_DIM_WEIGHTS
    w_sum = sum(W[k] for k in W if dims[k] is not None)
    tot = sum(dims[k] * W[k] for k in W if dims[k] is not None) / w_sum if w_sum else None
    if R is None:
        tot = None   # 无财报数据（ETF/LOF/指数类）：五维综合不适用（动量/波动因子照常计算）

    return {
        "as_of": as_of, "close": close,
        "val_pe_ttm": pe, "val_pb": pb, "val_pe_pct": pe_pct, "val_pb_pct": pb_pct,
        "div_yield_ttm": div_yield, "qua_roe_ttm": roe_ttm,
        "qua_margin": R.get("margin") if R else None,
        "grow_rev_yoy": R.get("rev_g") if R else None,
        "grow_np_yoy": R.get("np_g") if R else None,
        "mom_12_1": mom_12_1, "vol_250": vol_250,
        # P1.1 反转 / P1.2 品质补齐
        "rev_20d": rev_20d, "rev_60d": rev_60d,
        "qua_leverage": qf["qua_leverage"], "qua_accrual": qf["qua_accrual"], "qua_fcf": qf["qua_fcf"],
        "used_period": R["period"] if R else None,
        "used_announce": R["announce_date"] if R else None,
        "pe_hist_n": len(pe_win), "pb_hist_n": len(pb_win),
        "dims": {k: round(v, 1) if v is not None else None for k, v in dims.items()},
        "grades": {k: _grade(v) for k, v in dims.items()},
        "composite": round(tot, 1) if tot is not None else None,
        "composite_grade": _grade(tot),
    }


# ── 引擎 B ────────────────────────────────────────────
def compute_beta(secid: str, bars: list[dict], as_of: str | None = None,
                 win: int = 20, band: float = 0.02, band_sigma: float = 1.5,
                 bias_thresh: float = 0.08, vpd_thresh: float = 0.30,
                 pct_window: int = 250) -> dict:
    """引擎 B：20 日窗口信号（参数化）。

    - ``win``：MA / 动量 / 高点窗口（默认 20，R1 敏感度扫描可配）
    - ``band``：MA 突破确认带宽（默认 ±2%，R1 可配）
    - ``band_sigma``：波动止损带宽（默认 1.5σ，R1 可配）
    - ``bias_thresh``：极端乖离阈值（默认 ±8%，P1.1 sig_bias_ext）
    - ``vpd_thresh``：量价背离阈值（20 日量价相关性 ≤ −阈值，默认 0.30，P1.3）
    - ``pct_window``：换手率代理分位窗口（默认 250 交易日，P1.3）

    默认参数下既有字段（mom_20d/vol_20d/ma20_break/…/state）与历史版本逐位一致（锚点纪律）。
    """
    n = int(win)
    closes = [(b["dt"], b["close"]) for b in bars if b["close"]]
    highs = [(b["dt"], b["high"]) for b in bars if b.get("high")]
    as_of = as_of or closes[-1][0]
    hist_c = [c for d, c in closes if d <= as_of]
    hist_h = [h for d, h in highs if d <= as_of]
    hist_l = [b["low"] for b in bars if b["dt"] <= as_of and b.get("low")]
    hist_amt = [b.get("amount") for b in bars if b.get("close") and b["dt"] <= as_of]   # 与 hist_c 同序对齐
    close = hist_c[-1]

    w = hist_c[-n:]
    ma20 = sum(w) / n
    ratio = close / ma20 - 1
    mom = close / w[0] - 1
    rets20 = [hist_c[i] / hist_c[i - 1] - 1 for i in range(len(hist_c) - n, len(hist_c))]
    mu = sum(rets20) / len(rets20)
    vol20 = (sum((r - mu) ** 2 for r in rets20) / (len(rets20) - 1)) ** 0.5 * (252 ** 0.5)
    high20_close = max(w)
    high20_intra = max(hist_h[-n:])
    low20 = min(hist_l[-n:])
    high20_touch = 1 if close >= high20_close else 0
    dist_high20 = close / high20_intra - 1
    band_pct = f"±{band * 100:g}%"

    # P1.3 量价情绪：20 日量价相关性（日收益 vs 成交额变化）+ 换手率代理分位（成交额 pct_window 日分位）
    r_all = [None] * len(hist_c)
    da = [None] * len(hist_c)
    for i in range(1, len(hist_c)):
        r_all[i] = hist_c[i] / hist_c[i - 1] - 1
        a0, a1 = hist_amt[i - 1], hist_amt[i]
        da[i] = (a1 / a0 - 1) if (a0 not in (None, 0) and a1 is not None) else None
    idx = [i for i in range(len(hist_c) - n, len(hist_c)) if r_all[i] is not None and da[i] is not None]
    vp_corr = _pearson([r_all[i] for i in idx], [da[i] for i in idx])
    amt_hist = [a for a in hist_amt if a is not None][-int(pct_window):]
    turnover_pct = _pctile(amt_hist, hist_amt[-1] if hist_amt else None)   # 换手率代理：成交额分位

    if ratio < -band:
        state, sig = "below", {"signal_id": "sig_ma20_break", "type": "卖出预警",
                               "detail": f"收盘低于 MA20 {ratio:+.1%}（{band_pct} 确认带外）", "evidence": "E2",
                               "action": "生成减仓评估单（不自动卖出）", "cooldown": "10 交易日"}
    elif ratio > band:
        state, sig = "above", {"signal_id": "sig_ma20_break", "type": "买入/持有确认",
                               "detail": f"收盘高于 MA20 {ratio:+.1%}（{band_pct} 确认带外）", "evidence": "E2",
                               "action": "引擎A目标仓位执行确认（模式③）", "cooldown": "10 交易日"}
    else:
        state, sig = "neutral", None
    sigs = [sig] if sig else []
    if high20_touch:
        sigs.append({"signal_id": "sig_high20_touch", "type": "持有确认",
                     "detail": f"收盘创 {n} 日收盘新高（{close:.2f}）", "evidence": "E2",
                     "action": "趋势延续确认，持有（引擎A持仓内）", "cooldown": "10 交易日"})
    # P1.1 极端乖离（反转风险）：|乖离率| ≥ bias_thresh
    if abs(ratio) >= bias_thresh:
        sigs.append({"signal_id": "sig_bias_ext",
                     "type": "卖出预警" if ratio > 0 else "买入/持有确认",
                     "detail": f"乖离率 {ratio:+.1%}（|乖离| ≥ {bias_thresh * 100:g}%，"
                               f"{'超买' if ratio > 0 else '超卖'}极端）", "evidence": "E2",
                     "action": "反转风险提示：超买减仓评估 / 超卖关注回归（不自动交易）",
                     "cooldown": "10 交易日"})
    # P1.3 量价背离：20 日量价相关性 ≤ −vpd_thresh（价升量缩）
    if vp_corr is not None and vp_corr <= -vpd_thresh:
        sigs.append({"signal_id": "sig_vol_price_div", "type": "卖出预警",
                     "detail": f"20 日量价相关性 {vp_corr:+.2f} ≤ −{vpd_thresh:g}（价升量缩/量价背离）",
                     "evidence": "E2", "action": "趋势可持续性存疑：减仓评估（不自动卖出）",
                     "cooldown": "10 交易日"})

    sd = vol20 / (252 ** 0.5)  # 日波动
    return {
        "as_of": as_of, "close": close, "mom_20d": mom, "vol_20d": vol20,
        "ma20": ma20, "ma20_break": ratio, "state": state,
        "high20_touch": high20_touch, "dist_high20": dist_high20,
        "high20_intra": high20_intra, "low20": low20,
        # P1.3 量价情绪
        "turnover_pct": turnover_pct, "vol_price_corr": vp_corr,
        "sigma_daily": sd,
        "band_low": ma20 * (1 - band_sigma * sd), "band_high": ma20 * (1 + band_sigma * sd),
        "vol_stop": bool(close < ma20 * (1 - band_sigma * sd)),
        "ma_window": n, "band": band, "band_sigma": band_sigma,
        "bias_thresh": bias_thresh, "vpd_thresh": vpd_thresh, "pct_window": int(pct_window),
        "signals": sigs,
    }


# 引擎 B 信号事件 id（R4 延迟衰减 / R1 敏感度共用）
ENGINE_B_SIGNAL_IDS = ["sig_ma20_break", "sig_high20_touch", "sig_bias_ext", "sig_vol_price_div"]


def iter_signal_events(secid: str, bars: list[dict], signal_id: str = "all", horizon: int = 20,
                       win: int = 20, band: float = 0.02, band_sigma: float = 1.5,
                       start: str | None = None, end: str | None = None) -> list[dict]:
    """阶段〇 0.3：逐日 as_of 调用参数化 ``compute_beta`` 识别全部历史信号事件。

    仅在该标的自身有效 K 线（``close`` 为真）序列上循环；每个事件带四种执行价
    ``t0_close / t1_open / t1_close / t2_open``（不足则 None）与固定窗口退出价
    ``exit_close``（信号后第 ``horizon`` 个交易日收盘），供 R4 延迟衰减实证使用。

    默认参数下事件与 ``compute_beta`` 逐日输出一致（锚点纪律）。
    """
    valid = [b for b in bars if b.get("close")]
    h = int(horizon)
    evs = []
    for i, b in enumerate(valid):
        d = b["dt"]
        if (start and d < start) or (end and d > end):
            continue
        if i < int(win):          # 窗口不足（compute_beta 需 ≥ win+1 根）→ 无信号可识别
            continue
        r = compute_beta(secid, bars, d, win, band, band_sigma)
        if not r["signals"]:
            continue
        n1 = valid[i + 1] if i + 1 < len(valid) else None
        n2 = valid[i + 2] if i + 2 < len(valid) else None
        fwd = valid[i + 1:i + h + 1]
        exit_close = fwd[-1]["close"] if len(fwd) >= h else None
        for s in r["signals"]:
            if signal_id != "all" and s["signal_id"] != signal_id:
                continue
            evs.append({
                "secid": secid, "dt": d, "signal_id": s["signal_id"], "type": s["type"],
                "exec_prices": {"t0_close": b["close"],
                                "t1_open": n1.get("open") if n1 else None,
                                "t1_close": n1["close"] if n1 else None,
                                "t2_open": n2.get("open") if n2 else None},
                "exit_close": exit_close,
            })
    return evs
