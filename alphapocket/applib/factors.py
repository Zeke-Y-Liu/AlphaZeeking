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


# ══ 引擎C · 宏观流动性计算层（T-C2）══════════════════════════════════════════
# 纯函数：无 IO、无时钟（幂等重算一致）；输入 = T-C1 macro_liquidity payload 的
# indicators 字段 + as_of（组合 K 线最后交易日，由调用方传入——不取未来）。
# 不决策：输出为市场条件变量（regime 温度计），不产生买卖信号、不参与权重；
# 消费方（T-C3 工具层 / T-C5 报告层）仅做展示与参考。
# 输出位范围约束（engines/macro.py 包装器语义，组件化 T-R2/R3 收编）：档位恒在
# -2..+2 ∪ {"unavailable"}，评分 clamp 于 [-1,1]。
# 三层聚合：① scorer 表（逐指标：分位点×方向 → 阈值触发 / 阈值跳平 / 前值站位 /
# 样本不足回退）② 维度等权（D1–D4 维内均值→跨维均值，缺维度不折损已知维度）
# ③ 检验状态机（neutral / 偏松 / 偏紧 / unavailable）。

MACRO_DIMS = {"D1": "央行注入", "D2": "货币总量", "D3": "价格信号", "D4": "跨境流动"}
MACRO_BAND_MID = 0.2        # 跳平带：|score| < 0.2 → 0 档
MACRO_BAND_HI = 0.6         # 触发带：|score| >= 0.6 → 极端档（±2）；状态机 偏松/偏紧 分界
MACRO_WINDOW_MONTHLY = 12   # 月度指标 12 月滚动窗
MACRO_WINDOW_DAILY = 60     # 日度/事件指标滚动窗
MACRO_MIN_MONTHLY = 6       # 有效样本下限，不足 → 回退（0 档 insufficient）
MACRO_MIN_DAILY = 20
MACRO_STATES = {"loose": "偏松", "tight": "偏紧", "neutral": "neutral", "na": "unavailable"}

# 方向映射（scorer 表输入）：+1 = 值越高越宽松；-1 = 值越高越紧
MACRO_DIRECTIONS = {
    "cn_m2_yoy": +1, "cn_srzg": +1,
    "shibor_1w": -1, "lpr_1y": -1, "fr007": -1,
    "cn_us_spread_10y": +1,
    "us_fed_rate": -1, "us_10y_2y": +1,
    "us_cpi_yoy": -1, "us_nonfarm": -1, "us_gdp": -1,
    "hk_hibor_1m": -1, "hk_hibor_3m": -1,
    "hk_usdcnh": -1,   # 港汇方向映射随 H-0006 实测数据复核（本会话 HK 降级不影响主线）
}


def _macro_bands(score: float) -> int:
    """档位映射（阈值触发 + 跳平带）：|s|>=HI → ±2；|s|>=MID → ±1；|s|<MID → 0（跳平）。"""
    if score >= MACRO_BAND_HI:
        return 2
    if score >= MACRO_BAND_MID:
        return 1
    if score > -MACRO_BAND_MID:
        return 0
    if score > -MACRO_BAND_HI:
        return -1
    return -2


def _macro_is_monthly(series: list[dict]) -> bool:
    """月度指标识别：全部 dt 为月初站位（T-C1 约定）→ 月度。"""
    return bool(series) and all(str(r["dt"]).endswith("-01") for r in series)


def _macro_usable(series: list[dict], as_of: str, monthly: bool) -> list[dict]:
    """PIT 可用集（不取未来）：日度/事件 dt<=as_of；月度 dt < as_of 当月（前值站位防前视）。"""
    ms = as_of[:8] + "01"
    return [r for r in series if r["dt"] < ms] if monthly else [r for r in series if r["dt"] <= as_of]


def _macro_scorer(meta: dict, series: list[dict], as_of: str) -> dict:
    """单指标 scorer 表：分位点×方向 → score ∈ [-1,1] → 档位 ∈ {-2..2}；新+旧双期排位。

    四路径：触发（越阈值给极端档）/ 跳平（|s|<MID → 0）/ 前值站位（月度当月点不可用，
    上月值站位；stale 标注站位滞后）/ 回退（样本不足 → 0 档 insufficient）。
    """
    sid = meta.get("id", "?")
    dirn = MACRO_DIRECTIONS.get(sid, 0)
    monthly = _macro_is_monthly(series)
    win_max = MACRO_WINDOW_MONTHLY if monthly else MACRO_WINDOW_DAILY
    win_min = MACRO_MIN_MONTHLY if monthly else MACRO_MIN_DAILY
    base = {"name": meta.get("name"), "market": meta.get("market"), "dim": meta.get("dim"),
            "dir": dirn, "monthly": monthly, "score": None, "level": None,
            "pctile": None, "prev_pctile": None, "cur_dt": None, "cur_value": None,
            "stale": False, "n_window": 0}
    if not series:
        return {**base, "path": "no_data", "level": None}
    usable = _macro_usable(series, as_of, monthly)
    values = [r["value"] for r in usable]
    if len(values) < win_min:
        # 回退：样本不足 → 0 档 insufficient（展示 0 档；聚合时排除，不折损其余指标）
        return {**base, "path": "insufficient", "level": 0, "n_window": len(values)}
    cur = usable[-1]
    # 站位滞后标注：月度最新可用点早于 as_of 上一月 → 前值站位到更旧月份；日度滞后 > 10 自然日
    if monthly:
        prev_month = _prev_month_start(as_of)
        stale = cur["dt"] < prev_month
    else:
        y, m, d = map(int, as_of.split("-"))
        stale = cur["dt"] < (dt.date(y, m, d) - dt.timedelta(days=10)).isoformat()
    p = _pctile(values[-win_max:], cur["value"])
    score = round(max(-1.0, min(1.0, (2 * p - 1) * dirn)), 4)
    prev_p = None
    if len(values) >= 2:
        prev_p = _pctile(values[-(win_max + 1):-1], values[-2])
    return {**base, "path": "carried_prev" if stale else "ok", "stale": stale,
            "pctile": round(p, 4), "prev_pctile": None if prev_p is None else round(prev_p, 4),
            "cur_dt": cur["dt"], "cur_value": cur["value"],
            "score": score, "level": _macro_bands(score), "n_window": min(len(values), win_max)}


def _prev_month_start(as_of: str) -> str:
    y, m = int(as_of[:4]), int(as_of[5:7])
    return f"{y - 1}-12-01" if m == 1 else f"{y}-{m - 1:02d}-01"


def compute_macro_liquidity(indicators: dict, as_of: str) -> dict:
    """引擎C T-C2：12+ 指标时序 → 三市场温度计（冻结契约，幂等重算一致）。

    输入：indicators = macro_liquidity.json 的 indicators 字段（T-C1 产物，含 series）；
    as_of = 组合 K 线最后交易日（调用方传入，PIT 不取未来）。
    输出（冻结）：``{state, global, markets: {A, HK, US}, global_score, as_of}``；
    ``detail`` 为非冻结扩展（scorer 表 + 聚合中间量），供 T-C3/T-C5 消费。
    纪律：纯函数不决策；输出仅为市场条件变量，不产生买卖信号、不参与权重。
    """
    detail: dict[str, dict] = {}
    dim_scores: dict[str, dict[str, list[float]]] = {m: {} for m in ("A", "HK", "US")}
    for sid, meta in indicators.items():
        sc = _macro_scorer({**meta, "id": sid}, meta.get("series") or [], as_of)
        detail[sid] = sc
        if sc["path"] in ("ok", "carried_prev") and sc["market"] in dim_scores:
            dim_scores[sc["market"]].setdefault(sc["dim"], []).append(sc["score"])

    # ② 维度等权：维内均值 → 跨维均值（缺维度/缺市场不折损已知维度均值）
    market_scores: dict[str, float | None] = {}
    for mkt, dims in dim_scores.items():
        if dims:
            market_scores[mkt] = round(sum(sum(v) / len(v) for v in dims.values()) / len(dims), 4)
        else:
            market_scores[mkt] = None

    markets: dict[str, int | str] = {}
    for mkt, s in market_scores.items():
        markets[mkt] = "unavailable" if s is None else _macro_bands(s)

    valid = {m: s for m, s in market_scores.items() if s is not None}
    if valid:
        global_score = round(sum(valid.values()) / len(valid), 3)
        global_lvl = _macro_bands(global_score)
        state = (MACRO_STATES["loose"] if global_score >= MACRO_BAND_HI
                 else MACRO_STATES["tight"] if global_score <= -MACRO_BAND_HI
                 else MACRO_STATES["neutral"])
    else:
        global_score, global_lvl, state = None, "unavailable", MACRO_STATES["na"]

    return {"state": state, "global": global_lvl, "markets": markets,
            "global_score": global_score, "as_of": as_of,
            "detail": {"indicators": detail, "market_scores": market_scores,
                       "dims": MACRO_DIMS,
                       "bands": {"mid": MACRO_BAND_MID, "hi": MACRO_BAND_HI,
                                 "window_monthly": MACRO_WINDOW_MONTHLY,
                                 "window_daily": MACRO_WINDOW_DAILY,
                                 "min_monthly": MACRO_MIN_MONTHLY, "min_daily": MACRO_MIN_DAILY}}}
